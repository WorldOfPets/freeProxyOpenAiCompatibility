# API

Base URL: `http://127.0.0.1:8080`

Все `/v1/*` и `/admin/*` требуют авторизации. `/health` и `/readyz` — публичные.
Swagger UI: `/docs`, OpenAPI JSON: `/openapi.json`.

## Авторизация

Клиент передаёт один из ключей `FP_API_KEYS` (каждый ключ — минимум 16
символов):

```http
Authorization: Bearer <key>
```

или

```http
X-API-Key: <key>
```

Если `FP_API_KEYS` пуст и `FP_ALLOW_ANONYMOUS=true`, ключ не проверяется.
Если ключи заданы, неверный/отсутствующий ключ даёт `401`:

```json
{
  "error": {
    "message": "Invalid or missing API key",
    "type": "authentication_error",
    "code": "invalid_api_key",
    "param": null
  }
}
```

Админка (`/admin/*`) дополнительно требует `X-Admin-Key` (или Bearer) при
заданном `FP_ADMIN_API_KEY`. Без admin-ключа она доступна только с loopback;
запросы с `Forwarded`/`X-Forwarded-For` блокируются, даже если reverse proxy
сам пришёл с loopback. За reverse proxy всегда задавайте `FP_ADMIN_API_KEY`.

## `POST /v1/chat/completions`

OpenAI-совместимый chat completions. Поддерживаются JSON и SSE (`stream: true`).

Минимальный запрос:

```json
{
  "model": "auto",
  "messages": [
    {"role": "user", "content": "Привет!"}
  ]
}
```

Поля:

| Поле | Тип | Описание |
|---|---|---|
| `model` | string | alias (`auto`, `coding`, ...) или `provider/model`; по умолчанию `auto` |
| `messages` | array | обязательно, непустой массив OpenAI-сообщений |
| `stream` | bool | `true` → `text/event-stream` |
| `max_tokens` / `max_completion_tokens` | int | не больше `FP_MAX_OUTPUT_TOKENS`; если не указано, подставляется `FP_DEFAULT_MAX_TOKENS` |
| `tools`, `tool_choice` | array/object | принимаются; нативные провайдеры используют prompt-shim |
| `temperature`, `top_p`, `stop`, ... | любые | пробрасываются без изменения |

Шлюз не ограничивает набор полей: неизвестные поля уходят в upstream. Он
валидирует только структуру сообщений, `model`, `stream` и лимиты выхода.

### Non-streaming

```bash
curl http://127.0.0.1:8080/v1/chat/completions \
  -H "Authorization: Bearer <key>" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "coding",
    "messages": [{"role": "user", "content": "Напиши fizzbuzz на Python"}],
    "max_tokens": 512
  }'
```

Успешный ответ — обычный OpenAI JSON. Шлюз не переупаковывает `choices`,
`tool_calls`, `reasoning_content` и `usage`.

### Streaming

```bash
curl -N http://127.0.0.1:8080/v1/chat/completions \
  -H "Authorization: Bearer <key>" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "auto",
    "messages": [{"role": "user", "content": "Расскажи про FastAPI"}],
    "stream": true
  }'
```

Ответ:

```text
data: {"id":"...","object":"chat.completion.chunk",...}

data: {"id":"...","object":"chat.completion.chunk",...}

data: [DONE]
```

Шлюз передаёт SSE-байты без пересборки. Если upstream вернул ошибку до первого
байта, клиент получает обычный JSON с ошибкой и соответствующим статусом.
После начала стрима fallback на другого провайдера не делается.

Если upstream оборвал уже начатый стрим, шлюз отправляет терминальное событие
и закрывает поток, чтобы клиент не принял обрезанный ответ за успешный:

```text
event: error
data: {"error":{"message":"Upstream stream was interrupted","type":"upstream_error","code":"stream_interrupted","param":null,"provider":"glm"}}
```

### Принудительный провайдер

```http
X-FreeProxy-Provider: qwen
```

Сужает alias до указанного провайдера. Если alias не содержит этого
провайдера — `404 model_not_found`. Если модель уже namespaced
(`glm/...`) и заголовок указывает другого провайдера — `400`.

### Сессия

Шлюз по-прежнему вычисляет стабильный `x-agent-session` для совместимости:

- явный `X-FreeProxy-Session` или `x-agent-session` клиента имеет приоритет, но
  хэшируется вместе с identity клиента;
- иначе стабильный `fp-<hash>` строится из клиента, запрошенной модели и первого
  сообщения пользователя.

Нативные провайдеры создают stateless-сессию на каждый запрос и этот заголовок
игнорируют; история передаётся через `messages`.

### Заголовки ответа

```text
X-Request-ID: 5f0c...
X-FreeProxy-Provider: glm
X-FreeProxy-Model: glm-4.7
X-FreeProxy-Route: glm/glm-4.7
X-FreeProxy-Requested-Model: auto
X-FreeProxy-Fallback: false
X-RateLimit-Limit: 30
X-RateLimit-Remaining: 29
Retry-After: 60          # 429/503
```

В streaming дополнительно:

```text
Cache-Control: no-cache
X-Accel-Buffering: no
```

