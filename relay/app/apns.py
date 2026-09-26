"""Apple Push Notification client.

Uses a token-based (.p8) APNs key with a short-lived ES256 JWT. In development
(no key configured) pushes are logged instead of sent, so the whole pipeline can
be exercised without an Apple Developer account.
"""
from __future__ import annotations

import logging
import time

import httpx
import jwt

from .config import Config

_LOGGER = logging.getLogger("hemnyckel.apns")
_APNS_HOST = "https://api.push.apple.com"


class ApnsClient:
    def __init__(self, cfg: Config) -> None:
        self._cfg = cfg
        self._key: str | None = None
        self._token: str | None = None
        self._token_ts: float = 0.0
        self._client: httpx.AsyncClient | None = None
        if cfg.apns_configured:
            with open(cfg.apns_key_path, encoding="utf-8") as fh:
                self._key = fh.read()

    async def start(self) -> None:
        if self._key:
            self._client = httpx.AsyncClient(http2=True, timeout=15)

    async def stop(self) -> None:
        if self._client is not None:
            await self._client.aclose()

    def _jwt(self) -> str:
        now = time.time()
        if self._token is None or now - self._token_ts > 3000:
            self._token = jwt.encode(
                {"iss": self._cfg.apns_team_id, "iat": int(now)},
                self._key,
                algorithm="ES256",
                headers={"kid": self._cfg.apns_key_id},
            )
            self._token_ts = now
        return self._token

    async def send(self, device_token: str, payload: dict, *, push_type: str = "alert",
                   priority: int = 10) -> bool:
        if self._client is None:
            _LOGGER.info("[dev] push -> %s: %s", device_token[:8], payload)
            return True
        resp = await self._client.post(
            f"{_APNS_HOST}/3/device/{device_token}",
            json=payload,
            headers={
                "authorization": f"bearer {self._jwt()}",
                "apns-topic": self._cfg.bundle_id,
                "apns-push-type": push_type,
                "apns-priority": str(priority),
            },
        )
        if resp.status_code >= 400:
            _LOGGER.warning("APNs %s: %s", resp.status_code, resp.text[:200])
            return False
        return True
