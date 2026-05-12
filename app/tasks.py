from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

from .cards import approval_card, codex_card, truncate_lines
from .codex_app_server import CodexAppServerClient
from .config import Settings
from .feishu import FeishuClient


APPROVAL_METHODS = {
    "item/commandExecution/requestApproval",
    "item/fileChange/requestApproval",
    "item/permissions/requestApproval",
    "mcpServer/elicitation/request",
    "execCommandApproval",
    "applyPatchApproval",
}

APPROVAL_DECISIONS = {"accept", "acceptForSession", "decline", "cancel"}


@dataclass
class TaskState:
    task_id: str
    source_message_id: str
    card_message_id: str
    repo_alias: str
    repo_path: Path
    cwd: Path
    prompt: str
    status: str = "starting"
    thread_id: str | None = None
    turn_id: str | None = None
    lines: list[str] = field(default_factory=list)
    stream_line_by_item: dict[str, int] = field(default_factory=dict)
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    asyncio_task: asyncio.Task | None = None
    completion: asyncio.Future | None = None


@dataclass
class PendingApproval:
    approval_id: str
    task_id: str
    method: str
    params: dict[str, Any]
    future: asyncio.Future
    created_at: float = field(default_factory=time.time)
    decision: str | None = None


class TaskManager:
    def __init__(self, settings: Settings, feishu: FeishuClient) -> None:
        self._settings = settings
        self._feishu = feishu
        self._tasks: dict[str, TaskState] = {}
        self._tasks_by_thread: dict[str, TaskState] = {}
        self._approvals: dict[str, PendingApproval] = {}
        self._codex = CodexAppServerClient(settings, self.handle_codex_event, self.handle_approval_request)

    async def close(self) -> None:
        await self._codex.close()

    def list_tasks(self) -> list[TaskState]:
        return list(self._tasks.values())

    def list_approvals(self) -> list[PendingApproval]:
        return [approval for approval in self._approvals.values() if not approval.future.done()]

    async def start(
        self,
        source_message_id: str,
        repo_alias: str,
        cwd: Path,
        prompt: str,
    ) -> TaskState:
        repo_path = self._settings.repos[repo_alias]
        task_id = uuid4().hex[:8]
        card = codex_card(
            f"Codex starting: {task_id}",
            f"repo: {repo_alias}\ncwd: {cwd}\n\nStarting app-server turn...",
            "running",
        )
        card_message_id = await self._feishu.reply_card(source_message_id, card)
        state = TaskState(
            task_id=task_id,
            source_message_id=source_message_id,
            card_message_id=card_message_id,
            repo_alias=repo_alias,
            repo_path=repo_path,
            cwd=cwd,
            prompt=prompt,
        )
        state.completion = asyncio.get_running_loop().create_future()
        task = asyncio.create_task(self._run(state))
        state.asyncio_task = task
        self._tasks[task_id] = state
        return state

    async def cancel(self, task_id: str) -> bool:
        state = self._tasks.get(task_id)
        if not state or state.status not in {"starting", "running", "waiting_approval"}:
            return False
        if state.thread_id and state.turn_id:
            try:
                await self._codex.interrupt_turn(state.thread_id, state.turn_id)
            except Exception as exc:
                _append_line(state, f"Failed to interrupt turn: {exc}")
        if state.asyncio_task:
            state.asyncio_task.cancel()
        return True

    async def decide_approval(self, approval_id: str, decision: str) -> bool:
        if decision not in APPROVAL_DECISIONS:
            return False
        approval = self._approvals.get(approval_id)
        if not approval or approval.future.done():
            return False
        approval.decision = decision
        approval.future.set_result(decision)
        task = self._tasks.get(approval.task_id)
        if task:
            _append_line(task, f"Approval {approval_id}: {decision}")
            task.status = "running"
            await self._update(task, "running")
        return True

    async def _run(self, state: TaskState) -> None:
        last_update = 0.0
        try:
            state.thread_id = await self._codex.start_thread(state.cwd)
            self._tasks_by_thread[state.thread_id] = state
            state.status = "running"
            _append_line(state, f"Thread started: {state.thread_id}")
            state.turn_id = await self._codex.start_turn(state.thread_id, state.cwd, state.prompt)
            _append_line(state, f"Turn started: {state.turn_id}")
            await self._update(state, "running")

            assert state.completion is not None
            while not state.completion.done():
                now = time.monotonic()
                if now - last_update >= self._settings.stream_update_interval:
                    await self._update(state, state.status)
                    last_update = now
                await asyncio.sleep(0.2)

            failed = state.completion.result()
            state.status = "failed" if failed else "success"
            state.finished_at = time.time()
            await self._update(state, state.status)
        except asyncio.CancelledError:
            state.status = "cancelled"
            state.finished_at = time.time()
            _append_line(state, "Task cancelled.")
            await self._update(state, "cancelled")
        except Exception as exc:
            state.status = "failed"
            state.finished_at = time.time()
            _append_line(state, f"Bridge error: {exc}")
            await self._update(state, "failed")

    async def handle_codex_event(self, method: str, params: dict[str, Any]) -> None:
        thread_id = params.get("threadId")
        if not isinstance(thread_id, str):
            return
        state = self._tasks_by_thread.get(thread_id)
        if not state:
            return

        _append_event(state, method, params)

        if method == "turn/completed":
            failed = _turn_failed(params)
            if state.completion and not state.completion.done():
                state.completion.set_result(failed)

    async def handle_approval_request(self, method: str, params: dict[str, Any]) -> str:
        if method not in APPROVAL_METHODS:
            return "decline"

        thread_id = params.get("threadId") or params.get("conversationId")
        state = self._tasks_by_thread.get(thread_id) if isinstance(thread_id, str) else None
        if not state:
            return "decline"

        approval_id = uuid4().hex[:8]
        future = asyncio.get_running_loop().create_future()
        approval = PendingApproval(
            approval_id=approval_id,
            task_id=state.task_id,
            method=method,
            params=params,
            future=future,
        )
        self._approvals[approval_id] = approval

        body = _render_approval_body(approval_id, method, params)
        state.status = "waiting_approval"
        _append_line(state, f"Waiting for approval {approval_id}: {method}")
        await self._update(state, "waiting_approval")
        await self._feishu.reply_card(
            state.source_message_id,
            approval_card(approval_id, f"Codex approval: {approval_id}", body),
        )

        try:
            return await asyncio.wait_for(future, timeout=30 * 60)
        except asyncio.TimeoutError:
            if state:
                _append_line(state, f"Approval {approval_id} timed out.")
            return "cancel"
        finally:
            self._approvals.pop(approval_id, None)

    async def _update(self, state: TaskState, status: str) -> None:
        body = _format_task_body(state, self._settings.max_card_lines)
        card = codex_card(
            f"Codex {status}: {state.task_id}",
            f"**Repo:** `{state.repo_alias}`\n**Cwd:** `{state.cwd}`\n\n{body}",
            status,
        )
        await self._feishu.update_card(state.card_message_id, card)


