from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

import httpx


FEISHU_BASE_URL = "https://open.feishu.cn/open-apis"


@dataclass
class TenantToken:
    value: str
    expires_at: float


class FeishuClient:
    def __init__(self, app_id: str, app_secret: str) -> None:
        self._app_id = app_id
        self._app_secret = app_secret
        self._token: TenantToken | None = None
        self._client = httpx.AsyncClient(base_url=FEISHU_BASE_URL, timeout=20)

    async def close(self) -> None:
        await self._client.aclose()

    async def reply_card(self, message_id: str, card: dict[str, Any]) -> str:
        response = await self._request(
            "POST",
            f"/im/v1/messages/{message_id}/reply",
            json={"msg_type": "interactive", "content": json.dumps(card, ensure_ascii=False)},
        )
        return response.get("data", {}).get("message_id", "")

    async def update_card(self, message_id: str, card: dict[str, Any]) -> None:
        await self._request(
            "PATCH",
            f"/im/v1/messages/{message_id}",
            json={"content": json.dumps(card, ensure_ascii=False)},
        )

    async def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        token = await self._tenant_access_token()
        headers = kwargs.pop("headers", {})
        headers["Authorization"] = f"Bearer {token}"

        response = await self._client.request(method, path, headers=headers, **kwargs)
        try:
            payload = response.json()
        except ValueError:
            payload = {"raw": response.text}
        if response.is_error:
            raise RuntimeError(f"Feishu HTTP {response.status_code} error on {method} {path}: {payload}")
        if payload.get("code", 0) != 0:
            raise RuntimeError(f"Feishu API error: {payload}")
        return payload

    async def _tenant_access_token(self) -> str:
        now = time.time()
        if self._token and self._token.expires_at > now + 60:
            return self._token.value

        response = await self._client.post(
            "/auth/v3/tenant_access_token/internal",
            json={"app_id": self._app_id, "app_secret": self._app_secret},
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("code", 0) != 0:
            raise RuntimeError(f"Failed to get tenant_access_token: {payload}")

        expire = int(payload.get("expire", 7200))
        token = payload["tenant_access_token"]
        self._token = TenantToken(value=token, expires_at=now + expire)
        return token
