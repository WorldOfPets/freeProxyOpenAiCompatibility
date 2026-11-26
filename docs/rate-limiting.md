# Rate limiting

Задача лимитов — не дать одному клиенту или агенту выесть динамические квоты
веб-аккаунтов и не провоцировать `429`/WAF у upstream'ов. Гарантировать
официальные нормы провайдеров нельзя: они меняются и зависят от аккаунта.
Шлюз делает поток предсказуемым и быстро отдаёт `429`/`503` вместо шквала
ошибок.

## Слои

| Слой | Где настраивается | По умолчанию | Что ограничивает |
|---|---|---:|---|
| Клиентский RPM | `FP_CLIENT_RPM` | 30 | запросы/мин на API-ключ |
| Клиентская concurrency | `FP_CLIENT_MAX_CONCURRENCY` | 2 | одновременные запросы на ключ |
| Клиентская суточная квота | `FP_CLIENT_DAILY_REQUESTS` | 1000 | запросы/UTC-сутки на ключ |
| Общий RPM шлюза | `FP_GLOBAL_RPM` | 60 | все клиенты вместе |
| Общая concurrency | `FP_GLOBAL_MAX_CONCURRENCY` | 8 | одновременные запросы |
| RPM провайдера | `max_rpm` в `providers.json` | 6/6/10 | запросы/мин к конкретному провайдеру |
| Суточная квота провайдера | `max_requests_per_day` | 300/300/400 | запросы/сутки к провайдеру |
| Concurrency провайдера | `max_concurrency` | 1/1/2 | одновременные запросы к провайдеру |
| Очередь | `queue_timeout_seconds` | 60/60/8 | сколько ждать слот провайдера |
| Circuit breaker | автоматически | — | временно исключает сбойный upstream |
| Размер тела | `FP_MAX_BODY_BYTES` | 8 МБ | защита от гигантских промптов |
| Выход | `FP_MAX_OUTPUT_TOKENS` | 8192 | верхняя граница `max_tokens` |

Клиентский RPM и суточная квота используют API-ключ. В анонимном режиме
идентификатор — IP-адрес (без доверия `X-Forwarded-For`). За reverse proxy
анонимные клиенты будут считаться одним клиентом; используйте ключи.

## Алгоритм

- RPM — **sliding window** на 60 секунд, а не жёсткий bucket по календарной
  минуте. Поэтому всплеск в конце минуты не даёт двойной квоты.
- Суточная квота — по UTC-дате; сброс в `00:00 UTC`.
- Проверка и списание происходят без `await` между ними, поэтому запросы не
  могут «проскочить» лимит из-за гонки.
- Сначала проверяются все слои; если хотя бы один против, ни один счётчик не
  списывается (кроме уже списанного более внешнего слоя в редких гонках).
- Запрос к alias может сделать несколько upstream-попыток. Каждая попытка
  расходует RPM/квоту **провайдера**, но клиентский запрос считается один раз.
- Если провайдер ответил `429`, шлюз открывает cooldown (уважает
  `Retry-After`) и пробует следующего кандидата alias'а.
- После трёх подряд сетевых/`5xx` ошибок провайдер уходит в cooldown
  (15 → 30 → 60 … до 300 секунд). Успешный запрос сбрасывает счётчик.

## Заголовки

Успешный ответ:

```text
X-RateLimit-Limit: 30
X-RateLimit-Remaining: 27
X-RateLimit-Reset: 1760000000
X-RateLimit-Policy: 30;w=60

X-RateLimit-Daily-Limit: 1000
X-RateLimit-Daily-Remaining: 997
X-RateLimit-Daily-Reset: 1760054400

X-RateLimit-Global-Limit: 60
X-RateLimit-Global-Remaining: 58
X-RateLimit-Global-Reset: 1760000000
```

`X-RateLimit-Reset` — Unix timestamp, когда окно/квота освободится.

При превышении:

```http
HTTP/1.1 429 Too Many Requests
Retry-After: 42
X-RateLimit-Scope: client_rpm
Content-Type: application/json
```

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

Возможные `X-RateLimit-Scope`:

- `client_rpm`
- `client_daily`
- `client_concurrency`
- `gateway_rpm`
- `provider:glm`, `provider:qwen`, `provider:deepseek`
- `provider:...:daily`
- `providers` — все кандидаты alias'а упёрлись в лимиты

`503` с `Retry-After` означает не квоту, а недоступность/перегрузку:
`provider_busy`, `provider_unavailable`, `gateway_busy`.

## Настройка

`.env`:

```dotenv
FP_CLIENT_RPM=20
FP_CLIENT_DAILY_REQUESTS=800
FP_GLOBAL_RPM=45
FP_GLOBAL_MAX_CONCURRENCY=6
```

`config/providers.json`:

```json
{
  "max_rpm": 10,
  "max_requests_per_day": 400,
  "max_concurrency": 2,
  "queue_timeout_seconds": 8
}
```

Начните консервативно. Если upstream'ы стабильны и не отдают `429`, можно
поднимать на 20–30% за раз и наблюдать:

- `/admin/stats` — ошибки и `provider_rate_limited`;
- `/admin/providers` — breaker, used RPM, active concurrency;
- `/readyz` — доступность;
- `Retry-After` в ответах.

## Почему не TPM

Точный подсчёт токенов в streaming-ответе требует доверять `usage` upstream'а,
а веб-прокси отдают его не всегда. Поэтому шлюз ограничивает запросы,
concurrency и размер выхода. Если нужен TPM, его лучше добавить на уровне
конкретного upstream'а или reverse proxy, где известен биллинг.

## Что делать при 429

1. Прочитайте `Retry-After` и `X-RateLimit-Scope`.
2. Подождите указанное время; не ретрайте сразу.
3. Проверьте, не запущены ли параллельно другие агенты/IDE.
4. Уменьшите `FP_GLOBAL_MAX_CONCURRENCY` или `max_concurrency` провайдера.
5. Для срочной задачи выберите модель другого провайдера
   (`glm`/`qwen-max`/`deepseek-chat`) вместо alias'а.
6. Если upstream регулярно отдаёт `429`, поднимите его лимиты/аккаунты или
   уменьшите `max_rpm` в `providers.json`.

## Сброс

Админ-эндпоинты (нужен `FP_ADMIN_API_KEY` или loopback):

```bash
curl -X POST http://127.0.0.1:8080/admin/providers/glm/reset \
  -H "X-Admin-Key: <admin-key>"

curl -X POST http://127.0.0.1:8080/admin/limits/reset \
  -H "X-Admin-Key: <admin-key>"
```

Сброс лимитов не отменяет cooldown upstream'а и не лечит истёкшую авторизацию.
