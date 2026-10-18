"""The Home Assistant client: a slow lock must read as a refusal, not a crash."""

from __future__ import annotations

import httpx

from app.ha import SERVICE_TIMEOUT, HaClient


class _Cfg:
    ha_token = "token"


class _BrokenHttp:
    """Stands in for the httpx client when the call never gets an answer."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def post(self, _url: str, **kwargs) -> httpx.Response:
        self.calls.append(kwargs)
        raise httpx.ReadTimeout("the lock took too long")


class _QuietHttp:
    """Answers, but records what the client asked for."""

    def __init__(self, status: int = 200, body: dict | None = None) -> None:
        self.status = status
        self.body = body or {}
        self.calls: list[dict] = []

    async def post(self, _url: str, **kwargs) -> httpx.Response:
        self.calls.append(kwargs)
        return httpx.Response(self.status, json=self.body)


def _client() -> HaClient:
    client = HaClient.__new__(HaClient)
    client._cfg = _Cfg()
    return client


async def test_a_service_timeout_is_a_refusal_not_an_exception() -> None:
    """A slow clear must become a 502 in the endpoint, not a FastAPI 500.

    Measured live: `hemnyckel.clear_slot` outlasted the client's 15 s default,
    and the app showed "Reläet svarade inte som väntat" while Home Assistant
    went on to finish the clear.
    """
    client = _client()
    client._http = _BrokenHttp()

    assert await client.call_service("hemnyckel", "clear_slot", {"slot": 14}) is False


async def test_a_service_call_gets_the_long_timeout() -> None:
    client = _client()
    http = _BrokenHttp()
    client._http = http

    await client.call_service("hemnyckel", "clear_slot", {"slot": 14})

    assert http.calls[0]["timeout"] == SERVICE_TIMEOUT
    assert SERVICE_TIMEOUT > 15


async def test_a_returning_service_call_also_waits_for_the_lock() -> None:
    client = _client()
    http = _QuietHttp(200, {"service_response": {}})
    client._http = http

    accepted, body = await client.call_service_result("hemnyckel", "list_guests", {})

    assert accepted is True
    assert body == {}
    assert http.calls[0]["timeout"] == SERVICE_TIMEOUT