def _append_event(state: TaskState, method: str, params: dict[str, Any]) -> None:
    if method == "item/agentMessage/delta":
        _append_stream_delta(state, _stream_key(method, params), params.get("delta", ""))
        return
    if method == "item/reasoning/summaryTextDelta":
        _append_stream_delta(state, _stream_key(method, params), params.get("delta", ""), prefix="Reasoning: ")
        return
    if method in {"item/commandExecution/outputDelta", "command/exec/outputDelta", "process/outputDelta"}:
        _append_stream_delta(state, _stream_key(method, params), params.get("delta", ""))
        return
    if method == "item/started":
        item = params.get("item") or {}
        item_type = item.get("type")
        if item_type == "commandExecution":
            _append_line(state, f"$ {item.get('command', '')}")
            return
        if item_type == "fileChange":
            _append_line(state, "File change started")
            return
        if item_type in {"mcpToolCall", "dynamicToolCall"}:
            _append_line(state, f"Tool started: {item.get('tool', item_type)}")
            return
        if item_type not in {"agentMessage", "reasoning", "userMessage"}:
            _append_line(state, f"Started: {item_type}")
        return
    if method == "item/completed":
        item = params.get("item") or {}
        item_type = item.get("type")
        if item_type == "agentMessage":
            if item.get("text") and not _item_was_streamed(state, item.get("id")):
                _append_line(state, item.get("text", ""))
            return
        if item_type == "commandExecution":
            output = item.get("aggregatedOutput") or ""
            exit_code = item.get("exitCode")
            if output and not _item_was_streamed(state, item.get("id")):
                _append_line(state, output)
            _append_line(state, f"Command exit: {exit_code}")
            return
        if item_type == "fileChange":
            _append_line(state, "File change completed")
            return
    if method == "turn/completed":
        _append_line(state, "Turn completed")
        return
    if method in {"error", "warning", "guardianWarning"}:
        _append_line(state, f"{method}: {json.dumps(params, ensure_ascii=False)[:500]}")


