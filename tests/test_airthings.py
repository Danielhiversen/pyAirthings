from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

from aiohttp import ClientError
import pytest

from airthings import (
    API_URL,
    CACHE_TTL,
    Airthings,
    AirthingsAuthError,
    AirthingsConnectionError,
    AirthingsError,
    AirthingsNotFoundError,
    get_token,
)


class MockResponse:
    def __init__(
        self,
        *,
        json_data: dict | None = None,
        text_data: str | None = None,
        status: int = 200,
        reason: str = "OK",
    ) -> None:
        self._json_data = json_data
        self._text_data = text_data
        self.status = status
        self.reason = reason

    async def json(self) -> dict | None:
        return self._json_data

    async def text(self) -> str:
        return self._text_data or ""


def token_response(token: str = "token") -> MockResponse:
    return MockResponse(text_data=json.dumps({"access_token": token}))


PRODUCT_NAMES = {"WAVE_PLUS": "Wave Plus", "VIEW_PLUS": "View Plus"}


class FakeCloud:
    """In-memory ext-api account that routes GET requests by URL."""

    def __init__(self) -> None:
        self.locations = {"loc1": "Home"}
        self.devices = {"dev1": {"location": "loc1", "name": "Kitchen", "type": "WAVE_PLUS"}}
        self.extra_samples: dict[str, list] = {}
        self.forbidden: set[str] = set()
        self.urls: list[str] = []

    async def get(self, url: str, headers: dict) -> MockResponse:
        self.urls.append(url)
        if url == API_URL + "locations":
            return MockResponse(
                json_data={"locations": [{"id": i, "name": n} for i, n in self.locations.items()]}
            )
        if url == API_URL + "devices":
            return MockResponse(
                json_data={
                    "devices": [
                        {"id": i, "deviceType": d["type"], "productName": PRODUCT_NAMES[d["type"]]}
                        for i, d in self.devices.items()
                    ]
                }
            )
        location_id = url.split("/locations/")[1].split("/")[0]
        if location_id not in self.locations or location_id in self.forbidden:
            return MockResponse(status=403, reason="Forbidden")
        samples = [
            {
                "id": i,
                "segment": {"name": d["name"], "isActive": True},
                "data": {"temp": 20.0},
            }
            for i, d in self.devices.items()
            if d["location"] == location_id
        ]
        return MockResponse(json_data={"devices": samples + self.extra_samples.get(location_id, [])})

    def count(self, suffix: str) -> int:
        return sum(url.endswith(suffix) for url in self.urls)


def make_airthings(cloud: FakeCloud | None = None) -> tuple[Airthings, MagicMock]:
    websession = MagicMock()
    websession.post = AsyncMock(return_value=token_response())
    if cloud is not None:
        websession.get = AsyncMock(side_effect=cloud.get)
    return Airthings("client_id", "secret", websession), websession


@pytest.fixture
def clock():
    now = [1000.0]
    with patch("airthings.time.monotonic", side_effect=lambda: now[0]):
        yield now


@pytest.mark.asyncio
async def test_update_devices_returns_device_data() -> None:
    cloud = FakeCloud()
    airthings, websession = make_airthings(cloud)

    result = await airthings.update_devices()

    device = result["dev1"]
    assert device.device_id == "dev1"
    assert device.name == "Kitchen"
    assert device.sensors == {"temp": 20.0}
    assert list(device.sensor_types) == ["temp"]
    assert device.is_active is True
    assert device.location_name == "Home"
    assert device.device_type == "WAVE_PLUS"
    assert device.product_name == "Wave Plus"
    websession.get.assert_any_call(API_URL + "locations", headers={"Authorization": "token"})


@pytest.mark.asyncio
async def test_update_devices_reuses_cache_and_token(clock) -> None:
    cloud = FakeCloud()
    airthings, websession = make_airthings(cloud)

    await airthings.update_devices()
    await airthings.update_devices()

    assert cloud.count("/locations") == 1
    assert cloud.count("/devices") == 1
    assert cloud.count("/latest-samples") == 2
    assert websession.post.await_count == 1


@pytest.mark.asyncio
async def test_update_devices_refreshes_cache_after_ttl(clock) -> None:
    cloud = FakeCloud()
    airthings, _ = make_airthings(cloud)
    await airthings.update_devices()

    cloud.locations["loc1"] = "Renamed"
    cloud.locations["loc2"] = "Cabin"
    cloud.devices["dev2"] = {"location": "loc2", "name": "Office", "type": "VIEW_PLUS"}
    clock[0] += CACHE_TTL
    result = await airthings.update_devices()
    assert "dev2" not in result
    assert result["dev1"].location_name == "Home"

    clock[0] += 1
    result = await airthings.update_devices()
    assert result["dev1"].location_name == "Renamed"
    assert result["dev2"].location_name == "Cabin"
    assert cloud.count("/locations") == 2


