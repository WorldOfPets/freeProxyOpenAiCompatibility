from __future__ import annotations

import asyncio
import base64
import json
import math
import struct
from typing import Any

import httpx
import wasmtime

POW_DEFAULT_WASM = "https://fe-static.deepseek.com/chat/static/sha3_wasm_bg.7b9ca65ddd.wasm"


class PowError(RuntimeError):
    pass


_MODULE_CACHE: dict[str, tuple[wasmtime.Engine, wasmtime.Module]] = {}


class PowSolver:
    """DeepSeek proof-of-work solver using the upstream SHA3 WASM module."""

    def __init__(self, client: httpx.AsyncClient, wasm_url: str | None = None) -> None:
        self.client = client
        self.wasm_url = wasm_url or POW_DEFAULT_WASM
        self._engine: wasmtime.Engine | None = None
        self._module: wasmtime.Module | None = None
        self._lock = asyncio.Lock()

    async def _module_for(self) -> wasmtime.Module:
        async with self._lock:
            cached = _MODULE_CACHE.get(self.wasm_url)
            if cached:
                self._engine, self._module = cached
                return self._module
            response = await self.client.get(self.wasm_url, timeout=30)
            response.raise_for_status()
            wasm_bytes = response.content
            if not wasm_bytes.startswith(b"\x00asm"):
                raise PowError("DeepSeek PoW WASM URL did not return a WebAssembly module")
            engine = wasmtime.Engine()
            module = wasmtime.Module(engine, wasm_bytes)
            _MODULE_CACHE[self.wasm_url] = (engine, module)
            self._engine, self._module = engine, module
            return module

    async def solve(self, challenge: dict[str, Any]) -> int:
        module = await self._module_for()
        engine = self._engine
        if engine is None:
            raise PowError("PoW engine is not initialized")
        return await asyncio.to_thread(self._solve_sync, challenge, engine, module)

    @staticmethod
    def _solve_sync(
        challenge: dict[str, Any],
        engine: wasmtime.Engine,
        module: wasmtime.Module,
    ) -> int:
        challenge_text = str(challenge.get("challenge") or "")
        salt = str(challenge.get("salt") or "")
        expire_at = str(challenge.get("expire_at") or "")
        difficulty = challenge.get("difficulty")
        if not challenge_text or not salt or difficulty is None:
            raise PowError("DeepSeek PoW challenge is missing challenge/salt/difficulty")
        prefix = f"{salt}_{expire_at}_"

        store = wasmtime.Store(engine)
        instance = wasmtime.Instance(store, module, [])
        exports = instance.exports(store)
        memory = exports["memory"]
        alloc = exports["__wbindgen_export_0"]
        stack_adjust = exports["__wbindgen_add_to_stack_pointer"]
        solve = exports["wasm_solve"]

        challenge_bytes = challenge_text.encode()
        prefix_bytes = prefix.encode()
        challenge_ptr = int(alloc(store, len(challenge_bytes), 1))
        prefix_ptr = int(alloc(store, len(prefix_bytes), 1))
        memory.write(store, challenge_bytes, challenge_ptr)
        memory.write(store, prefix_bytes, prefix_ptr)

        stack_ptr = int(stack_adjust(store, -16))
        try:
            solve(
                store,
                stack_ptr,
                challenge_ptr,
                len(challenge_bytes),
                prefix_ptr,
                len(prefix_bytes),
                float(difficulty),
            )
            raw = bytes(memory.read(store, stack_ptr, stack_ptr + 16))
        finally:
            stack_adjust(store, 16)
        code = struct.unpack_from("<i", raw, 0)[0]
        answer = struct.unpack_from("<d", raw, 8)[0]
        if code == 0 or not math.isfinite(answer) or answer < 0:
            raise PowError("DeepSeek PoW solver returned no answer")
        return int(answer)

    @staticmethod
    def build_header(challenge: dict[str, Any], answer: int, target_path: str) -> str:
        value = {
            "algorithm": challenge.get("algorithm"),
            "challenge": challenge.get("challenge"),
            "salt": challenge.get("salt"),
            "answer": answer,
            "signature": challenge.get("signature"),
            "target_path": target_path,
        }
        return base64.b64encode(json.dumps(value, separators=(",", ":")).encode()).decode("ascii")
