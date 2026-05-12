from __future__ import annotations

import asyncio
import json
import os
import shutil
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from .config import Settings


EventHandler = Callable[[str, dict[str, Any]], Awaitable[None]]
ApprovalHandler = Callable[[str, dict[str, Any]], Awaitable[str]]


class CodexAppServerClient:
    def __init__(
        self,
        settings: Settings,
        event_handler: EventHandler,
        approval_handler: ApprovalHandler,
    ) -> None:
        self._settings = settings
        self._event_handler = event_handler
        self._approval_handler = approval_handler
        self._process: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task | None = None
        self._next_id = 1
        self._pending: dict[int, asyncio.Future] = {}
        self._start_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()

    async def close(self) -> None:
        if self._reader_task:
            self._reader_task.cancel()
        if self._process:
            self._process.terminate()
            try:
                await asyncio.wait_for(self._process.wait(), timeout=5)
            except asyncio.TimeoutError:
                self._process.kill()
                await self._process.wait()

    async def start_thread(self, cwd: Path) -> str:
        await self.ensure_started()
        result = await self.request(
            "thread/start",
            {
                "model": self._settings.codex_model,
                "modelProvider": None,
                "cwd": str(cwd),
                "approvalPolicy": self._settings.codex_approval_policy,
                "sandbox": self._settings.codex_sandbox,
                "config": None,
                "baseInstructions": None,
                "developerInstructions": None,
                "ephemeral": False,
            },
        )
        thread = result.get("thread", {})
        thread_id = thread.get("id")
        if not isinstance(thread_id, str):
            raise RuntimeError(f"thread/start did not return a thread id: {result}")
        return thread_id

    async def start_turn(self, thread_id: str, cwd: Path, prompt: str) -> str:
        params: dict[str, Any] = {
            "threadId": thread_id,
            "input": [{"type": "text", "text": prompt, "text_elements": []}],
            "cwd": str(cwd),
            "approvalPolicy": self._settings.codex_approval_policy,
            "sandboxPolicy": _sandbox_policy(self._settings.codex_sandbox, cwd),
            "model": self._settings.codex_model,
            "effort": None,
            "summary": None,
            "outputSchema": None,
        }
        result = await self.request("turn/start", params)
        turn = result.get("turn", {})
        turn_id = turn.get("id")
        if not isinstance(turn_id, str):
            raise RuntimeError(f"turn/start did not return a turn id: {result}")
        return turn_id

    async def interrupt_turn(self, thread_id: str, turn_id: str) -> None:
        await self.request("turn/interrupt", {"threadId": thread_id, "turnId": turn_id})

    async def ensure_started(self) -> None:
        async with self._start_lock:
            if self._process and self._process.returncode is None:
                return

            codex_bin = _resolve_executable(self._settings.codex_bin)
            self._process = await asyncio.create_subprocess_exec(
                codex_bin,
                "app-server",
                "--listen",
                "stdio://",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            self._reader_task = asyncio.create_task(self._read_loop())

            await self.request(
                "initialize",
                {
                    "clientInfo": {
                        "name": "feishu_codex",
                        "title": "Feishu Codex Bridge",
                        "version": "0.1.0",
                    },
                    "capabilities": {
                        "experimentalApi": True,
                        "optOutNotificationMethods": [],
                    },
                },
            )
            await self.notify("initialized")

    async def request(self, method: str, params: Any | None = None, timeout: float = 600) -> Any:
        if not self._process or not self._process.stdin:
            raise RuntimeError("Codex app-server is not running")

        request_id = self._next_id
        self._next_id += 1
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        self._pending[request_id] = future

        await self._write({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        return await asyncio.wait_for(future, timeout=timeout)

    async def notify(self, method: str, params: Any | None = None) -> None:
        await self._write({"jsonrpc": "2.0", "method": method, "params": params})

    async def _write(self, message: dict[str, Any]) -> None:
        if not self._process or not self._process.stdin:
            raise RuntimeError("Codex app-server is not running")
        raw = json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n"
        async with self._write_lock:
            self._process.stdin.write(raw.encode("utf-8"))
            await self._process.stdin.drain()

    async def _read_loop(self) -> None:
        assert self._process and self._process.stdout
        try:
            async for raw_line in self._process.stdout:
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    continue
                await self._handle_message(message)
        finally:
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(RuntimeError("Codex app-server exited"))
            self._pending.clear()

    async def _handle_message(self, message: dict[str, Any]) -> None:
        request_id = message.get("id")
        method = message.get("method")

        if request_id is not None and ("result" in message or "error" in message):
            future = self._pending.pop(int(request_id), None)
            if not future or future.done():
                return
            if "error" in message:
                future.set_exception(RuntimeError(message["error"]))
            else:
                future.set_result(message.get("result"))
            return

        if request_id is not None and method:
            asyncio.create_task(self._handle_server_request(int(request_id), method, message.get("params") or {}))
            return

        if method:
            await self._event_handler(method, message.get("params") or {})

    async def _handle_server_request(self, request_id: int, method: str, params: dict[str, Any]) -> None:
        try:
            decision = await self._approval_handler(method, params)
            await self._write({"jsonrpc": "2.0", "id": request_id, "result": {"decision": decision}})
        except Exception as exc:
            await self._write(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": -32000, "message": str(exc)},
                }
            )


def _sandbox_policy(mode: str, cwd: Path) -> dict[str, Any]:
    if mode == "danger-full-access":
        return {"type": "dangerFullAccess"}
    if mode == "read-only":
        return {"type": "readOnly", "networkAccess": False}
    return {
        "type": "workspaceWrite",
        "writableRoots": [str(cwd)],
        "networkAccess": False,
        "excludeTmpdirEnvVar": False,
        "excludeSlashTmp": False,
    }


def _resolve_executable(command: str) -> str:
    path = shutil.which(command)
    if path:
        return path

    if os.name == "nt":
        candidates = []
        if not command.lower().endswith((".exe", ".cmd", ".bat")):
            candidates.extend([f"{command}.cmd", f"{command}.exe", f"{command}.bat"])
        candidates.extend(
            [
                str(Path.home() / "AppData" / "Roaming" / "npm" / f"{command}.cmd"),
                str(Path.home() / "AppData" / "Roaming" / "npm" / f"{command}.exe"),
            ]
        )
        for candidate in candidates:
            resolved = shutil.which(candidate)
            if resolved:
                return resolved
            if Path(candidate).exists():
                return candidate

    raise FileNotFoundError(
        f"Cannot find Codex executable: {command!r}. "
        "Set CODEX_BIN to the full path, for example "
        r"CODEX_BIN=C:\Users\<you>\AppData\Roaming\npm\codex.cmd, "
        "or make sure `codex --version` works in the same terminal that starts this bridge."
    )