@pytest.mark.asyncio
async def test_update_devices_refreshes_when_device_is_added(clock) -> None:
    cloud = FakeCloud()
    airthings, _ = make_airthings(cloud)
    await airthings.update_devices()

    cloud.devices["dev2"] = {"location": "loc1", "name": "Office", "type": "VIEW_PLUS"}
    result = await airthings.update_devices()

    assert set(result) == {"dev1", "dev2"}
    assert result["dev2"].product_name == "View Plus"
    assert cloud.count("/devices") == 2

    await airthings.update_devices()
    assert cloud.count("/devices") == 2


@pytest.mark.asyncio
async def test_update_devices_refreshes_when_location_is_deleted(clock) -> None:
    cloud = FakeCloud()
    cloud.locations["loc2"] = "Cabin"
    cloud.devices["dev2"] = {"location": "loc2", "name": "Office", "type": "VIEW_PLUS"}
    airthings, _ = make_airthings(cloud)
    await airthings.update_devices()

    del cloud.locations["loc1"]
    del cloud.devices["dev1"]
    result = await airthings.update_devices()

    assert set(result) == {"dev2"}
    assert cloud.count("/locations") == 2
    assert cloud.count("/loc1/latest-samples") == 2

    await airthings.update_devices()
    assert cloud.count("/loc1/latest-samples") == 2


@pytest.mark.asyncio
async def test_update_devices_removes_deleted_device(clock) -> None:
    cloud = FakeCloud()
    cloud.devices["dev2"] = {"location": "loc1", "name": "Office", "type": "VIEW_PLUS"}
    airthings, _ = make_airthings(cloud)
    await airthings.update_devices()

    del cloud.devices["dev2"]
    result = await airthings.update_devices()

    assert set(result) == {"dev1"}
    assert cloud.count("/locations") == 1


@pytest.mark.asyncio
async def test_update_devices_skips_location_still_inaccessible_after_refresh(clock) -> None:
    cloud = FakeCloud()
    cloud.locations["loc2"] = "Cabin"
    airthings, _ = make_airthings(cloud)
    await airthings.update_devices()

    cloud.forbidden.add("loc1")
    result = await airthings.update_devices()

    assert result == {}
    assert cloud.count("/locations") == 2


@pytest.mark.asyncio
async def test_update_devices_skips_inaccessible_location_on_fresh_cache(clock) -> None:
    cloud = FakeCloud()
    cloud.forbidden.add("loc1")
    airthings, _ = make_airthings(cloud)

    result = await airthings.update_devices()

    assert result == {}
    assert cloud.count("/locations") == 1


@pytest.mark.asyncio
async def test_update_devices_skips_device_unknown_after_refresh(clock) -> None:
    cloud = FakeCloud()
    airthings, _ = make_airthings(cloud)
    await airthings.update_devices()

    cloud.extra_samples["loc1"] = [{"id": "ghost", "segment": {"name": "Ghost"}, "data": {}}]
    result = await airthings.update_devices()

    assert set(result) == {"dev1"}
    assert cloud.count("/devices") == 2


@pytest.mark.asyncio
async def test_update_devices_skips_device_without_id_without_refreshing(clock) -> None:
    cloud = FakeCloud()
    cloud.extra_samples["loc1"] = [{"segment": {"name": "No id"}, "data": {}}, "not a dict"]
    airthings, _ = make_airthings(cloud)
    await airthings.update_devices()

    result = await airthings.update_devices()

    assert set(result) == {"dev1"}
    assert cloud.count("/locations") == 1


@pytest.mark.asyncio
async def test_update_devices_skips_locations_without_id_or_devices() -> None:
    airthings, websession = make_airthings()
    websession.get = AsyncMock(
        side_effect=[
            MockResponse(
                json_data={
                    "locations": [
                        {"name": "No id"},
                        {"id": "empty", "name": "Empty"},
                        {"id": "null", "name": "Null"},
                    ]
                }
            ),
            MockResponse(json_data={"devices": []}),
            MockResponse(json_data={"devices": []}),
            MockResponse(json_data=None),
        ]
    )

    assert await airthings.update_devices() == {}
    assert websession.get.await_count == 4


