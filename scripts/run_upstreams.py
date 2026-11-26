"""Deprecated launcher for the old external bridges.

All three providers are now native (GLM, Qwen, DeepSeek). There is nothing to
start on ports 3001/3264/9655. This wrapper is kept for compatibility and simply
starts the single FastAPI service.
"""

from __future__ import annotations

import contextlib
import subprocess
import sys


def main() -> None:
    print(
        "All providers are native; starting the single FreeProxyAPI process.\n"
        "Pinned bridge commands are no longer required."
    )
    with contextlib.suppress(KeyboardInterrupt):
        subprocess.run([sys.executable, "-m", "freeproxy"], check=False)


if __name__ == "__main__":
    main()
