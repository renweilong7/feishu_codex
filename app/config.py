from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


load_dotenv()


def _csv(value: str | None) -> set[str]:
    if not value:
        return set()
    return {item.strip() for item in value.split(",") if item.strip()}


def _repo_map(value: str | None) -> dict[str, Path]:
    repos: dict[str, Path] = {}
    if not value:
        return repos

    for item in value.split(","):
        item = item.strip()
        if not item:
            continue
        if "=" not in item:
            raise ValueError(f"Invalid CODEX_REPOS entry: {item!r}")
        alias, raw_path = item.split("=", 1)
        alias = alias.strip()
        path = Path(raw_path.strip()).expanduser().resolve()
        if not alias:
            raise ValueError(f"Invalid CODEX_REPOS alias: {item!r}")
        repos[alias] = path
    return repos


@dataclass(frozen=True)
class Settings:
    feishu_app_id: str
    feishu_app_secret: str
    feishu_verification_token: str
    allowed_users: set[str]
    allowed_chats: set[str]
    repos: dict[str, Path]
    default_repo: str
    codex_bin: str
    codex_model: str | None
    codex_sandbox: str
    codex_approval_policy: str
    codex_skip_git_repo_check: bool
    stream_update_interval: float
    max_card_lines: int
    host: str
    port: int

    @classmethod
    def from_env(cls) -> "Settings":
        repos = _repo_map(os.getenv("CODEX_REPOS"))
        default_repo = os.getenv("DEFAULT_REPO", "").strip()
        if repos and default_repo not in repos:
            default_repo = next(iter(repos))

        return cls(
            feishu_app_id=os.getenv("FEISHU_APP_ID", ""),
            feishu_app_secret=os.getenv("FEISHU_APP_SECRET", ""),
            feishu_verification_token=os.getenv("FEISHU_VERIFICATION_TOKEN", ""),
            allowed_users=_csv(os.getenv("ALLOWED_USERS")),
            allowed_chats=_csv(os.getenv("ALLOWED_CHATS")),
            repos=repos,
            default_repo=default_repo,
            codex_bin=os.getenv("CODEX_BIN", "codex"),
            codex_model=os.getenv("CODEX_MODEL") or None,
            codex_sandbox=os.getenv("CODEX_SANDBOX", "workspace-write"),
            codex_approval_policy=os.getenv("CODEX_APPROVAL_POLICY", "never"),
            codex_skip_git_repo_check=os.getenv("CODEX_SKIP_GIT_REPO_CHECK", "true").lower()
            in {"1", "true", "yes", "on"},
            stream_update_interval=float(os.getenv("CODEX_STREAM_UPDATE_INTERVAL", "1.5")),
            max_card_lines=int(os.getenv("CODEX_MAX_CARD_LINES", "32")),
            host=os.getenv("HOST", "0.0.0.0"),
            port=int(os.getenv("PORT", "8000")),
        )

    def validate(self) -> None:
        missing = []
        if not self.feishu_app_id:
            missing.append("FEISHU_APP_ID")
        if not self.feishu_app_secret:
            missing.append("FEISHU_APP_SECRET")
        if not self.repos:
            missing.append("CODEX_REPOS")
        if missing:
            raise RuntimeError(f"Missing required configuration: {', '.join(missing)}")
