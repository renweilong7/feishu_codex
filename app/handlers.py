from __future__ import annotations

import json
from typing import Any

from .cards import codex_card
from .runtime import feishu, settings, shell_commands, tasks


async def handle_message_event(event: dict[str, Any]) -> None:
    sender = event.get("sender", {})
    message = event.get("message", {})
    open_id = sender.get("sender_id", {}).get("open_id", "")
    chat_id = message.get("chat_id", "")
    message_id = message.get("message_id", "")

    if not allowed(open_id, chat_id):
        if message_id:
            await feishu.reply_card(
                message_id,
                codex_card("Codex rejected", "You are not allowed to use this bridge.", "failed"),
            )
        return

    text = extract_text(message)
    if not text:
        return

    session_id = chat_id or open_id or message_id
    await handle_command(message_id, session_id, text)


def allowed(open_id: str, chat_id: str) -> bool:
    if settings.allowed_users and open_id not in settings.allowed_users:
        return False
    if settings.allowed_chats and chat_id not in settings.allowed_chats:
        return False
    return True


def extract_text(message: dict[str, Any]) -> str:
    if message.get("message_type") != "text":
        return ""
    content = message.get("content", "")
    try:
        payload = json.loads(content)
    except json.JSONDecodeError:
        return content.strip()
    text = payload.get("text", "")
    if not isinstance(text, str):
        return ""
    return text.strip()


async def handle_command(message_id: str, session_id: str, text: str) -> None:
    command = text.strip()
    if command.startswith("/approve-session"):
        await _handle_approval_command(message_id, command, "acceptForSession", "/approve-session <approval_id>")
        return

    if command.startswith("/approve"):
        await _handle_approval_command(message_id, command, "accept", "/approve <approval_id>")
        return

    if command.startswith("/deny"):
        await _handle_approval_command(message_id, command, "decline", "/deny <approval_id>")
        return

    if command.startswith("/abort"):
        await _handle_approval_command(message_id, command, "cancel", "/abort <approval_id>")
        return

    if command.startswith("/approvals"):
        lines = [
            f"{approval.approval_id} task={approval.task_id} method={approval.method}"
            for approval in tasks.list_approvals()
        ]
        await feishu.reply_card(
            message_id,
            codex_card("Codex approvals", "\n".join(lines) if lines else "No pending approvals."),
        )
        return

    if command.startswith("/cancel"):
        parts = command.split(maxsplit=1)
        if len(parts) != 2:
            await feishu.reply_card(message_id, codex_card("Codex cancel", "Usage: /cancel <task_id>"))
            return
        cancelled = await tasks.cancel(parts[1].strip())
        result = "Cancellation requested." if cancelled else "Task not found or not running."
        await feishu.reply_card(message_id, codex_card("Codex cancel", result))
        return

    if command.startswith("/status"):
        lines = [
            f"{task.task_id} {task.status} repo={task.repo_alias} cwd={task.cwd}"
            for task in tasks.list_tasks()
        ]
        await feishu.reply_card(
            message_id,
            codex_card("Codex status", "\n".join(lines) if lines else "No tasks."),
        )
        return

    shell_result = await shell_commands.handle(session_id, command)
    if shell_result.handled:
        await feishu.reply_card(
            message_id,
            codex_card(shell_result.title, shell_result.body, shell_result.status),
        )
        return

    if command.startswith("/codex"):
        command = command.removeprefix("/codex").strip()

    repo_alias, prompt = parse_repo_and_prompt(command)
    if not prompt:
        await feishu.reply_card(
            message_id,
            codex_card(
                "Codex usage",
                "Usage: /codex [repo_alias] <task>\nRepos: " + ", ".join(sorted(settings.repos)),
            ),
        )
        return

    if repo_alias not in settings.repos:
        await feishu.reply_card(
            message_id,
            codex_card(
                "Codex rejected",
                f"Unknown repo: {repo_alias}\nRepos: " + ", ".join(sorted(settings.repos)),
                "failed",
            ),
        )
        return

    if _command_has_explicit_repo(command):
        cwd = settings.repos[repo_alias]
    else:
        cwd = shell_commands.cwd(session_id)
        repo_alias = shell_commands.repo_alias_for(cwd)

    await tasks.start(message_id, repo_alias, cwd, prompt)


async def handle_approval_action(approval_id: str, decision: str) -> bool:
    return await tasks.decide_approval(approval_id, decision)


async def _handle_approval_command(
    message_id: str,
    command: str,
    decision: str,
    usage: str,
) -> None:
    parts = command.split(maxsplit=1)
    if len(parts) != 2:
        await feishu.reply_card(message_id, codex_card("Codex approval", f"Usage: {usage}", "failed"))
        return
    approval_id = parts[1].strip()
    ok = await tasks.decide_approval(approval_id, decision)
    result = f"Approval {approval_id}: {decision}" if ok else "Approval not found or already resolved."
    await feishu.reply_card(message_id, codex_card("Codex approval", result, "success" if ok else "failed"))


def parse_repo_and_prompt(command: str) -> tuple[str, str]:
    parts = command.split(maxsplit=1)
    if not parts:
        return settings.default_repo, ""
    if parts[0] in settings.repos:
        return parts[0], parts[1].strip() if len(parts) > 1 else ""
    return settings.default_repo, command.strip()


def _command_has_explicit_repo(command: str) -> bool:
    parts = command.split(maxsplit=1)
    return bool(parts and parts[0] in settings.repos)
