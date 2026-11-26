from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import UTC, datetime

from .capture import AuthCaptureError, capture
from .exporters import export_all
from .paths import AuthPaths
from .store import AuthStoreError, TokenStore


def _paths(args: argparse.Namespace) -> AuthPaths:
    return AuthPaths.from_env(
        store=args.store,
        deepseek_repo=args.deepseek_repo,
        qwen_repo=args.qwen_repo,
        glm_repo=args.glm_repo,
        browser_channel=args.browser,
        timeout_seconds=args.timeout,
    )


def _print_status(store: TokenStore) -> None:
    print(f"Auth store: {store.path}")
    status = store.status()
    for provider in ("deepseek", "qwen", "glm"):
        item = status[provider]
        if not item.get("configured"):
            print(f"  {provider:9} : not configured")
            continue
        expiry = item.get("expires_at")
        expiry_text = "unknown"
        if expiry:
            expiry_text = datetime.fromtimestamp(int(expiry), tz=UTC).strftime("%Y-%m-%d %H:%M UTC")
        state = "EXPIRED" if item.get("expired") else "ok"
        print(
            f"  {provider:9} : {state}, token={item.get('token_length')} chars, "
            f"expires={expiry_text}, cookies={item.get('has_cookies')}"
        )


async def _terminal_confirm(provider: str) -> None:
    print(
        f"\n[{provider}] Подтвердите, что вы уже вошли и отправили тестовое сообщение. "
        "Нажмите Enter в терминале..."
    )
    try:
        await asyncio.to_thread(input, "> ")
    except EOFError:
        print(f"[{provider}] stdin закрыт; продолжаю в автоматическом режиме.")


async def _login_one(
    provider: str,
    paths: AuthPaths,
    store: TokenStore,
    headless: bool,
    *,
    export_files: bool,
    require_confirmation: bool,
    cdp_url: str | None = None,
) -> None:
    confirm = (lambda: _terminal_confirm(provider)) if require_confirmation else None
    data = await capture(
        provider,
        paths,
        headless=headless,
        confirm=confirm,
        cdp_url=cdp_url,
    )
    store.upsert(provider, data)
    print(f"[{provider}] Сохранено в {store.path} (token {len(data.get('token', ''))} chars).")
    if not export_files:
        return
    from .exporters import export_provider

    exported: dict[str, list[str]] = {}
    files = export_provider(store, paths, provider)
    if files:
        exported[provider] = [str(path) for path in files]
    for files_provider, files in exported.items():
        print(f"[{files_provider}] export:")
        for file in files:
            print(f"  {file}")
    if provider == "glm":
        print(
            "[glm] Captcha: нативный генератор уже встроен. Если Aliyun вернёт "
            "risk-отказ F001, один раз соберите `tokens.sqlite` legacy-коллектором "
            "и укажите `FP_GLM_TOKENS_DB`."
        )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="freeproxy-auth",
        description=(
            "Open a provider login page, capture the web token from the browser, "
            "and save it to secrets/tokens.json. DeepSeek is consumed natively; "
            "GLM/Qwen tokens are also exported for their bridge-прокси."
        ),
    )
    parser.add_argument("--store", help="Path to secrets/tokens.json")
    parser.add_argument("--deepseek-repo", help="Path to FreeDeepseekAPI checkout")
    parser.add_argument("--qwen-repo", help="Path to FreeQwenApi checkout")
    parser.add_argument("--glm-repo", help="Path to GLM-Free-API checkout")
    parser.add_argument(
        "--browser",
        help="Playwright browser channel: chrome (default) or chromium",
    )
    parser.add_argument("--timeout", type=int, help="Login timeout in seconds (default 600)")

    sub = parser.add_subparsers(dest="command", required=True)

    login = sub.add_parser("login", help="Capture a token for one provider")
    login.add_argument("provider", choices=("deepseek", "qwen", "glm", "all"))
    login.add_argument(
        "--headless",
        action="store_true",
        help="Do not open a visible window (only works with an already logged-in profile)",
    )
    login.add_argument(
        "--no-confirm",
        action="store_true",
        help="Do not wait for the terminal confirmation; auto-detect the token",
    )
    login.add_argument(
        "--cdp-url",
        help=("Connect to an already running Chrome over CDP, for example http://127.0.0.1:9222"),
    )
    login.add_argument(
        "--no-export",
        action="store_true",
        help="Save the token store but do not write upstream files",
    )

    sub.add_parser("status", help="Show captured token status")
    export = sub.add_parser("export", help="Write provider-specific files from the token store")
    export.add_argument(
        "provider", nargs="?", choices=("deepseek", "qwen", "glm", "all"), default="all"
    )
    return parser


async def _run(args: argparse.Namespace) -> int:
    paths = _paths(args)
    store = TokenStore(paths.store)

    if args.command == "status":
        _print_status(store)
        return 0

    if args.command == "export":
        if args.provider == "all":
            result = export_all(store, paths)
        else:
            from .exporters import export_provider

            files = export_provider(store, paths, args.provider)
            result = {args.provider: [str(path) for path in files]} if files else {}
        if not result:
            print("Nothing to export: run `freeproxy-auth login ...` first.")
            return 1
        for provider, files in result.items():
            print(f"[{provider}]")
            for file in files:
                print(f"  {file}")
        return 0

    if args.command == "login":
        providers = ("deepseek", "qwen", "glm") if args.provider == "all" else (args.provider,)
        failures = 0
        for provider in providers:
            try:
                await _login_one(
                    provider,
                    paths,
                    store,
                    args.headless,
                    export_files=not args.no_export,
                    require_confirmation=not args.no_confirm,
                    cdp_url=args.cdp_url,
                )
            except AuthCaptureError as exc:
                failures += 1
                print(f"[{provider}] ERROR: {exc}", file=sys.stderr)
        _print_status(store)
        return 1 if failures else 0

    return 1


def main(argv: list[str] | None = None) -> None:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        raise SystemExit(asyncio.run(_run(args)))
    except KeyboardInterrupt:
        print("\nCancelled.")
        raise SystemExit(130) from None
    except (AuthCaptureError, AuthStoreError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
