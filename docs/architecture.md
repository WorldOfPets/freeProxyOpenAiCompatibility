# Архитектура

## Почему один процесс

Все три протокола портированы в Python и работают внутри одного FastAPI-сервиса:

- GLM: подпись, Aliyun captcha/device-token pool, WAF-ошибки, SSE;
- Qwen: встроенный Playwright-page для WAF-защищённых запросов, SSE;
- DeepSeek: PoW через `wasmtime`, сессии, SSE, tool shim.

Внешние bridge-процессы, Node.js и Go в runtime не используются. Стабильный
контракт (OpenAI Chat Completions) остаётся прежним, а хрупкая часть изолирована
в `src/freeproxy/native/`.

## Поток запроса

```text
POST /v1/chat/completions
  │
  ├─ RequestContextMiddleware: X-Request-ID, security headers, access log
  ├─ authenticate: Bearer/X-API-Key → client_id (hash)
  ├─ enforce_client_limits: global RPM → daily → client RPM
  ├─ read_limited_body + JSON validation
  ├─ ModelRouter.resolve(model, forced_provider)
  │    ├─ alias → [glm/..., qwen/..., deepseek/...]
  │    ├─ namespace → provider + model
  │    └─ bare → known model / default provider
  ├─ global concurrency slot
  └─ for each candidate:
       ├─ acquire provider slot (breaker, daily, RPM, semaphore)
       ├─ build OpenAI request (model, max_tokens, session)
       ├─ send to native ASGI app (or external HTTP provider)
       ├─ 2xx + stream → ProxyStreamingResponse
       ├─ 2xx + JSON   → JSONResponse
       ├─ 4xx client   → relay error
       └─ 429/5xx/net  → cooldown + next candidate
```

## Компоненты

| Модуль | Ответственность |
|---|---|
| `config.py` | pydantic-settings, `${ENV}`-подстановка, валидация провайдеров |
| `api/dependencies.py` | аутентификация, admin-доступ, клиентские лимиты |
| `rate_limit.py` | sliding window, UTC-квоты, композитный клиентский лимитер |
| `services/routing.py` | alias'ы, namespace'ы, bare-модели, список моделей |
| `services/proxy.py` | валидация, retry/fallback, SSE, слоты, ошибки |
| `providers/base.py` | breaker, RPM/квота/concurrency, health, discovery |
| `providers/http.py` | auth-заголовки, `/models`, `/health`, URL'ы |
| `native/deepseek.py` | DeepSeek: сессия, completion, SSE, tools, OpenAI JSON/SSE |
| `native/pow.py` | DeepSeek PoW: WASM через `wasmtime`, `X-DS-PoW-Response` |
| `native/qwen.py` | Qwen: Playwright page context, chat/SSE, tools |
| `native/glm.py` | GLM: подпись, captcha, completion, edit-SSE, tools |
| `native/glm_captcha/` | порт Aliyun device-token/captcha (MIT) |
| `middleware.py` | request ID, security headers, structured access log |
| `metrics.py` | счётчики попыток/ошибок/токенов |
| `api/admin.py` | состояние, статистика, сброс, статус auth |
| `auth/` | Playwright-захват токенов, token store, native export |

### Авторизация upstream'ов

`freeproxy-auth` — отдельный CLI, не часть горячего пути запросов:

1. Playwright открывает persistent-профиль провайдера.
2. Пользователь входит и отправляет тестовое сообщение.
3. CLI опрашивает localStorage/cookies/заголовки, пока не появится валидный токен.
4. Токен пишется в `secrets/tokens.json` (0600).
5. Нативные провайдеры читают store напрямую: DeepSeek — PoW/сессии, Qwen —
   cookies/Baxia для встроенного browser-page, GLM — JWT и HTTP-подпись.
6. `scripts/run_upstreams.py` оставлен как совместимый алиас и просто запускает
   `freeproxy`; отдельных bridge-процессов больше нет.

Сам шлюз не открывает браузер и не хранит пароли; он только читает статус
через `/admin/auth`.

