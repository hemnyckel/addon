from __future__ import annotations

import asyncio

import httpx
import jwt
import pytest

from app.apns import ApnsClient

from .conftest import DEVICE_TOKEN

PAYLOAD = {"aps": {"alert": {"title": "Ytterdörren"}}}


def mock_handler(*responses):
    """A MockTransport handler replaying (status, json) pairs; records requests."""
    queue = list(responses)
    calls: list[httpx.Request] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        status, body = queue.pop(0) if queue else (200, {})
        return httpx.Response(status, json=body, request=request)

    return _handler, calls


async def send(cfg, handler, **kwargs):
    client = ApnsClient(cfg, transport=httpx.MockTransport(handler), backoff_base=0)
    await client.start()
    try:
        return await client.send(DEVICE_TOKEN, PAYLOAD, **kwargs)
    finally:
        await client.stop()


def test_delivers_and_signs_with_es256(cfg, apns_key):
    _, public_key = apns_key
    handler, calls = mock_handler((200, {}))
    result = asyncio.run(send(cfg, handler))

    assert result.ok and result.status == 200 and result.apns_id
    request = calls[0]
    assert request.url.host == "api.push.apple.com"
    assert request.url.path == f"/3/device/{DEVICE_TOKEN}"

    token = request.headers["authorization"].removeprefix("bearer ")
    header = jwt.get_unverified_header(token)
    assert header["alg"] == "ES256"
    assert header["kid"] == "ABC123DEFG"
    claims = jwt.decode(token, public_key, algorithms=["ES256"])
    assert claims["iss"] == "TEAM123456"
    assert isinstance(claims["iat"], int)


def test_sets_topic_type_priority_and_options(cfg):
    handler, calls = mock_handler((200, {}))
    asyncio.run(send(cfg, handler, expiration=1700000000, collapse_id="door-front-unlock"))

    headers = calls[0].headers
    assert headers["apns-topic"] == "se.hemnyckel.app"
    assert headers["apns-push-type"] == "alert"
    assert headers["apns-priority"] == "10"
    assert headers["apns-expiration"] == "1700000000"
    assert headers["apns-collapse-id"] == "door-front-unlock"
    assert headers["apns-id"]


def test_topic_override(cfg):
    cfg.apns_topic = "se.hemnyckel.app.voip"
    handler, calls = mock_handler((200, {}))
    asyncio.run(send(cfg, handler))
    assert calls[0].headers["apns-topic"] == "se.hemnyckel.app.voip"


def test_sandbox_host_for_development(cfg):
    handler, calls = mock_handler((200, {}))
    asyncio.run(send(cfg, handler, env="development"))
    assert calls[0].url.host == "api.sandbox.push.apple.com"


@pytest.mark.parametrize(
    "status,reason",
    [(410, "Unregistered"), (400, "BadDeviceToken"), (400, "DeviceTokenNotForTopic")],
)
def test_dead_token_is_flagged_for_pruning(cfg, status, reason):
    handler, calls = mock_handler((status, {"reason": reason}))
    result = asyncio.run(send(cfg, handler, max_attempts=1))

    assert not result.ok
    assert result.invalidate_token
    assert result.reason == reason
    assert len(calls) == 1


def test_expired_provider_token_mints_a_new_one(cfg, monkeypatch):
    import app.apns as apns

    ticks = iter(range(1_000, 2_000))
    monkeypatch.setattr(apns, "_now", lambda: float(next(ticks)))

    handler, calls = mock_handler((403, {"reason": "ExpiredProviderToken"}), (200, {}))
    result = asyncio.run(send(cfg, handler))

    assert result.ok
    assert len(calls) == 2
    claims = [
        jwt.decode(
            c.headers["authorization"].removeprefix("bearer "),
            options={"verify_signature": False},
        )
        for c in calls
    ]
    assert claims[0]["iat"] != claims[1]["iat"]  # a genuinely new provider token


def test_retryable_status_then_success(cfg):
    handler, calls = mock_handler((503, {}), (200, {}))
    result = asyncio.run(send(cfg, handler))
    assert result.ok
    assert len(calls) == 2


def test_retryable_status_gives_up(cfg):
    handler, calls = mock_handler((503, {}), (503, {}), (503, {}))
    result = asyncio.run(send(cfg, handler))
    assert not result.ok
    assert result.retryable and result.status == 503
    assert len(calls) == 3


def test_payload_too_large_is_not_retried_or_pruned(cfg):
    handler, calls = mock_handler((413, {"reason": "PayloadTooLarge"}))
    result = asyncio.run(send(cfg, handler))
    assert not result.ok
    assert not result.retryable
    assert not result.invalidate_token
    assert len(calls) == 1


def test_provider_token_is_cached_between_sends(cfg):
    handler, calls = mock_handler((200, {}), (200, {}))

    async def go():
        client = ApnsClient(cfg, transport=httpx.MockTransport(handler), backoff_base=0)
        await client.start()
        try:
            await client.send(DEVICE_TOKEN, PAYLOAD)
            await client.send(DEVICE_TOKEN, PAYLOAD)
        finally:
            await client.stop()

    asyncio.run(go())

    assert len(calls) == 2
    # A byte-identical bearer token means it was minted once and reused (ES256
    # signatures are randomised, so two mints would never produce the same bytes).
    assert calls[0].headers["authorization"] == calls[1].headers["authorization"]


def test_dev_mode_without_a_key_logs_instead(tmp_path):
    from app.config import Config

    client = ApnsClient(Config(data_dir=str(tmp_path)))  # no key configured
    result = asyncio.run(client.send(DEVICE_TOKEN, PAYLOAD))
    assert result.ok
    assert result.status is None