@pytest.mark.asyncio
async def test_fetch_handles_missing_or_malformed_payloads() -> None:
    airthings, websession = make_airthings()
    websession.get = AsyncMock(
        side_effect=[
            MockResponse(json_data={"locations": None}),
            MockResponse(json_data={"devices": None}),
            MockResponse(json_data={"devices": [{"deviceType": "WAVE_PLUS"}, "not a dict"]}),
        ]
    )

    await airthings._fetch_locations()
    assert airthings._locations == []

    await airthings._fetch_devices()
    assert airthings._devices == {}

    await airthings._fetch_devices()
    assert airthings._devices == {}


@pytest.mark.asyncio
async def test_request_refreshes_token_and_retries_on_unauthorized() -> None:
    airthings, websession = make_airthings()
    websession.post = AsyncMock(side_effect=[token_response("old"), token_response("new")])
    ok = MockResponse(json_data={})
    websession.get = AsyncMock(side_effect=[MockResponse(status=401, reason="Unauthorized"), ok])

    assert await airthings._request("url") is ok
    assert websession.get.await_args_list[-1].kwargs["headers"] == {"Authorization": "new"}


@pytest.mark.asyncio
async def test_request_raises_after_retries_are_exhausted() -> None:
    airthings, websession = make_airthings()
    websession.get = AsyncMock(return_value=MockResponse(status=500, reason="Server Error"))

    with pytest.raises(AirthingsError, match="Server Error"):
        await airthings._request("url")
    assert websession.get.await_count == 4


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "error"),
    [(403, AirthingsNotFoundError), (404, AirthingsNotFoundError), (429, AirthingsError)],
)
async def test_request_does_not_retry(status: int, error: type[Exception]) -> None:
    airthings, websession = make_airthings()
    websession.get = AsyncMock(return_value=MockResponse(status=status, reason="Nope"))

    with pytest.raises(error):
        await airthings._request("url")
    assert websession.get.await_count == 1
    assert websession.post.await_count == 1


@pytest.mark.asyncio
async def test_request_raises_on_client_error() -> None:
    airthings, websession = make_airthings()
    websession.get = AsyncMock(side_effect=ClientError("boom"))

    with pytest.raises(AirthingsError):
        await airthings._request("url")
    assert airthings._access_token is None


@pytest.mark.asyncio
async def test_request_retries_on_timeout() -> None:
    airthings, websession = make_airthings()
    ok = MockResponse(json_data={})
    websession.get = AsyncMock(side_effect=[asyncio.TimeoutError, ok])

    assert await airthings._request("url") is ok
    assert websession.post.await_count == 2


@pytest.mark.asyncio
async def test_request_raises_when_timeouts_are_exhausted() -> None:
    airthings, websession = make_airthings()
    websession.get = AsyncMock(side_effect=asyncio.TimeoutError)

    with pytest.raises(AirthingsError):
        await airthings._request("url", retry=1)
    assert websession.get.await_count == 2


@pytest.mark.asyncio
async def test_get_token_returns_access_token() -> None:
    websession = MagicMock()
    websession.post = AsyncMock(return_value=token_response("abc"))

    assert await get_token(websession, "id", "secret") == "abc"
    assert websession.post.await_args.kwargs["data"] == {
        "grant_type": "client_credentials",
        "client_id": "id",
        "client_secret": "secret",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [ClientError("boom"), asyncio.TimeoutError()])
async def test_get_token_retries_then_succeeds(error: Exception) -> None:
    websession = MagicMock()
    websession.post = AsyncMock(side_effect=[error, token_response("abc")])

    assert await get_token(websession, "id", "secret") == "abc"


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [ClientError("boom"), asyncio.TimeoutError()])
async def test_get_token_raises_connection_error_when_retries_are_exhausted(
    error: Exception,
) -> None:
    websession = MagicMock()
    websession.post = AsyncMock(side_effect=error)

    with pytest.raises(AirthingsConnectionError):
        await get_token(websession, "id", "secret", retry=1)
    assert websession.post.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        MockResponse(status=401, reason="Unauthorized"),
        MockResponse(text_data=json.dumps({"error": "invalid_client"})),
    ],
)
async def test_get_token_raises_auth_error(response: MockResponse) -> None:
    websession = MagicMock()
    websession.post = AsyncMock(return_value=response)

    with pytest.raises(AirthingsAuthError):
        await get_token(websession, "id", "secret")
