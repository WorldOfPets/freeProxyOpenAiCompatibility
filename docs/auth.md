# Авторизация в браузере

FreeProxyAPI не хранит пароли и не обходит captcha. `freeproxy-auth` открывает
обычный Chrome, ждёт, пока вы войдёте в веб-чат, и **после подтверждения Enter**
забирает токен из `localStorage`/cookies в `secrets/tokens.json`. Нативные
провайдеры читают этот файл напрямую.

| Провайдер | Что забираем | Куда пишем |
|---|---|---|
| DeepSeek | `token`, cookies, `x-hif-dliq`/`x-hif-leim`, wasm URL | `secrets/tokens.json` |
| Qwen | JWT из `localStorage.token`, cookies, Baxia-заголовки | `secrets/tokens.json` |
| GLM / Z.AI | `ZAI_TOKEN` (JWT из `localStorage.token`), `user_id` из JWT | `secrets/tokens.json` |

## 1. Подготовка

```bash
uv sync
uv run playwright install chromium   # не нужно, если установлен Google Chrome
```

`FP_AUTH_BROWSER_CHANNEL=chrome` (по умолчанию) использует обычный Chrome через
DevTools-порт, без `--no-sandbox` и automation-флагов Playwright. Это важно для
Aliyun Access Verification: слайдер в automation-браузере может не проходить.

Если Chrome/Edge лежит нестандартно:

```dotenv
FP_AUTH_CHROME_PATH=D:/Apps/Chrome/chrome.exe
```

## 2. Вход

```bash
uv run freeproxy-auth login deepseek
uv run freeproxy-auth login qwen
uv run freeproxy-auth login glm
# или все сразу:
uv run freeproxy-auth login all
```

Что происходит:

1. Открывается обычный Chrome с persistent-профилем
   `secrets/browser/<provider>`.
2. Вы входите в аккаунт и открываете чат.
3. Для Qwen/GLM отправьте тестовое сообщение — фронтенд обновит токен и
   Baxia/device-заголовки.
4. Вернитесь в терминал и нажмите **Enter**. Только после этого CLI читает
   токен. Это защищает от захвата старой/пустой сессии.
5. Токен, cookies и заголовки сохраняются в `secrets/tokens.json` (0600).

Токены **не печатаются** в консоль.

Автоматический режим без Enter (для уже авторизованного профиля):

```bash
uv run freeproxy-auth login qwen --no-confirm
```

### Профиль браузера

Профили лежат в `secrets/browser/<provider>`. Чтобы войти в другой аккаунт,
закройте браузер и удалите профиль провайдера.

### Уже запущенный Chrome

Можно подключиться к обычному Chrome, который вы открыли сами:

```bash
chrome.exe --remote-debugging-port=9222
uv run freeproxy-auth login qwen --cdp-url http://127.0.0.1:9222
```

## 3. Что появляется на диске

`secrets/tokens.json`:

```json
{
  "version": 1,
  "providers": {
    "deepseek": {"token": "...", "cookie": "...", "hif_dliq": "...", "wasm_url": "..."},
    "qwen": {"token": "...", "cookies": [], "baxia_headers": {}},
    "glm": {"token": "...", "zai_token": "..."}
  }
}
```

- **DeepSeek:** нативный PoW/SSE читает этот файл при каждом запросе.
- **Qwen:** нативный Playwright-сеанс подставляет cookies и `localStorage.token`.
- **GLM:** нативный HTTP читает токен и декодирует `user_id` из JWT.

Старые файлы `deepseek-auth.json`, `session/`, `upstream.env` нужны только для
`freeproxy-auth export`; нативные провайдеры их не используют.

## 4. Проверка

```bash
uv run freeproxy-auth status
uv run python scripts/check_upstreams.py
```

`status` показывает только длину токена и срок действия, без самого токена:

```text
Auth store: secrets/tokens.json
  deepseek  : ok, token=1234 chars, expires=2026-12-01 10:00 UTC, cookies=True
  qwen      : ok, token=987 chars, expires=2026-11-22 12:30 UTC, cookies=True
  glm       : ok, token=1100 chars, expires=2026-12-01 10:00 UTC, cookies=True
```

## 5. Обновление токенов

- **Qwen:** токен короткоживущий (~15 минут). Нативный провайдер читает
  актуальный `localStorage.token` из страницы; если сессия истекла, повторите
  `uv run freeproxy-auth login qwen`.
- **DeepSeek:** токен живёт дольше; после `401/403` повторите вход. Провайдер
  читает store при каждом запросе, перезапуск не нужен.
- **GLM:** `ZAI_TOKEN` истекает по JWT `exp`; после входа перезапуск не нужен,
  потому что токен читается из store на каждый запрос. Для captcha см.
  [providers.md](providers.md).

## 6. Капча Access Verification

Если Qwen/GLM показывает слайдер **Access Verification**:

1. Убедитесь, что в логе есть строка `Запускаю обычный Chrome: ...`, а не
   Playwright Chromium.
2. Пройдите слайдер вручную.
3. Отправьте тестовое сообщение и нажмите Enter в терминале.
4. Если слайдер не проходит, попробуйте `--cdp-url` с уже запущенным Chrome.
5. Для GLM встроенный генератор captcha может получить risk-отказ Aliyun
   (`F001`). Надёжный путь — один раз собрать `tokens.sqlite` legacy-коллектором
   и указать `FP_GLM_TOKENS_DB`.

## 7. Безопасность

- `secrets/` — секретный каталог; он в `.gitignore`.
- `secrets/tokens.json`, `deepseek-auth.json`, `session/`, browser-профили и
  `tokens.sqlite` нельзя коммитить, копировать в образ или показывать в логах.
- На сервере без GUI выполните вход на машине с браузером и перенесите
  `secrets/tokens.json` (и `tokens.sqlite` для GLM).
