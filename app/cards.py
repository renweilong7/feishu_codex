from __future__ import annotations


def truncate_lines(lines: list[str], max_lines: int) -> str:
    visible = lines[-max_lines:]
    prefix = ""
    if len(lines) > max_lines:
        prefix = f"... omitted {len(lines) - max_lines} earlier lines\n"
    return prefix + "\n".join(visible)


def codex_card(title: str, body: str, status: str = "info") -> dict:
    colors = {
        "info": "blue",
        "running": "wathet",
        "waiting_approval": "orange",
        "success": "green",
        "failed": "red",
        "cancelled": "grey",
    }
    body = body or "No output yet."
    return {
        "config": {"wide_screen_mode": True, "update_multi": True},
        "header": {
            "template": colors.get(status, "blue"),
            "title": {"tag": "plain_text", "content": title},
        },
        "elements": [
            {
                "tag": "markdown",
                "content": f"{body}\n",
            }
        ],
    }


def approval_card(
    approval_id: str,
    title: str,
    body: str,
) -> dict:
    return {
        "config": {"wide_screen_mode": True, "update_multi": True},
        "header": {
            "template": "orange",
            "title": {"tag": "plain_text", "content": title},
        },
        "elements": [
            {
                "tag": "markdown",
                "content": f"{body}\n",
            },
            {
                "tag": "action",
                "actions": [
                    _approval_button("批准", "primary", approval_id, "accept"),
                    _approval_button("本会话批准", "default", approval_id, "acceptForSession"),
                    _approval_button("拒绝", "danger", approval_id, "decline"),
                    _approval_button("取消", "default", approval_id, "cancel"),
                ],
            },
        ],
    }


def _approval_button(label: str, button_type: str, approval_id: str, decision: str) -> dict:
    return {
        "tag": "button",
        "text": {"tag": "plain_text", "content": label},
        "type": button_type,
        "value": {
            "action": "codex_approval",
            "approval_id": approval_id,
            "decision": decision,
        },
    }


def _card_safe(value: str) -> str:
    # Feishu card payloads have size limits; keep one update comfortably small.
    value = value.replace("```", "'''")
    if len(value) > 12000:
        return "... output truncated ...\n" + value[-12000:]
    return value
