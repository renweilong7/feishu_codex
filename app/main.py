from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException, Request

from .handlers import handle_approval_action, handle_message_event
from .runtime import feishu, settings, tasks


app = FastAPI(title="Feishu Codex Bridge")


@app.on_event("shutdown")
async def shutdown() -> None:
    await tasks.close()
    await feishu.close()


@app.get("/health")
async def health() -> dict[str, Any]:
    return {"ok": True, "repos": sorted(settings.repos)}


@app.post("/feishu/events")
async def feishu_events(request: Request) -> dict[str, Any]:
    body = await request.json()

    if body.get("type") == "url_verification":
        _verify_token(body)
        return {"challenge": body["challenge"]}

    header = body.get("header", {})
    event_type = header.get("event_type") or body.get("event", {}).get("type")
    if event_type != "im.message.receive_v1":
        return {"code": 0}

    await handle_message_event(body.get("event", {}))
    return {"code": 0}


@app.post("/feishu/card")
async def feishu_card_action(request: Request) -> dict[str, Any]:
    body = await request.json()
    if body.get("type") == "url_verification":
        _verify_token(body)
        return {"challenge": body["challenge"]}

    token = body.get("token")
    if settings.feishu_verification_token and token != settings.feishu_verification_token:
        raise HTTPException(status_code=403, detail="Invalid verification token")

    action = body.get("action") or {}
    value = action.get("value") or {}
    if value.get("action") != "codex_approval":
        return {"msg": "ignored"}

    approval_id = str(value.get("approval_id") or "")
    decision = str(value.get("decision") or "")
    ok = await handle_approval_action(approval_id, decision)
    return {"toast": {"type": "success" if ok else "error", "content": "已处理" if ok else "审批已失效"}}


def _verify_token(body: dict[str, Any]) -> None:
    expected = settings.feishu_verification_token
    if expected and body.get("token") != expected:
        raise HTTPException(status_code=403, detail="Invalid verification token")