## Слоты и streaming

Слот провайдера (`ProviderSlot`) удерживается на всё время запроса, включая
стрим. Это не даёт одному долгому SSE-ответу обойти `max_concurrency`.

Для streaming используется `ProxyStreamingResponse`:

- upstream-соединение и slot передаются в объект ответа;
- `__call__` в `finally` закрывает upstream, освобождает slot и общий слот;
- это работает и при нормальном завершении, и при отмене клиентом.

Fallback возможен только **до** отправки первого байта. Если стрим уже начался,
ошибка upstream'а обрывает ответ; повторять запрос нельзя без дублирования
контента.

## Лимиты и cooldown

- `SlidingWindowLimiter` хранит deque монотонных timestamp'ов на ключ.
  Периодически чистит пустые ключи.
- `DailyQuota` хранит `(UTC-дата, счётчик)`.
- `ClientLimiter` сначала делает `preview` всех слоёв, затем `consume`; между
  ними нет `await`, поэтому нет гонки.
- `ProviderRuntime.acquire()` проверяет breaker и делает быстрый preview
  RPM/квоты, затем ждёт semaphore с таймаутом и только после слота повторно
  проверяет и списывает лимиты. Поэтому запрос, не дождавшийся concurrency-слота,
  не сжигает квоту upstream'а.
- `CircuitBreaker`:
  - `429` → cooldown по `Retry-After` (или 30 с);
  - `401/403` → 60 с (нет смысла долбить сломанную авторизацию);
  - `5xx`/сеть → после 3 ошибок 15 с, затем экспоненциально до 300 с;
  - успех сбрасывает.

## Конфигурация

`config/providers.json` — единственный реестр провайдеров. Он загружается один
раз при старте. Чтобы применить изменения, перезапустите шлюз. Провайдер с
полем `"native": "deepseek"` обслуживается внутри процесса; для него
`base_url` — синтетический хост (`http://deepseek.native`), а не сетевой адрес.

`Settings` читает `.env` и переменные `FP_*`. Секреты upstream'ов не хранятся в
JSON: используются `${ENV_VAR}` / `${ENV_VAR:-default}`. При отсутствии
переменной без default старт падает с ошибкой, но без вывода значения.

## Модели

- Статические `models` — fallback и гарантия, что модель появится в UI даже при
  недоступном upstream.
- Discovery в фоне дергает `/models` и объединяет результаты; TTL —
  `FP_MODEL_DISCOVERY_TTL_SECONDS`.
- `/v1/models` показывает alias'ы и namespaced ID. Bare-модели принимаются, но
  не перечисляются, чтобы не было коллизий.
- `FP_STRICT_MODELS=true` запрещает неизвестные bare-модели (полезно, чтобы
  опечатка не уходила в upstream).

## Деплой

- По умолчанию bind `127.0.0.1`.
- Все провайдеры работают внутри процесса; портов `3001/3264/9655` больше нет.
- Для Docker используйте `config/providers.docker.json` и mount `./secrets`
  (writable: GLM consumes device tokens); образ содержит Chromium для Qwen.
  См. `docker-compose.yml`.
- Для удалённого доступа ставьте reverse proxy с TLS и отдельной
  аутентификацией; не открывайте порт шлюза напрямую.
- Один процесс uvicorn достаточен для локального использования. Не запускайте
  несколько воркеров с in-memory лимитами: каждый воркер будет считать лимиты
  отдельно. Если нужен multi-worker, вынесите счётчики в Redis (точка
  расширения — `ClientLimiter`/`SlidingWindowLimiter`).

## Точки расширения

- Новый OpenAI-совместимый upstream — добавить объект в `providers`.
- Другой алгоритм лимитов — заменить `ClientLimiter`/`ProviderRuntime`.
- Redis/БД для квот — реализовать интерфейс `preview/consume`.
- Anthropic/Responses — добавить отдельный роутер и конвертер, не трогая
  текущий `/v1/chat/completions`.
- Метрики Prometheus — добавить endpoint, читающий `Metrics.snapshot()`.
