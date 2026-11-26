# Troubleshooting

## Сначала три проверки

```bash
curl http://127.0.0.1:8080/health
curl http://127.0.0.1:8080/readyz
uv run python scripts/check_upstreams.py
```

- `/health` — жив ли шлюз.
- `/readyz` — проверяет token store нативных провайдеров.
- `check_upstreams.py` — показывает статус каждого провайдера без ключей.

Дальше:

```bash
curl -H "Authorization: Bearer <key>" http://127.0.0.1:8080/v1/models
curl -H "X-Admin-Key: <admin-key>" http://127.0.0.1:8080/admin/status
```

## Шлюз не стартует

### `Configuration error: FP_API_KEYS is empty ...`

Это **клиентский** ключ, который OpenCode/curl отправляет в `Authorization`, а не
токен провайдера. Без него сервис не стартует при
`FP_ALLOW_ANONYMOUS=false`. Для локальной проверки можно либо задать
`FP_API_KEYS=<длинный-ключ>`, либо поставить `FP_ALLOW_ANONYMOUS=true`.
Наличие одного, двух или трёх провайдерских токенов на это не влияет: сервис
работает и с одним провайдером, `/readyz` станет `200`, если хотя бы один
провайдер здоров.

### `Missing environment variables referenced by providers config`

В `providers.json` есть `${VAR}` без default, а переменная не задана. Добавьте
её в `.env` (например `SOME_UPSTREAM_KEY=`) или используйте
`${VAR:-default}`.

### `Providers config not found`

`FP_PROVIDERS_FILE` указывает не туда. Из корня проекта:

```bash
FP_PROVIDERS_FILE=config/providers.json uv run freeproxy
```

### `Invalid JSON in ...`

В `config/providers.json` синтаксическая ошибка. Проверьте запятые/кавычки;
сообщение содержит строку и колонку.

### Порт занят

```powershell
Get-NetTCPConnection -LocalPort 8080
```

Или задайте `FP_PORT=8081` и обновите `settings.baseURL` в OpenCode.

## Токены провайдеров

Сначала:

```bash
uv run freeproxy-auth status
```

Если провайдер `not configured`/`EXPIRED`, повторите
`uv run freeproxy-auth login <provider>`; перезапуск `freeproxy` обычно не нужен,
потому что нативные провайдеры читают store при каждом запросе. Полная
инструкция — [auth.md](auth.md).

## 401 / 403

### `401 invalid_api_key`

- Ключ в `Authorization: Bearer ...` не совпадает ни с одним из `FP_API_KEYS`.
- В ключе пробелы/кавычки. В PowerShell кавычки вокруг значения не нужны:
  `curl.exe -H "Authorization: Bearer $env:FP_KEY" ...`.
- OpenCode: `FREEPROXY_API_KEY` не задан в окружении процесса OpenCode.
  Перезапустите OpenCode после установки переменной.

### `403 admin_forbidden`

`FP_ADMIN_API_KEY` задан, а `X-Admin-Key` неверный. Либо ключ не задан, но
запрос пришёл не с loopback. Для локальной проверки используйте
`http://127.0.0.1:8080`, а не имя машины/внешний IP.

## 429

Смотрите `Retry-After` и `X-RateLimit-Scope`.

```bash
curl -i -H "Authorization: Bearer <key>" \
  -H "Content-Type: application/json" \
  -d '{"model":"auto","messages":[{"role":"user","content":"test"}]}' \
  http://127.0.0.1:8080/v1/chat/completions
```

- `client_rpm` — подождите или поднимите `FP_CLIENT_RPM`.
- `client_daily` — сброс в 00:00 UTC; поднимите
  `FP_CLIENT_DAILY_REQUESTS`.
- `gateway_rpm` — конкурирующие клиенты; поднимите `FP_GLOBAL_RPM` или
  уменьшите параллелизм.
- `provider:*` — upstream-квота/`max_rpm`; подождите, выберите другую модель
  или уменьшите `max_rpm`, чтобы не злить upstream.
- `providers` — все кандидаты alias'а упёрлись в лимиты. Выберите конкретную
  модель другого провайдера.

## 503 / 502

### `provider_unavailable`

Upstream не отвечает, в cooldown или health не `ok`.

```bash
curl http://127.0.0.1:8080/readyz
uv run freeproxy-auth status
```

- GLM: `captcha_failed` — нет `FP_ALIYUN_CAPTCHA_ACCESS_KEY`/
  `FP_ALIYUN_CAPTCHA_SECRET_KEY`, Aliyun risk/flow rejection или нет device-токенов.
  Задайте credentials, затем при необходимости `FP_GLM_TOKENS_DB` с `tokens.sqlite`
  или повторите вход.
- Qwen: `upstream_auth_error` — токен/сессия истекли; `uv run freeproxy-auth login qwen`.
  Если браузер не стартует, установите Chrome или `uv run playwright install chromium`.
- DeepSeek: `provider_not_configured` — нет токена в `secrets/tokens.json`;
  выполните `uv run freeproxy-auth login deepseek`.

### `provider_busy` / `gateway_busy`

