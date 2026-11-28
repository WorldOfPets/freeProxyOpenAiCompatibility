# FreeProxyAPI

Единый OpenAI-совместимый прокси к веб-чатам GLM, Qwen и DeepSeek. Все три
провайдера работают **внутри одного Python-процесса**:

- **DeepSeek Web** — PoW через `wasmtime`, сессии и SSE; порт
  [FreeDeepseekAPI](https://github.com/ForgetMeAI/FreeDeepseekAPI);
- **GLM / Z.AI** — прямой HTTP, подпись, Aliyun captcha и device-token pool;
  порт [GLM-Free-API](https://github.com/izaart95-jpg/GLM-Free-API);
- **Qwen Chat** — встроенный Playwright-сеанс, который выполняет запросы из
  реальной страницы (Qwen WAF не пропускает обычный HTTP); порт
  [FreeQwenApi](https://github.com/y13sint/FreeQwenApi).

Прокси написан на **Python 3.12 / FastAPI / httpx** и управляется через **uv**.
Он даёт один адрес `http://127.0.0.1:8080/v1`, один клиентский ключ, общий
список моделей, alias-маршрутизацию с fallback и многоуровневые rate limit'ы.

> **Статус порта.** Внешние bridge-процессы и порты `3001`, `3264`, `9655`
> больше не нужны. `uv run freeproxy` — единственный сервис. Qwen требует
> установленный Chrome/Chromium; GLM для captcha использует либо встроенный
> Python-генератор, либо `tokens.sqlite`, собранный один раз.
> Подробно: [docs/native-port.md](docs/native-port.md).

## Возможности

- OpenAI-compatible `POST /v1/chat/completions` (JSON и SSE streaming).
- Проброс `tools`, `tool_calls` и reasoning-полей без потери структуры.
  Vision-части пока не загружаются нативными провайдерами и заменяются на
  `[image]` (см. `docs/native-port.md`).
- Единый `GET /v1/models` с пространствами имён `glm/...`, `qwen/...`,
  `deepseek/...`.
- Alias'ы `auto`, `fast`, `coding`, `reasoning`, `vision` с fallback между
  провайдерами до первого байта ответа.
- Аутентификация клиентов по API-ключу (`Bearer` или `X-API-Key`).
- Лимиты: общий RPM, RPM на клиента, суточная квота, RPM/сутки и concurrency
  на каждого провайдера, очередь и circuit breaker.
- `429` с `Retry-After` и стандартными `X-RateLimit-*` заголовками.
- Health/readiness и админ-статистика без утечки ключей.
- Браузерная авторизация: `freeproxy-auth login deepseek|qwen|glm` сама
  забирает токены из localStorage/cookies в `secrets/tokens.json`.
- JSON-логи с `X-Request-ID`, без логирования промптов и секретов.
- Docker/Compose, smoke-скрипты, pytest-тесты, ruff.
- Готовый конфиг OpenCode V2 в `examples/opencode.jsonc`.

## Архитектура

```text
OpenCode / OpenAI SDK / curl
            │
            ▼
  FreeProxyAPI :8080/v1
  ├─ auth + client rate limits
  ├─ alias / namespace routing
  ├─ provider RPM + concurrency + breaker
  ├─ native/glm.py       → chat.z.ai  (+ Aliyun captcha)
  ├─ native/qwen.py      → chat.qwen.ai (Playwright page context)
  ├─ native/deepseek.py  → chat.deepseek.com (PoW + SSE)
  └─ SSE / JSON passthrough
```

Один клиентский запрос может породить несколько upstream-попыток (например,
`auto` сначала идёт в GLM, затем в Qwen). Это учитывается в лимитах
провайдеров, но клиентский лимит считает запрос один раз.

## Быстрый старт

### 1. Требования

- Python **3.12+**
- [uv](https://docs.astral.sh/uv/)
- Google Chrome или Chromium для Qwen (`uv run playwright install chromium`)
- Node.js и Go больше не нужны

### 2. Установите зависимости

```bash
uv sync
uv run playwright install chromium   # не нужно, если установлен Google Chrome
```

### 3. Авторизуйте три сервиса в браузере

```bash
uv run freeproxy-auth login all
```

CLI откроет обычный Chrome (без automation-флагов Playwright) для DeepSeek,
Qwen и GLM. Войдите в аккаунт, при необходимости пройдите слайдер Access
Verification вручную, отправьте тестовое сообщение и **вернитесь в терминал —
нажмите Enter** как подтверждение авторизации. Только после этого CLI забирает
токен, cookies и заголовки. Подробно: [docs/auth.md](docs/auth.md).

Проверить захват:

```bash
uv run freeproxy-auth status
```

### 4. Настройте и запустите прокси

```bash
cp .env.example .env
uv run python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Минимум, что нужно изменить в `.env`:

```dotenv
FP_API_KEYS=<ваш-длинный-ключ>
FP_ALLOW_ANONYMOUS=false
```

Для GLM captcha нужны креденшелы Aliyun captcha SDK:
`FP_ALIYUN_CAPTCHA_ACCESS_KEY` и `FP_ALIYUN_CAPTCHA_SECRET_KEY` в `.env`
(в репозиторий они не попадают). Если есть `tokens.sqlite` от старого
token-collector, дополнительно укажите `FP_GLM_TOKENS_DB=...`. Без credentials
GLM вернёт `captcha_failed`; при risk-отказе Aliyun соберите пул токенов.

Достаточно авторизовать **один** провайдер: `/readyz` вернёт `200`, если хотя бы
один провайдер здоров, а alias'ы с fallback обойдут недоступные. `FP_API_KEYS`
при этом — клиентский ключ для OpenCode/curl, а не токен провайдера.

```bash
uv run python scripts/check_upstreams.py
uv run freeproxy
```

`scripts/run_upstreams.py` оставлен только как совместимый алиас и просто
запускает `freeproxy`.

Проверка:

```bash
curl http://127.0.0.1:8080/health
curl -H "Authorization: Bearer <ваш-ключ>" http://127.0.0.1:8080/v1/models
uv run python scripts/smoke.py --api-key <ваш-ключ>
```

Swagger: `http://127.0.0.1:8080/docs`.

### 6. Подключите OpenCode

Скопируйте пример в корень проекта, откуда запускается OpenCode:

```bash
cp examples/opencode.jsonc ./opencode.jsonc
```

Задайте ключ в окружении **до** запуска OpenCode:

```powershell
# PowerShell
$env:FREEPROXY_API_KEY = "<ваш-ключ>"
opencode
```

```bash
# bash
export FREEPROXY_API_KEY="<ваш-ключ>"
opencode
```

Затем `/models` → `FreeProxyAPI` → `auto` или `coding`. Подробная инструкция:
[docs/opencode.md](docs/opencode.md).

## Модели и alias'ы

Пространства имён:

| Модель | Провайдер |
|---|---|
| `glm/glm-5.3-flash`, `glm/glm-4.7`, ... | GLM |
| `qwen/qwen3.8-max`, `qwen/qwen3.7-plus`, ... | Qwen |
| `deepseek/deepseek-chat`, `deepseek/deepseek-reasoner`, ... | DeepSeek |

Alias'ы из `config/providers.json`:

| Alias | Порядок fallback | Когда использовать |
|---|---|---|
| `auto` | GLM Flash → Qwen Max → DeepSeek Chat | обычный чат по умолчанию |
| `fast` | GLM Flash → Qwen Plus → DeepSeek Chat | короткие ответы |
| `coding` | Qwen 3.7 Plus → GLM 4.7 → DeepSeek Chat | код и tool calling |
| `reasoning` | DeepSeek Reasoner → GLM 5.3 → Qwen Max | сложные рассуждения |
| `vision` | Qwen 3.8 Max → GLM-5v | изображения |

Любую модель можно вызвать и напрямую: `"model": "qwen/qwen3.7-max"`.
Заголовок `X-FreeProxy-Provider: qwen` принудительно сужает alias до одного
провайдера.

Список моделей обновляется из upstream'ов в фоне (`/models`), TTL задаётся
`FP_MODEL_DISCOVERY_TTL_SECONDS`. После успешного discovery `/v1/models`
показывает только модели upstream'а; если он недоступен, используются
статически прописанные модели из конфига. Для Qwen список аккаунта появляется
только после первого чата, который запускает браузер: до этого `/v1/models`
отдаёт статический список.

## Лимиты

Слои ограничений:

1. **Клиент**: `FP_CLIENT_RPM` запросов/мин, `FP_CLIENT_MAX_CONCURRENCY`
   одновременных запросов и `FP_CLIENT_DAILY_REQUESTS` в сутки (UTC).
   Идентификатор — хэш API-ключа; в анонимном режиме — IP.
2. **Шлюз**: `FP_GLOBAL_RPM` и `FP_GLOBAL_MAX_CONCURRENCY`.
3. **Провайдер**: `max_rpm`, `max_requests_per_day`, `max_concurrency` в
   `config/providers.json`.
4. **Очередь**: запрос ждёт слот провайдера `queue_timeout_seconds`; не
   дождался — `503` + `Retry-After`.
5. **Circuit breaker**: после серии ошибок/`429` провайдер временно исключается,
   остальные alias-кандидаты продолжают работать.
6. **Размер запроса/ответа**: `FP_MAX_BODY_BYTES`, `FP_MAX_OUTPUT_TOKENS`,
   `FP_DEFAULT_MAX_TOKENS`.

При превышении клиентского лимита возвращается `429`:

```json
{
  "error": {
    "message": "Client rate limit exceeded (30 requests/minute)",
    "type": "rate_limit_error",
    "code": "rate_limit_exceeded",
    "param": null
  }
}
```

Заголовки ответа:

```text
X-RateLimit-Limit: 30
X-RateLimit-Remaining: 27
X-RateLimit-Reset: 1760000000
X-RateLimit-Daily-Limit: 1000
X-RateLimit-Daily-Remaining: 997
X-RateLimit-Global-Limit: 60
X-RateLimit-Global-Remaining: 58
Retry-After: 60          # только при 429/503
```

Значения по умолчанию в `config/providers.json` — консервативная стартовая
точка, а не официальные квоты провайдеров. Подробнее:
[docs/rate-limiting.md](docs/rate-limiting.md).

## API

Основные endpoints:

| Метод | Путь | Авторизация | Назначение |
|---|---|---|---|
| `GET` | `/health` | нет | liveness |
| `GET` | `/readyz` | нет | проверка upstream'ов |
| `GET` | `/v1/models` | да | список моделей |
| `POST` | `/v1/chat/completions` | да | чат, JSON или SSE |
| `GET` | `/admin/status` | admin | состояние провайдеров |
| `GET` | `/admin/stats` | admin | счётчики |
| `GET` | `/admin/limits` | admin | использование лимитов |
| `GET` | `/admin/auth` | admin | статус трёх захваченных токенов |
| `POST` | `/admin/auth/export` | admin | повторный экспорт auth-файлов |
| `POST` | `/admin/providers/{id}/reset` | admin | сброс breaker/лимитов |

Пример:

```bash
curl http://127.0.0.1:8080/v1/chat/completions \
  -H "Authorization: Bearer <ваш-ключ>" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "auto",
    "messages": [{"role": "user", "content": "Привет! Кто ты?"}],
    "max_tokens": 256
  }'
```

Streaming:

```bash
curl -N http://127.0.0.1:8080/v1/chat/completions \
  -H "Authorization: Bearer <ваш-ключ>" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "coding",
    "messages": [{"role": "user", "content": "Напиши Hello World на Python"}],
    "stream": true
  }'
```

OpenAI Python SDK:

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://127.0.0.1:8080/v1",
    api_key="<ваш-ключ>",
)

response = client.chat.completions.create(
    model="auto",
    messages=[{"role": "user", "content": "Привет!"}],
)
print(response.choices[0].message.content)
```

Полное описание полей, ошибок и заголовков: [docs/api.md](docs/api.md).

## Конфигурация

### Переменные окружения

Все настройки шлюза имеют префикс `FP_` и читаются из `.env`:

| Переменная | По умолчанию | Описание |
|---|---|---|
| `FP_HOST` | `127.0.0.1` | адрес прослушивания |
| `FP_PORT` | `8080` | порт |
| `FP_SERVER_LIMIT_CONCURRENCY` | `128` | лимит соединений uvicorn |
| `FP_API_KEYS` | пусто | клиентские ключи через запятую, минимум 16 символов |
| `FP_ALLOW_ANONYMOUS` | `false` | разрешить без ключа, только если `FP_API_KEYS` пуст |
| `FP_ADMIN_API_KEY` | пусто | ключ для `/admin/*`; без него админка только с loopback |
| `FP_CLIENT_RPM` | `30` | запросов/мин на клиента |
| `FP_CLIENT_MAX_CONCURRENCY` | `2` | одновременных запросов на клиента |
| `FP_CLIENT_DAILY_REQUESTS` | `1000` | запросов/сутки на клиента |
| `FP_GLOBAL_RPM` | `60` | запросов/мин на весь шлюз |
| `FP_GLOBAL_MAX_CONCURRENCY` | `8` | одновременных запросов |
| `FP_GLOBAL_QUEUE_TIMEOUT_SECONDS` | `10` | сколько ждать общий слот |
| `FP_MODEL_DISCOVERY_TTL_SECONDS` | `300` | период обновления `/models` |
| `FP_MAX_BODY_BYTES` | `8000000` | максимум тела запроса |
| `FP_BODY_READ_TIMEOUT_SECONDS` | `30` | таймаут чтения тела |
| `FP_MAX_UPSTREAM_BYTES` | `16000000` | максимум ответа upstream |
| `FP_MAX_OUTPUT_TOKENS` | `8192` | максимум `max_tokens` |
| `FP_DEFAULT_MAX_TOKENS` | `4096` | подставляется, если клиент не указал |
| `FP_STRICT_MODELS` | `false` | запрещать неизвестные bare-модели |
| `FP_PROVIDERS_FILE` | `config/providers.json` | реестр провайдеров |
| `FP_AUTH_STORE_PATH` | `secrets/tokens.json` | хранилище захваченных токенов (`FP_AUTH_STORE` — устаревший псевдоним, `FP_AUTH_STORE_PATH` важнее) |
| `FP_AUTH_BROWSER_DIR` | `secrets/browser` | профили Playwright |
| `FP_AUTH_BROWSER_CHANNEL` | `chrome` | `chrome` или `chromium` |
| `FP_AUTH_CHROME_PATH` | пусто | путь к Chrome/Edge для auth |
| `FP_AUTH_TIMEOUT_SECONDS` | `600` | таймаут входа |
| `FP_QWEN_HEADLESS` | `true` | запускать внутренний браузер Qwen без окна |
| `FP_GLM_TOKENS_DB` | пусто | путь к `tokens.sqlite`; значение из `.env` применяется к gateway и важнее путей по умолчанию (`../GLM-Free-API/tokens.sqlite`, `secrets/glm-tokens.sqlite`) |
| `FP_ALIYUN_CAPTCHA_ACCESS_KEY` | пусто | Aliyun AccessKey ID для встроенного GLM captcha |
| `FP_ALIYUN_CAPTCHA_SECRET_KEY` | пусто | Aliyun AccessKey Secret для встроенного GLM captcha |
| `FP_DEEPSEEK_REPO` | `../FreeDeepseekAPI` | только для `freeproxy-auth export` |
| `FP_QWEN_REPO` | `../FreeQwenApi` | только для `freeproxy-auth export` |
| `FP_GLM_REPO` | `../GLM-Free-API` | только для `freeproxy-auth export` |
| `FP_LOG_LEVEL` | `INFO` | уровень логов |
| `FP_LOG_FORMAT` | `json` | `json` или `text` |
| `FP_CORS_ORIGINS` | пусто | origins через запятую |

Внешних upstream-ключей больше нет: все провайдеры работают в процессе.

### `config/providers.json`

Для каждого провайдера задаются:

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

`auth_mode`: `bearer`, `x-api-key` или `none`. `auth_required` (по умолчанию
`true`) не даёт запуститься с пустым ключом; для нативных провайдеров
используется `false` и `auth_mode: none`. Поле `"native": "glm" | "qwen" |
"deepseek"` включает встроенный провайдер; его `base_url` — только
синтетический хост для внутреннего ASGI-клиента. `health_url` для нативных
провайдеров не нужен: health проверяет token store. Алиасы находятся в конце
файла в `aliases`.

Ключи в JSON можно не хранить: используйте `${ENV_VAR}` или
`${ENV_VAR:-default}`. При отсутствии переменной без default старт завершится
понятной ошибкой.

## Docker

```bash
cp .env.example .env
# отредактируйте .env
uv run freeproxy-auth login all   # токены попадут в secrets/tokens.json
docker compose up --build -d
docker compose logs -f gateway
```

Compose монтирует `./secrets` в контейнер: все провайдеры читают токены оттуда,
а GLM удаляет израсходованные device-токены из `tokens.sqlite`. Образ
собирается с Chromium для нативного Qwen. Если используете внешний
`tokens.sqlite` для GLM, положите его в `secrets/` и задайте
`FP_GLM_TOKENS_DB`. Прокси слушает только `127.0.0.1:8080`; не меняйте это без
reverse proxy и TLS/аутентификации.

## Разработка

```bash
uv sync
uv run ruff check .
uv run ruff format --check .
uv run pytest -q
uv run pytest --cov=freeproxy --cov-report=term-missing
```

Структура:

```text
src/freeproxy/
├── main.py              # create_app, lifespan, exception handlers
├── config.py            # pydantic-settings + providers.json
├── rate_limit.py        # sliding window, daily quota, client limiter
├── middleware.py        # request id, security headers, access log
├── metrics.py
├── providers/           # HTTP-адаптер и runtime провайдера
├── native/              # GLM/Qwen/DeepSeek in-process providers
├── services/            # routing + proxy/streaming
├── auth/                # Playwright capture, token store, native export
└── api/                 # chat, models, health, admin
tests/                   # pytest + mock upstream transport
scripts/                 # smoke, check_upstreams, run_upstreams
config/                  # реестр провайдеров
examples/opencode.jsonc  # готовый provider для OpenCode V2
```

## Участие в разработке

Как настроить окружение, прогнать проверки и оформить PR, описано в
[CONTRIBUTING.md](CONTRIBUTING.md). Об уязвимостях сообщайте приватно по
[SECURITY.md](SECURITY.md), а не через публичные issues.

## Оригинальные проекты

FreeProxyAPI вырос из трёх прокси-серверов для веб-чатов. Их протоколы
портированы в `src/freeproxy/native/`, но сами проекты остаются полезным
референсом и обновляются при изменениях веб-API:

| Провайдер | Оригинальный прокси | Лицензия |
|---|---|---|
| DeepSeek Web | [ForgetMeAI/FreeDeepseekAPI](https://github.com/ForgetMeAI/FreeDeepseekAPI) | MIT |
| Qwen Chat | [y13sint/FreeQwenApi](https://github.com/y13sint/FreeQwenApi) | не указана |
| GLM / Z.AI | [izaart95-jpg/GLM-Free-API](https://github.com/izaart95-jpg/GLM-Free-API) | MIT |

Запускать их вместе с FreeProxyAPI больше не нужно: все три провайдера
работают внутри одного процесса. Код GLM captcha портирован из
[GLM-Free-API](https://github.com/izaart95-jpg/GLM-Free-API) с сохранением
MIT-атрибуции в
[`src/freeproxy/native/glm_captcha/LICENSE.glm-free-api`](src/freeproxy/native/glm_captcha/LICENSE.glm-free-api).

## GitHub Wiki

Документация из `docs/` автоматически публикуется в
[GitHub Wiki](https://github.com/WorldOfPets/freeProxyOpenAiCompatibility/wiki)
workflow-ом [`.github/workflows/wiki.yml`](.github/workflows/wiki.yml) при
пуше в `master` (и вручную через `workflow_dispatch`).

- Источник истины — `docs/*.md`; страницы вики генерируются, не редактируйте
  их вручную.
- `Home.md` и `_Sidebar.md` собираются автоматически.
- Ссылки между документами превращаются в ссылки вики, ссылки на файлы
  репозитория — в абсолютные GitHub URL.
- Удалённые из `docs/` страницы удаляются из вики; страницы, созданные
  вручную, не трогаются.
- Нужно один раз включить **Settings → Actions → General → Workflow
  permissions → Read and write permissions**, иначе `GITHUB_TOKEN` не сможет
  запушить вики. Первую страницу вики GitHub создаёт только через веб-интерфейс
  (она уже создана).

Локальная проверка:

```bash
git clone https://github.com/WorldOfPets/freeProxyOpenAiCompatibility.wiki.git /tmp/wiki
python scripts/sync_wiki.py --source docs --dest /tmp/wiki \
  --repo-url https://github.com/WorldOfPets/freeProxyOpenAiCompatibility
```

## ECC (skills / agents / plugins)

Проект использует подход и материалы
[affaan-m/ECC](https://github.com/affaan-m/ecc) (MIT):

- В `.opencode/skills/` лежат адаптированные ECC-навыки:
  `fastapi-patterns`, `python-patterns`, `api-design`, `security-review`,
  `error-handling`, `verification-loop`.
- В `.opencode/agents/` лежат read-only субагенты ECC:
  `fastapi-reviewer`, `python-reviewer`, `security-reviewer`. Их frontmatter
  переведён на формат OpenCode V2, модель наследуется от родителя, edit/shell
  ограничены.
- `AGENTS.md` содержит проектные правила для агентов.
- Полный ECC OpenCode plugin не вендорится: он тянет Node/TypeScript runtime и
  hooks, которые не нужны Python-сервису. Если нужен полный ECC, установите его
  по инструкции upstream-репозитория и не дублируйте skills.

Атрибуция и лицензия: [.opencode/README.md](.opencode/README.md),
`.opencode/ECC-LICENSE`.

## Ограничения и безопасность

- Это неофициальный инструмент. Использование веб-чатов через прокси может
  нарушать ToS провайдеров; аккаунты могут получить ограничения. Решение для
  локальных экспериментов, а не для production.
- Upstream'ы должны быть авторизованы вручную; шлюз не открывает браузер и не
  обходит captcha.
- GLM, Qwen и DeepSeek могут менять внутренние API, лимиты и названия моделей.
  При поломке сначала проверяйте upstream, затем `scripts/check_upstreams.py`.
- Не публикуйте upstream-порты в интернет. Держите их на loopback.
- Всегда задавайте `FP_API_KEYS`, если шлюз доступен не только с localhost.
  `FP_ALLOW_ANONYMOUS` действует только при пустом списке ключей.
- За reverse proxy задавайте `FP_ADMIN_API_KEY`: без него админка разрешена
  только для прямого loopback-запроса с loopback `Host`; запросы с
  `X-Forwarded-For` блокируются, а POST дополнительно требует
  `X-FreeProxy-Admin: true` (защита от CSRF).
- Не логируйте `.env`, `secrets/tokens.json`, `deepseek-auth.json`, `session/`,
  `tokens.sqlite` и browser-профили. `secrets/` исключён через `.gitignore`.
- `X-Forwarded-For` не используется для идентификации клиента: `freeproxy` и
  Docker-образ запускают uvicorn с `--no-proxy-headers`. За reverse proxy
  анонимный режим будет считать всех одним клиентом. Используйте API-ключи.
  Если запускаете uvicorn вручную и доверяете прокси, включайте proxy headers
  явно и ограничивайте `--forwarded-allow-ips`.

## Документация

- [docs/auth.md](docs/auth.md) — вход в браузере, захват трёх токенов и экспорт.
- [docs/opencode.md](docs/opencode.md) — подключение к OpenCode V2.
- [docs/providers.md](docs/providers.md) — запуск и настройка трёх upstream'ов.
- [docs/rate-limiting.md](docs/rate-limiting.md) — лимиты, заголовки, тюнинг.
- [docs/api.md](docs/api.md) — endpoints, ошибки, примеры.
- [docs/architecture.md](docs/architecture.md) — устройство и поток запроса.
- [docs/troubleshooting.md](docs/troubleshooting.md) — диагностика.

## Лицензия

MIT. Сторонние проекты и ECC-материалы сохраняют свои лицензии; см. ссылки
выше и `.opencode/ECC-LICENSE`.
