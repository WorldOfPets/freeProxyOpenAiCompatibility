# Провайдеры

Все три провайдера работают **внутри `uv run freeproxy`**. Отдельные bridge'и,
Node.js и Go в runtime не нужны.

| Провайдер | Транспорт | Порт | Что требуется |
|---|---|---:|---|
| GLM / Z.AI | прямой HTTP + подпись + Aliyun captcha | — | `freeproxy-auth login glm`, при risk-отказе — `tokens.sqlite` |
| Qwen Chat | встроенный Playwright-сеанс | — | Chrome/Chromium и `freeproxy-auth login qwen` |
| DeepSeek Web | прямой HTTP + PoW (`wasmtime`) | — | `freeproxy-auth login deepseek` |

## Быстрый старт

```bash
uv sync
uv run playwright install chromium   # если нет Google Chrome
uv run freeproxy-auth login all
uv run python scripts/check_upstreams.py
uv run freeproxy
```

`freeproxy-auth` открывает обычный Chrome, ждёт вашего входа и **Enter** как
подтверждения, затем сохраняет токены в `secrets/tokens.json`. Подробно:
[auth.md](auth.md).

## GLM / Z.AI

`src/freeproxy/native/glm.py`:

- читает `ZAI_TOKEN` из `secrets/tokens.json` и `user_id` из JWT;
- подписывает запрос HMAC-SHA256 с salt key;
- получает `captcha_verify_param`:
  - из `tokens.sqlite`, если задан `FP_GLM_TOKENS_DB` (значение из `.env`
    применяется к работающему gateway и имеет приоритет) или файл найден в
    `../GLM-Free-API/tokens.sqlite` / `secrets/glm-tokens.sqlite`;
  - иначе генерирует device-token и проходит Aliyun `InitCaptchaV3` →
    `VerifyCaptchaV3` через портированные скрипты в
    `src/freeproxy/native/glm_captcha/`;
- вызывает `POST https://chat.z.ai/api/v2/chat/completions`;
- разбирает edit-based SSE (`content`, `delta_content`, `edit_content`,
  `<details>` → reasoning);
- удаляет одноразовый chat после ответа.

### Captcha и risk-отказ

Для обоих путей (пул токенов и встроенный генератор) нужны креденшелы Aliyun
captcha SDK. Они читаются только из окружения и никогда не хранятся в
репозитории:

```dotenv
FP_ALIYUN_CAPTCHA_ACCESS_KEY=...
FP_ALIYUN_CAPTCHA_SECRET_KEY=...
```

Без них GLM отвечает `captcha_failed` с подсказкой, какие переменные задать.

Встроенный генератор воспроизводит reverse-engineered pipeline, но Aliyun может
ответить `F001` (risk policy) на синтетическую/чужую device-сессию. Это не
ошибка подписи. Надёжный путь — один раз собрать пул device-токенов на том же
аккаунте/IP:

```bash
# legacy-коллектор GLM-Free-API
cd ../GLM-Free-API
go run ./cmd/token-collector
```

Затем укажите путь к базе:

```dotenv
FP_GLM_TOKENS_DB=D:/proxies/GLM-Free-API/tokens.sqlite
```

Go-код в runtime не используется: Python читает таблицу `tokens` и удаляет
израсходованную строку.

### Модели

`glm-5.3-flash`, `glm-5.3`, `glm-5.2`, `GLM-5.1`, `GLM-5-Turbo`,
`GLM-5v-Turbo`, `glm-4.7`. Неизвестное имя передаётся upstream как есть.

## Qwen Chat

`src/freeproxy/native/qwen.py` держит один Playwright-page на процесс:

- страница `https://chat.qwen.ai/` загружается с cookies и `localStorage.token`
  из auth-store;
- `POST /api/v2/chats/new` и `POST /api/v2/chat/completions?chat_id=...`
  выполняются через `page.evaluate` — Qwen WAF (Baxia) не пропускает обычный
  HTTP и Node-streaming;
- `baxia_headers` из auth-store добавляются к запросу;
- SSE читается внутри страницы и передаётся в Python через exposed function,
  поэтому ответ стримится в реальном времени;
- запросы сериализуются; в конфиге `max_concurrency: 1`.

Токен Qwen короткоживущий. Провайдер читает актуальный `localStorage.token`
перед запросом; если сессия истекла — `401`, повторите
`uv run freeproxy-auth login qwen`.

Если Qwen показывает Access Verification внутри сервиса, запустите его с
`FP_QWEN_HEADLESS=false`, пройдите проверку в открывшемся окне и повторите
запрос; либо используйте профиль, авторизованный `freeproxy-auth`.

