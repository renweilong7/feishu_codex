from __future__ import annotations

import asyncio
import os
import re
import shlex
from dataclasses import dataclass
from pathlib import Path

from .config import Settings


@dataclass
class ShellResult:
    handled: bool
    title: str = ""
    body: str = ""
    status: str = "info"
    def __post_init__(self):
            # 只有当 body 不为空时，才添加 shell 代码块标记
            if self.body:
                self.body = f"```shell\n{self.body}\n```"
class ShellCommandHandler:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._cwd_by_session: dict[str, Path] = {}
        self._is_windows = os.name == "nt"
        

    async def handle(self, session_id: str, command: str) -> ShellResult:
        try:
            parts = shlex.split(command)
        except ValueError as exc:
            return ShellResult(True, "Shell error", str(exc), "failed")

        if not parts:
            return ShellResult(False)

        name = parts[0].removeprefix("/")
        args = parts[1:]

        if name == "pwd":
            return ShellResult(True, "Shell pwd", str(self._cwd(session_id)))
        if name == "cd":
            return self._cd(session_id, args)
        if name in {"ls", "ll"}:
            return await self._ls(session_id, name, args)

        return ShellResult(False)

    def _cwd(self, session_id: str) -> Path:
        cwd = self._cwd_by_session.get(session_id)
        if cwd and self._is_allowed(cwd):
            return cwd
        default = self._settings.repos[self._settings.default_repo]
        self._cwd_by_session[session_id] = default
        return default

    def cwd(self, session_id: str) -> Path:
        return self._cwd(session_id)

    def repo_alias_for(self, path: Path) -> str:
        resolved = path.resolve()
        best_alias = self._settings.default_repo
        best_length = -1
        for alias, root in self._settings.repos.items():
            root = root.resolve()
            if resolved == root or root in resolved.parents:
                length = len(str(root))
                if length > best_length:
                    best_alias = alias
                    best_length = length
        return best_alias

    def _cd(self, session_id: str, args: list[str]) -> ShellResult:
        if len(args) > 1:
            return ShellResult(True, "Shell cd", "Usage: cd [repo_alias|path]", "failed")

        if not args:
            target = self._settings.repos[self._settings.default_repo]
        elif args[0] in self._settings.repos:
            target = self._settings.repos[args[0]]
        else:
            target = self._resolve(session_id, args[0])

        if not self._is_allowed(target):
            return ShellResult(True, "Shell cd", f"Path is outside allowed repos: {target}", "failed")
        if not target.exists():
            return ShellResult(True, "Shell cd", f"No such directory: {target}", "failed")
        if not target.is_dir():
            return ShellResult(True, "Shell cd", f"Not a directory: {target}", "failed")

        self._cwd_by_session[session_id] = target
        return ShellResult(True, "Shell cd", str(target), "success")

    async def _ls(self, session_id: str, name: str, args: list[str]) -> ShellResult:
        ls_args = ["-la"] if name == "ll" and not self._is_windows else []
        dir_args = ["/a"] if name == "ll" and self._is_windows else []
        paths: list[Path] = []

        for arg in args:
            if arg.startswith("-"):
                if self._is_windows:
                    if arg in {"-a", "-l", "-la", "-al"}:
                        if "/a" not in dir_args:
                            dir_args.append("/a")
                        continue
                    return ShellResult(True, "Shell ls", f"Unsupported option: {arg}", "failed")
                if not _safe_ls_option(arg):
                    return ShellResult(True, "Shell ls", f"Unsupported option: {arg}", "failed")
                ls_args.append(arg)
                continue
            path = self._resolve(session_id, arg)
            if not self._is_allowed(path):
                return ShellResult(True, "Shell ls", f"Path is outside allowed repos: {path}", "failed")
            paths.append(path)

        if not paths:
            paths = [self._cwd(session_id)]

        if self._is_windows:
            process = await asyncio.create_subprocess_exec(
                "cmd",
                "/c",
                "dir",
                *dir_args,
                *(str(path) for path in paths),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        else:
            process = await asyncio.create_subprocess_exec(
                "/bin/ls",
                *ls_args,
                *(str(path) for path in paths),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        output_bytes, _ = await process.communicate()
        output = output_bytes.decode("utf-8", errors="replace").strip()
        if len(output) > 12000:
            output = "... output truncated ...\n" + output[-12000:]

        status = "success" if process.returncode == 0 else "failed"
        return ShellResult(True, f"Shell {name}", output or "(empty)", status)

    def _resolve(self, session_id: str, raw_path: str) -> Path:
        raw_path = self._normalize_windows_path(raw_path) if self._is_windows else raw_path
        path = Path(raw_path).expanduser()
        if not path.is_absolute():
            path = self._cwd(session_id) / path
        return path.resolve()

    def _normalize_windows_path(self, raw_path: str) -> str:
        value = raw_path.replace("\\", "/").replace("：", ":")
        if value == "/":
            return "C:/"
        if value.startswith("/") and not value.startswith("//"):
            return "C:" + value

        drive_match = re.fullmatch(r"([a-zA-Z])(?::)?(?:/(.*))?", value)
        if drive_match:
            drive = drive_match.group(1).upper()
            rest = drive_match.group(2)
            if rest is None or rest == "":
                return f"{drive}:/"
            return f"{drive}:/{rest}"

        return raw_path

    def _is_allowed(self, path: Path) -> bool:
        resolved = path.resolve()
        for root in self._settings.repos.values():
            root = root.resolve()
            if resolved == root or root in resolved.parents:
                return True
        return False


def _safe_ls_option(value: str) -> bool:
    if not value.startswith("-") or value == "-":
        return False
    return all(char.isalpha() or char == "-" or char == "1" for char in value)
