from __future__ import annotations

import asyncio
import json
import signal
from typing import Any

import lark_oapi as lark
from lark_oapi.event.callback.model.p2_card_action_trigger import (
    CallBackToast,
    P2CardActionTrigger,
    P2CardActionTriggerResponse,
)

from app.handlers import handle_approval_action, handle_message_event
from app.runtime import feishu, settings, tasks


def _event_to_dict(data: Any) -> dict[str, Any]:
    marshalled = lark.JSON.marshal(data)
    if isinstance(marshalled, str):
        return json.loads(marshalled)
    if isinstance(marshalled, dict):
        return marshalled
    raise TypeError(f"Unsupported lark event payload: {type(marshalled)!r}")


async def main() -> None:
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def do_p2_im_message_receive_v1(data: lark.im.v1.P2ImMessageReceiveV1) -> None:
        payload = _event_to_dict(data)
        event = payload.get("event", {})
        future = asyncio.run_coroutine_threadsafe(handle_message_event(event), loop)

        def log_failure(done: asyncio.Future) -> None:
            exc = done.exception()
            if exc:
                print(f"failed to handle Feishu event: {exc}")

        future.add_done_callback(log_failure)

    def do_p2_card_action_trigger(data: P2CardActionTrigger) -> P2CardActionTriggerResponse:
        action = data.event.action if data.event else None
        value = action.value if action else {}

        response = P2CardActionTriggerResponse()
        response.toast = CallBackToast()

        if not isinstance(value, dict) or value.get("action") != "codex_approval":
            response.toast.type = "info"
            response.toast.content = "已忽略"
            return response

        approval_id = str(value.get("approval_id") or "")
        decision = str(value.get("decision") or "")

        future = asyncio.run_coroutine_threadsafe(
            handle_approval_action(approval_id, decision),
            loop,
        )
        try:
            ok = future.result(timeout=2.5)
        except Exception as exc:
            print(f"failed to handle Feishu card action: {exc}")
            response.toast.type = "error"
            response.toast.content = "处理失败"
            return response

        response.toast.type = "success" if ok else "error"
        response.toast.content = "已处理" if ok else "审批已失效"
        return response

    event_handler = (
        lark.EventDispatcherHandler.builder("", "")
        .register_p2_im_message_receive_v1(do_p2_im_message_receive_v1)
        .register_p2_card_action_trigger(do_p2_card_action_trigger)
        .build()
    )

    client = lark.ws.Client(
        settings.feishu_app_id,
        settings.feishu_app_secret,
        event_handler=event_handler,
        log_level=lark.LogLevel.INFO,
    )

    ws_task = asyncio.create_task(asyncio.to_thread(client.start))

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_event.set)

    print("Feishu WebSocket client started. Press Ctrl+C to stop.")
    await stop_event.wait()
    ws_task.cancel()
    await tasks.close()
    await feishu.close()


if __name__ == "__main__":
    asyncio.run(main())