Слишком много одновременных запросов. Уменьшите параллелизм агентов или
поднимите `max_concurrency`/`FP_GLOBAL_MAX_CONCURRENCY` (осторожно: upstream
может ответить `429`).

### `502 upstream_error`

Upstream вернул `200`, но не JSON (или сломанный JSON). Смотрите лог
провайдера: часто это HTML captcha/WAF или страница логина.

### Upstream вернул 401/403

Шлюз отдаёт `503`, потому что клиент не может исправить авторизацию прокси.
Проверьте:

- GLM: `ZAI_TOKEN` читается из `secrets/tokens.json`; при `401` повторите вход.
- DeepSeek: веб-токен истёк — `uv run freeproxy-auth login deepseek`.
- Qwen: токен короткоживущий — повторите `uv run freeproxy-auth login qwen`.

## OpenCode

### Провайдер не появился

- `opencode.jsonc` лежит в проекте или `~/.config/opencode/`.
- Используется V2-поле `providers`, а не старое `provider`.
- `package` — `@opencode/ai/providers/openai-compatible`.
- `settings.baseURL` заканчивается на `/v1`.
- Перезапустите OpenCode после изменения env; конфиг сам перечитывается, env —
  нет.

### `Model unavailable: freeproxy/auto`

В `models` нет ключа `auto` или ошибка в `modelID`. Скопируйте
[examples/opencode.jsonc](../examples/opencode.jsonc) целиком.

### `Cannot initialize provider ... baseURL`

Пустая/битая переменная в `baseURL`, либо OpenCode не видит `FREEPROXY_API_KEY`.
Проверьте:

```powershell
$env:FREEPROXY_API_KEY
```

### Модель отвечает, но tools не работают

- Все провайдеры используют нативный prompt-shim для tools; сломанный JSON
  возвращается как `malformed_tool_call`, а не как текст ответа.
  Попробуйте `coding`/`qwen-coder`/`deepseek-chat`.
- В OpenCode у модели должно быть `"tools": true`.

### `OpenAI Chat stream ended without finish_reason`

Upstream-прокси не отправил финальный chunk с `finish_reason`. Обновите
upstream (все три проекта обычно отправляют его). Как временная мера в
`opencode.jsonc` у конкретной модели:

```jsonc
"compatibility": {
  "requireFinishReason": false
}
```

Это заставит OpenCode принять поток без финального маркера; используйте только
если уверены, что обрыв не приводит к потере ответа.

### Долгий первый ответ / обрыв

- DeepSeek «думает» долго. В примере `headerTimeout`/`chunkTimeout` увеличены.
- Проверьте `timeout_seconds` провайдера в `providers.json`; шлюз не будет ждать
  дольше.
- Если upstream отдаёт SSE keep-alive, обрыв маловероятен; если нет — увеличьте
  `chunkTimeout` в OpenCode.

### Пустой ответ

Скорее всего, истекла web-сессия провайдера или он упёрся в лимит.
Проверьте `/readyz` и `freeproxy-auth status`, переавторизуйте аккаунт.

## Docker

- Все провайдеры нативные; `127.0.0.1:3001/3264/9655` больше не нужны.
- `config/providers.docker.json` использует синтетические хосты
  `glm.native`, `qwen.native`, `deepseek.native`.
- Образ содержит Chromium для Qwen; `secrets/` монтируется writable, потому что
  GLM удаляет израсходованные device-токены из `tokens.sqlite`; `.env` читается
  compose, а не образом.
- Для GLM положите `tokens.sqlite` в `secrets/` и задайте `FP_GLM_TOKENS_DB`.
- Проверка:

```bash
docker compose ps
docker compose logs -f gateway
docker compose exec gateway python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8080/health').read())"
```

## Windows

- Используйте `curl.exe`, если PowerShell-алиас `curl` ведёт себя странно.
- Для JSON в PowerShell удобнее `Invoke-RestMethod` или файл с телом.
- Если `uv` не находится, перезапустите терминал после установки.
- Порт занят другим процессом — `Get-NetTCPConnection -LocalPort 8080`.

## Логи

По умолчанию JSON в stdout. Каждая запись содержит `request_id`; он же в
заголовке `X-Request-ID`. Ищите по нему:

```bash
# Linux/macOS
uv run freeproxy 2>&1 | grep <request-id>
```

Промпты и ключи не логируются. Если нужен текстовый формат:

```dotenv
FP_LOG_FORMAT=text
FP_LOG_LEVEL=DEBUG
```

`DEBUG` включает access-логи `/health` и `/readyz`, но не тела запросов.

## Сброс состояния

```bash
curl -X POST http://127.0.0.1:8080/admin/providers/glm/reset \
  -H "X-Admin-Key: <admin-key>"
curl -X POST http://127.0.0.1:8080/admin/limits/reset \
  -H "X-Admin-Key: <admin-key>"
```

Если `FP_ADMIN_API_KEY` не задан, локальный POST дополнительно требует
`X-FreeProxy-Admin: true` (защита от CSRF); лучше просто задать admin key.

Перезапуск шлюза сбрасывает все in-memory лимиты и breaker'ы.
