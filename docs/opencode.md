# OpenCode V2: подключение FreeProxyAPI

Инструкция рассчитана на **OpenCode V2** (конфигурация `opencode.jsonc`,
провайдеры в `providers`, модели в `models`). Для V1 используйте
[гайд миграции](https://opencode.ai/docs/migrate-v1); этот провайдер в V1 не
тестировался.

## 1. Что должно работать

1. Все три провайдера авторизованы: `uv run freeproxy-auth login all`.
2. Запущен FreeProxyAPI на `http://127.0.0.1:8080`.
3. Есть клиентский ключ из `FP_API_KEYS`.

Если токены ещё не захвачены:

```bash
uv run freeproxy-auth login all
uv run freeproxy
```

Отдельные bridge-процессы не нужны. Подробно — [auth.md](auth.md).

Проверка:

```bash
curl http://127.0.0.1:8080/health
curl -H "Authorization: Bearer <ключ>" http://127.0.0.1:8080/v1/models
```

Если `/v1/models` вернул `401`, ключ не совпадает с `FP_API_KEYS`. Если
`/readyz` вернул `503` — проверьте `uv run freeproxy-auth status` и логи
провайдера.

## 2. Конфигурация OpenCode

Скопируйте готовый пример в проект, из которого запускаете OpenCode:

```bash
cp examples/opencode.jsonc ./opencode.jsonc
```

Или создайте `opencode.jsonc` вручную:

```jsonc
{
  "$schema": "https://opencode.ai/config.json",
  "model": "freeproxy/auto",
  "providers": {
    "freeproxy": {
      "name": "FreeProxyAPI",
      "env": ["FREEPROXY_API_KEY"],
      "package": "@opencode/ai/providers/openai-compatible",
      "settings": {
        "baseURL": "http://127.0.0.1:8080/v1",
        "headerTimeout": 660000,
        "chunkTimeout": 660000
      },
      "compatibility": {
        "maxTokensField": "max_tokens",
        "streamUsage": false
      },
      "models": {
        "auto": {
          "name": "Auto (GLM → Qwen → DeepSeek)",
          "modelID": "auto",
          "capabilities": {
            "tools": true,
            "input": ["text"],
            "output": ["text"],
            "freeform": []
          },
          "limit": { "context": 128000, "output": 8192 }
        },
        "coding": {
          "name": "Coding (Qwen 3.7 Plus → GLM → DeepSeek)",
          "modelID": "coding",
          "capabilities": {
            "tools": true,
            "input": ["text"],
            "output": ["text"],
            "freeform": []
          },
          "limit": { "context": 128000, "output": 8192 }
        }
      }
    }
  }
}
```

Полный список моделей (`fast`, `reasoning`, `vision`, `glm-flash`, `glm`,
`qwen-max`, `qwen-coder`, `deepseek-chat`, `deepseek-reasoner`) — в
[examples/opencode.jsonc](../examples/opencode.jsonc).

Если OpenCode запускается не на той же машине, где шлюз, замените
`http://127.0.0.1:8080/v1` на доступный адрес (например,
`http://gateway.local:8080/v1`). Порт шлюза по умолчанию слушает только
loopback; для удалённого доступа нужен reverse proxy.

Важные поля:

| Поле | Значение |
|---|---|
| `providers.freeproxy.package` | `@opencode/ai/providers/openai-compatible` |
| `settings.baseURL` | `http://127.0.0.1:8080/v1` (обязательно с `/v1`) |
| `env` | имя переменной, из которой OpenCode берёт API-ключ |
| `modelID` | ID, который уйдёт в шлюз (`auto` или `glm/glm-4.7`) |
| `capabilities.tools` | `true` для tool calling |
| `capabilities.freeform` | `[]`, чтобы не отправлять lark/freeform tools |
| `compatibility.maxTokensField` | `max_tokens` для GLM/DeepSeek |
| `compatibility.reasoningField` | `reasoning_content` для reasoning-моделей |

`limit.context` и `limit.output` — приблизительные значения для UI OpenCode.
Реальный контекст зависит от upstream-модели; шлюз не увеличивает его.

## 3. Переменная с ключом

OpenCode не читает `.env` шлюза. Передайте ключ в окружении процесса OpenCode.

PowerShell:

```powershell
$env:FREEPROXY_API_KEY = "<ключ из FP_API_KEYS>"
opencode
```

bash:

```bash
export FREEPROXY_API_KEY="<ключ из FP_API_KEYS>"
opencode
```

Если шлюз запущен в анонимном режиме (`FP_API_KEYS` пуст,
`FP_ALLOW_ANONYMOUS=true`), можно указать любое непустое значение:

```powershell
$env:FREEPROXY_API_KEY = "local"
```

Но для постоянной работы лучше всегда задавать настоящий ключ.

## 4. Выбор модели

В OpenCode:

```text
/models
```

Выберите `FreeProxyAPI` → `auto` или другую модель.

Через CLI:

```bash
opencode run --model freeproxy/auto "Объясни, что делает этот репозиторий"
opencode run --model freeproxy/coding "Напиши тест для rate limiter"
```

Важно: селектор использует `providerID/modelID` из конфига OpenCode
(`freeproxy/auto`), а не внутренний ID шлюза. Внутри `modelID` можно указать
`glm/glm-4.7`; тогда запрос пойдёт напрямую в GLM без alias-fallback.

## 5. Какие модели выбирать

| Задача | Модель OpenCode | Что внутри |
|---|---|---|
| Обычный чат | `freeproxy/auto` | GLM Flash → Qwen Max → DeepSeek Chat |
| Быстрые ответы | `freeproxy/fast` | GLM Flash → Qwen Plus → DeepSeek Chat |
| Код, tool calling | `freeproxy/coding` | Qwen 3.7 Plus → GLM 4.7 → DeepSeek Chat |
| Сложные рассуждения | `freeproxy/reasoning` | DeepSeek Reasoner → GLM 5.3 → Qwen Max |
| Картинки | `freeproxy/vision` | Qwen 3.8 Max → GLM-5v |
| Конкретный провайдер | `freeproxy/glm` и т.п. | без fallback |

Для агентной работы (Build) начните с `coding`. Если Qwen 3.7 Plus перегружен,
alias автоматически уйдёт в GLM 4.7 или DeepSeek Chat. В примере заголовок
`X-FreeProxy-Provider` не задан; для принудительного провайдера выбирайте
конкретную модель, например `glm`.

## 6. Tool calling

Все провайдеры нативные, но tools по-прежнему эмулируются prompt-shim'ом:

- **GLM**: нативный `glm.py` разбирает tool-call JSON и отдаёт `tool_calls`.
- **Qwen**: нативный `qwen.py` делает то же через встроенный browser-page.
- **DeepSeek**: нативный `deepseek.py` использует prompt-shim и защиту от
  утечки сломанной разметки.

В OpenCode у моделей выставлено `capabilities.tools: true`. Если модель
начинает «ломать» JSON tool call, попробуйте `coding` или конкретный
`qwen-coder`/`deepseek-chat`, а не `auto`.

## 7. Streaming и reasoning

- `stream: true` проходит через шлюз как SSE без переупаковки.
- Если провайдер оборвёт уже начатый стрим, шлюз пришлёт SSE-событие ошибки
  (`stream_interrupted`); OpenCode увидит незавершённый ответ, а не «успех».
- OpenCode получает `reasoning_content`, если провайдер его отдаёт и в модели
  задан `compatibility.reasoningField`.
- Нативные провайдеры создают stateless-сессию на каждый запрос; история
  передаётся через `messages`. Заголовок `x-agent-session` сохраняется для
  совместимости, но нативными адаптерами не используется.

## 8. Лимиты и 429

OpenCode — агент, он может делать несколько запросов подряд (tool loop,
subagents). Все они считаются в лимитах шлюза. По умолчанию на один API-ключ
разрешено 2 одновременных запроса (`FP_CLIENT_MAX_CONCURRENCY`).

Если пришёл `429`:

1. Посмотрите `Retry-After` и `X-RateLimit-Scope` в ответе.
2. Проверьте `/admin/limits` (нужен admin-ключ).
3. Уменьшите `FP_CLIENT_RPM`/`FP_GLOBAL_RPM` или увеличьте лимиты провайдеров,
   если upstream это позволяет.
4. Для срочной работы выберите конкретную модель с свободным провайдером:
   `/models` → `FreeProxyAPI` → `glm`/`qwen-max`/`deepseek-chat`.

`503` означает, что провайдер в cooldown, перегружен или upstream недоступен.
Проверьте `/readyz` и `docker compose logs`/лог шлюза.

## 9. Где лежит конфиг

| Сценарий | Файл |
|---|---|
| Только этот проект | `<проект>/opencode.jsonc` |
| Все проекты пользователя | `~/.config/opencode/opencode.jsonc` |
| Проект с несколькими пакетами | `opencode.jsonc` или `.opencode/opencode.jsonc` |

OpenCode читает конфиг и горячо перезагружает его. Переменные окружения
подхватываются при запуске процесса; после изменения `FREEPROXY_API_KEY`
перезапустите OpenCode/сервис.

## 10. Проверка без OpenCode

Сначала убедитесь, что сам провайдер работает через OpenAI SDK:

```python
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:8080/v1", api_key="<ключ>")
for model in ("auto", "coding", "reasoning"):
    response = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": "Ответь одним словом: ok"}],
        max_tokens=16,
    )
    print(model, response.choices[0].message.content)
```

Если SDK работает, а OpenCode — нет, проблема в `opencode.jsonc`,
`FREEPROXY_API_KEY` или версии OpenCode. Если не работает SDK, смотрите
`/readyz`, `/admin/status` и логи upstream'ов.

## 11. Частые ошибки

| Симптом | Причина / решение |
|---|---|
| `Model unavailable: freeproxy/auto` | В `models` нет ключа `auto` или провайдер не загрузился. Скопируйте полный пример. |
| `Cannot initialize provider ... baseURL` | В `settings.baseURL` пусто/опечатка; нужен `http://127.0.0.1:8080/v1`. |
| `401 invalid_api_key` | `FREEPROXY_API_KEY` не совпадает с `FP_API_KEYS`. |
| `404 model_not_found` | В `modelID` указан неизвестный namespace/модель. Сверьтесь с `/v1/models`. |
| `429` | Лимит клиента/шлюза/провайдера; смотрите `Retry-After`. |
| `503 provider_unavailable` | Upstream не отвечает или в cooldown; `/readyz`, затем логи upstream. |
| Пустой ответ / обрыв | Истекла сессия upstream'а или сработал его лимит. Переавторизуйте upstream, проверьте `/health`. |
| Tool call не приходит | Для GLM не включён `--agent-mode`; либо модель не поддерживает tools. |
| `OpenAI Chat stream ended without finish_reason` | Upstream не прислал `finish_reason`. Обновите upstream; как временная мера задайте модели `compatibility.requireFinishReason: false`. |
| Долгий первый ответ | DeepSeek/GLM «думают»; `headerTimeout` в примере увеличен. Проверьте `timeout_seconds` провайдера. |

## 12. Ограничения

- OpenCode использует OpenAI Chat Completions; Anthropic `/v1/messages` и
  OpenAI Responses `/v1/responses` шлюзом не предоставляются.
- Медиа-эндпоинты Qwen (`images`, `videos`) не объединены; для них используйте
  исходный Qwen-прокси напрямую.
- Провайдеры неофициальные. OpenCode может ретраить запросы, а upstream — менять
  лимиты; следите за логами и не ставьте агрессивные значения concurrency.