def _append_line(state: TaskState, text: str) -> None:
    text = _clean_text(text)
    if not text:
        return
    state.lines.extend(text.splitlines() or [text])


def _append_stream_delta(state: TaskState, key: str, delta: str, prefix: str = "") -> None:
    if not delta:
        return
    if key not in state.stream_line_by_item:
        state.lines.append(prefix)
        state.stream_line_by_item[key] = len(state.lines) - 1

    line_index = state.stream_line_by_item[key]
    for part in delta.splitlines(keepends=True):
        if part.endswith(("\n", "\r")):
            state.lines[line_index] += part.rstrip("\r\n")
            state.lines.append("")
            line_index = len(state.lines) - 1
            state.stream_line_by_item[key] = line_index
        else:
            state.lines[line_index] += part


def _stream_key(method: str, params: dict[str, Any]) -> str:
    item_id = params.get("itemId") or params.get("item_id") or params.get("processId")
    return f"{method}:{item_id or 'default'}"


def _item_was_streamed(state: TaskState, item_id: Any) -> bool:
    if not item_id:
        return False
    suffix = f":{item_id}"
    return any(key.endswith(suffix) for key in state.stream_line_by_item)


def _format_task_body(state: TaskState, max_lines: int) -> str:
    lines = [_clean_text(line) for line in state.lines]
    lines = _compact_blank_lines(lines)
    return truncate_lines(lines, max_lines)


def _clean_text(text: str) -> str:
    return text.replace("```", "'''").strip("\r")


def _compact_blank_lines(lines: list[str]) -> list[str]:
    compacted: list[str] = []
    blank_seen = False
    for line in lines:
        if not line.strip():
            if not blank_seen:
                compacted.append("")
            blank_seen = True
            continue
        compacted.append(line)
        blank_seen = False
    while compacted and not compacted[-1].strip():
        compacted.pop()
    return compacted


def _turn_failed(params: dict[str, Any]) -> bool:
    turn = params.get("turn") or {}
    status = turn.get("status")
    return status == "failed" or bool(turn.get("error"))


def _render_approval_body(approval_id: str, method: str, params: dict[str, Any]) -> str:
    lines = [
        f"approval_id: {approval_id}",
        f"method: {method}",
    ]
    command = params.get("command")
    if command:
        lines.append(f"command: {command}")
    cwd = params.get("cwd")
    if cwd:
        lines.append(f"cwd: {cwd}")
    reason = params.get("reason")
    if reason:
        lines.append(f"reason: {reason}")
    grant_root = params.get("grantRoot")
    if grant_root:
        lines.append(f"grantRoot: {grant_root}")
    lines.append("")
    lines.append(f"Reply with: /approve {approval_id}, /approve-session {approval_id}, /deny {approval_id}, or /abort {approval_id}")
    return "\n".join(lines)