`X-FreeProxy-Fallback: true` означает, что первый кандидат alias'а не сработал
и ответ пришёл от следующего.

## `GET /v1/models`

```bash
curl -H "Authorization: Bearer <key>" http://127.0.0.1:8080/v1/models
```

```json
{
  "object": "list",
  "data": [
    {
      "id": "auto",
      "object": "model",
      "created": 0,
      "owned_by": "freeproxy",
      "provider": "auto",
      "routing": ["glm/glm-5.3-flash", "qwen/qwen3.7-max", "deepseek/deepseek-chat"]
    },
    {
      "id": "glm/glm-4.7",
      "object": "model",
      "created": 0,
      "owned_by": "glm",
      "provider": "glm",
      "root": "glm-4.7"
    }
  ]
}
```

`GET /models` — недокументированный alias без `/v1` для клиентов, которые
добавляют `/models` к base URL.

## Операционные endpoints

### `GET /health`

```json
{
  "status": "ok",
  "service": "FreeProxyAPI",
  "version": "0.1.0",
  "environment": "local",
  "uptime_seconds": 42.1,
  "providers": 3
}
```

### `GET /readyz`

Проверяет health URL всех включённых провайдеров. `200`, если хотя бы один
готов; иначе `503`.

```json
{
  "status": "ready",
  "providers": {
    "glm": {"ok": true},
    "qwen": {"ok": true},
    "deepseek": {"ok": false}
  }
}
```

Публичный `/readyz` не раскрывает коды/латентность/режим upstream'ов.
Детали доступны в `/admin/status`.

## Админка

Требует admin-ключ/loopback.

| Метод | Путь | Что возвращает |
|---|---|---|
| `GET` | `/admin/status` | конфиг без секретов, лимиты, состояние провайдеров |
| `GET` | `/admin/providers` | health, breaker, RPM, concurrency |
| `GET` | `/admin/stats` | счётчики запросов/ошибок/токенов |
| `GET` | `/admin/limits` | использование клиентских лимитов (хэши ключей) |
| `GET` | `/admin/auth` | статус захваченных токенов без самих токенов |
| `POST` | `/admin/auth/export` | повторный экспорт native-файлов |
| `POST` | `/admin/providers/{id}/reset` | сброс breaker/RPM/суточной квоты провайдера |
| `POST` | `/admin/limits/reset` | сброс клиентских лимитов |

Без `FP_ADMIN_API_KEY` изменяющие запросы (POST) дополнительно требуют
`X-FreeProxy-Admin: true`, чтобы браузер не мог отправить их простым
cross-site POST с loopback. GET-запросы админки доступны только с loopback и
только при loopback `Host`.

Пример:

```bash
curl http://127.0.0.1:8080/admin/status \
  -H "X-Admin-Key: <admin-key>"
```

## Ошибки

Формат:

```json
{
  "error": {
    "message": "Human readable message",
    "type": "invalid_request_error",
    "code": "invalid_request_error",
    "param": null,
    "provider": "glm"
  }
}
```

| HTTP | `code` | Когда |
|---:|---|---|
| 400 | `invalid_request_error` | пустые `messages`, неверный JSON, `Content-Type` не JSON, конфликт provider-заголовка |
| 400 | `max_tokens_too_large` | `max_tokens` выше лимита шлюза |
| 401 | `invalid_api_key` | ключ отсутствует/неверен |
| 403 | `admin_forbidden` | нет доступа к `/admin/*` |
| 404 | `model_not_found` | неизвестный namespace/model |
| 404 | `provider_not_found` | неизвестный provider в admin reset |
| 413 | `payload_too_large` | тело больше `FP_MAX_BODY_BYTES` |
| 422 | `validation_error` | ошибка FastAPI-валидации служебных endpoint'ов |
| 429 | `rate_limit_exceeded` | клиентский/провайдерский лимит |
| 502 | `upstream_error` | upstream вернул не-JSON/сломанный ответ |
| 503 | `provider_unavailable` | все кандидаты недоступны/в cooldown |
| 503 | `provider_busy` | не дождались слота concurrency |
| 503 | `gateway_busy` | не дождались общего слота |
| 500 | `internal_error` | непредвиденная ошибка (детали только в логе) |

При `4xx` от upstream (кроме 401/403/408/429) шлюз возвращает статус и тело
ошибки upstream'а, чтобы клиент видел причину. При `401/403` upstream'а шлюз
отдаёт `503`: клиент не может исправить авторизацию прокси.

## OpenAI SDK

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://127.0.0.1:8080/v1",
    api_key="<key>",
)

response = client.chat.completions.create(
    model="auto",
    messages=[{"role": "user", "content": "Привет!"}],
    max_tokens=256,
)
print(response.choices[0].message.content)

for chunk in client.chat.completions.create(
    model="coding",
    messages=[{"role": "user", "content": "Напиши тест"}],
    stream=True,
):
    delta = chunk.choices[0].delta.content
    if delta:
        print(delta, end="")
```

## Не поддерживается

- Anthropic Messages `/v1/messages` и OpenAI Responses `/v1/responses`
  (используйте `/v1/chat/completions`).
- Qwen image/video endpoints — они остаются на Qwen-прокси.
- Эмбеддинги, аудио, батчи.
