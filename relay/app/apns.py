"""Apple Push Notification client (token-based: .p8 key + ES256 JWT).

Production-grade:
  * sandbox and production hosts, chosen per device;
  * a cached provider JWT, refreshed on a timer and again on Apple's
    `ExpiredProviderToken`, inside Apple's 20-60 minute window;
  * per-push identifiers, `apns-collapse-id` and `apns-expiration`;
  * a classified result, so the caller can prune a dead device token and
    retry only what is worth retrying.

In development (no key configured) pushes are logged instead of sent, so the
whole pipeline can be exercised without an Apple Developer account.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass
from typing import Any

import httpx
import jwt

from .config import Config

_LOGGER = logging.getLogger("hemnyckel.apns")

# Apple: refresh the provider token at least once an hour, but no more than once
# every 20 minutes. 45 minutes sits comfortably inside both bounds.
_TOKEN_TTL = 45 * 60

HOSTS = {
    "development": "https://api.sandbox.push.apple.com",
    "production": "https://api.push.apple.com",
}

# Reasons that mean the *device* token is dead: stop sending until it registers
# a new one.
_DEAD_TOKEN_REASONS = frozenset(
    {"BadDeviceToken", "DeviceTokenNotForTopic", "Unregistered"}
)
# Reasons that mean our *provider* token is stale: mint a new one and retry.
_STALE_PROVIDER_REASONS = frozenset({"ExpiredProviderToken"})
# Statuses worth retrying at the transport level.
_RETRYABLE_STATUSES = frozenset({429, 500, 503})


@dataclass(frozen=True)
class PushResult:
    """Outcome of one push, classified for the caller."""

    ok: bool
    status: int | None = None
    reason: str | None = None
    apns_id: str | None = None
    invalidate_token: bool = False
    retryable: bool = False

    @classmethod
    def delivered(cls, apns_id: str) -> PushResult:
        return cls(ok=True, status=200, apns_id=apns_id)


def _now() -> float:
    """Seam for tests: the current wall-clock time."""
    return time.time()


def _short(token: str) -> str:
    return token[:8]


def _reason(resp: httpx.Response) -> str | None:
    try:
        return resp.json().get("reason")
    except ValueError:
        return None


def _retry_delay(resp: httpx.Response, attempt: int, base: float) -> float:
    """Prefer Apple's Retry-After, otherwise exponential backoff."""
    header = resp.headers.get("retry-after")
    if header:
        try:
            return max(0.0, float(header))
        except ValueError:
            pass
    return base * (2 ** (attempt - 1))


class ApnsClient:
    def __init__(
        self,
        cfg: Config,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        backoff_base: float = 2.0,
    ) -> None:
        self._cfg = cfg
        self._transport = transport
        self._backoff_base = backoff_base
        self._key: str | None = None
        self._client: httpx.AsyncClient | None = None
        self._token: str | None = None
        self._token_ts: float = 0.0
        self._token_lock = asyncio.Lock()
        if cfg.apns_configured:
            try:
                self._key = cfg.apns_key_material()
            except OSError as exc:
                # A bad path should not take the whole relay down: log loudly
                # and fall back to dev mode (pushes logged, not sent).
                _LOGGER.error("APNs key not readable (%s) — running in dev mode", exc)
                self._key = None

    # -- lifecycle ----------------------------------------------------------
    async def start(self) -> None:
        if self._key and self._client is None:
            self._client = httpx.AsyncClient(
                http2=True,
                timeout=httpx.Timeout(15.0, connect=10.0),
                transport=self._transport,
            )

    async def stop(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    @property
    def live(self) -> bool:
        """True when a real key is configured and the client is running."""
        return self._client is not None

    # -- provider token -----------------------------------------------------
    def _mint(self) -> str:
        iat = int(_now())
        return jwt.encode(
            {"iss": self._cfg.apns_team_id, "iat": iat},
            self._key,
            algorithm="ES256",
            headers={"kid": self._cfg.apns_key_id},
        )

    async def _provider_token(self, *, force: bool = False) -> str:
        async with self._token_lock:
            now = _now()
            if force or self._token is None or now - self._token_ts > _TOKEN_TTL:
                self._token = self._mint()
                self._token_ts = now
            return self._token

    # -- send ---------------------------------------------------------------
    async def send(
        self,
        device_token: str,
        payload: dict[str, Any],
        *,
        env: str = "production",
        push_type: str = "alert",
        priority: int = 10,
        expiration: int | None = None,
        collapse_id: str | None = None,
        topic: str | None = None,
        max_attempts: int = 3,
    ) -> PushResult:
        # Apple requires apns-id to be a canonical UUID string (8-4-4-4-12).
        # A hyphen-less .hex value is rejected with 400 BadMessageId.
        apns_id = str(uuid.uuid4())
        if self._client is None:
            _LOGGER.info(
                "[dev] push %s -> %s… type=%s payload=%s",
                apns_id, _short(device_token), push_type, payload,
            )
            return PushResult(ok=True, status=None, apns_id=apns_id)

        host = HOSTS.get(env, HOSTS["production"])
        url = f"{host}/3/device/{device_token}"
        topic = topic or self._cfg.topic

        stale = False
        for attempt in range(1, max_attempts + 1):
            headers = {
                "authorization": f"bearer {await self._provider_token(force=stale)}",
                "apns-topic": topic,
                "apns-push-type": push_type,
                "apns-priority": str(priority),
                "apns-id": apns_id,
            }
            if expiration is not None:
                headers["apns-expiration"] = str(expiration)
            if collapse_id:
                headers["apns-collapse-id"] = collapse_id

            try:
                resp = await self._client.post(url, json=payload, headers=headers)
            except httpx.HTTPError as exc:
                if attempt < max_attempts:
                    await asyncio.sleep(self._backoff_base * (2 ** (attempt - 1)))
                    continue
                _LOGGER.warning("APNs transport error for %s…: %s", _short(device_token), exc)
                return PushResult(ok=False, reason="TransportError", apns_id=apns_id, retryable=True)

            if resp.status_code == 200:
                return PushResult.delivered(apns_id)

            reason = _reason(resp)
            stale = reason in _STALE_PROVIDER_REASONS

            if stale and attempt < max_attempts:
                _LOGGER.info("APNs provider token expired; minting a new one (id=%s)", apns_id)
                continue
            if resp.status_code in _RETRYABLE_STATUSES and attempt < max_attempts:
                delay = _retry_delay(resp, attempt, self._backoff_base)
                _LOGGER.info("APNs %s; retrying in %.1fs (id=%s)", resp.status_code, delay, apns_id)
                await asyncio.sleep(delay)
                continue

            invalidate = reason in _DEAD_TOKEN_REASONS or resp.status_code == 410
            if invalidate:
                _LOGGER.warning(
                    "APNs %s %s for %s… — pruning device token (id=%s)",
                    resp.status_code, reason, _short(device_token), apns_id,
                )
            else:
                _LOGGER.warning(
                    "APNs %s %s for %s… (id=%s)",
                    resp.status_code, reason, _short(device_token), apns_id,
                )
            return PushResult(
                ok=False,
                status=resp.status_code,
                reason=reason,
                apns_id=apns_id,
                invalidate_token=invalidate,
                retryable=resp.status_code in _RETRYABLE_STATUSES,
            )

        # Unreachable: every branch above returns. Kept for exhaustiveness.
        return PushResult(ok=False, reason="Exhausted", apns_id=apns_id, retryable=True)