### Модели

Пока браузерная страница Qwen не запущена, `/v1/models` отдаёт статический
список ниже. После первого чата (страница «прогрета») провайдер читает список
моделей аккаунта из `GET /api/v2/models` и кэширует его на
`FP_MODEL_DISCOVERY_TTL_SECONDS` секунд (по умолчанию 300).
Неудачное обновление сохраняет предыдущий список и повторяется примерно через
30 секунд; если дискавери не удался ни разу, используется статический список.
Сам листинг браузер не запускает — `/v1/models` никогда не стартует Playwright.
Шлюз подхватывает обновлённый список фоновым discovery
(`FP_MODEL_DISCOVERY_TTL_SECONDS`), после чего `/v1/models` показывает только
модели аккаунта; статический список остаётся fallback'ом до успешного
discovery.

Модель, которой нет у аккаунта, возвращает `404 model_not_found` с именем
модели в сообщении: выберите модель из `GET /v1/models`.

Источник истины — список моделей аккаунта из discovery; он же наполняет
`/v1/models` после первого успешного обновления. Статический fallback (только
до discovery, для холодного старта): `qwen3.8-max`, `qwen3.7-max`,
`qwen3.7-plus`.

## DeepSeek Web

`src/freeproxy/native/deepseek.py` + `pow.py`:

- PoW `DeepSeekHashV1` через официальный WASM и `wasmtime`;
- `POST /api/v0/chat_session/create`, `POST /api/v0/chat_completion`;
- разбор `THINK`/`REASONING`, `RESPONSE`/`SEARCH`, patch-операций;
- tool-call prompt shim и защита от утечки сломанной разметки.

Модели: `deepseek-chat`, `deepseek-default`, `deepseek-v4-flash`,
`deepseek-v4.1-flash`, `deepseek-reasoner`, `deepseek-r1`,
`deepseek-chat-search` и search/expert-варианты.

## Конфигурация

```json
{
  "id": "glm",
  "base_url": "http://glm.native",
  "api_key": "",
  "auth_mode": "none",
  "auth_required": false,
  "native": "glm",
  "prefix": "glm",
  "models": ["glm-5.3-flash"],
  "default_model": "glm-5.3-flash",
  "models_path": "/v1/models",
  "chat_path": "/v1/chat/completions",
  "max_rpm": 6,
  "max_requests_per_day": 300,
  "max_concurrency": 1,
  "timeout_seconds": 600
}
```

- `native`: `glm`, `qwen` или `deepseek`.
- `base_url`: синтетический хост для внутреннего `httpx.ASGITransport`
  (`http://glm.native`, `http://qwen.native`, `http://deepseek.native`).
  Сетевых запросов на него нет.
- `models_path`/`chat_path`: `/v1/models` и `/v1/chat/completions`.
- `max_concurrency`: для Qwen и GLM рекомендуется `1` (браузер/captcha).
- `health_url` не нужен: health нативного провайдера проверяет token store.

## Как шлюз общается с провайдерами

Для нативного провайдера создаётся внутреннее FastAPI-приложение и
подключается через `httpx.ASGITransport`. Снаружи порт не открывается, но
сохраняются все преимущества `ProviderRuntime`: RPM, суточная квота,
concurrency, circuit breaker и fallback до первого байта ответа.

Клиентский `Authorization`/`X-API-Key` **никогда** не пересылается провайдеру.
Нативные адаптеры сами читают токены из `secrets/tokens.json`.

## Добавить свой OpenAI-совместимый провайдер

Можно добавить обычный HTTP-провайдер без `native`:

```json
{
  "id": "local",
  "name": "My local model",
  "base_url": "http://127.0.0.1:9000/v1",
  "api_key": "${LOCAL_UPSTREAM_KEY:-}",
  "auth_mode": "bearer",
  "prefix": "local",
  "models": ["my-coder"],
  "default_model": "my-coder",
  "health_url": "http://127.0.0.1:9000/health",
  "max_rpm": 10,
  "max_requests_per_day": 0,
  "max_concurrency": 2,
  "timeout_seconds": 300
}
```

`auth_mode`: `bearer`, `x-api-key` или `none`. Секреты храните через
`${ENV_VAR}` / `${ENV_VAR:-default}`, не в JSON.

## Проверка

```bash
uv run freeproxy-auth status
uv run python scripts/check_upstreams.py
curl http://127.0.0.1:8080/health
curl -H "Authorization: Bearer <ключ>" http://127.0.0.1:8080/v1/models
```

`check_upstreams.py` проверяет нативные провайдеры по token store, а внешние —
по health URL. Ключи и токены не печатаются.
