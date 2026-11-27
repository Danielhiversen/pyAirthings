from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import parse_qs, urlsplit

from aiohttp import ClientError
import pytest

from airthings import (
    API_URL,
    CACHE_TTL,
    Airthings,
    AirthingsAuthError,
    AirthingsConnectionError,
    AirthingsDevice,
    AirthingsError,
    AirthingsLocation,
    AirthingsNotFoundError,
    get_token,
)


class MockResponse:
    def __init__(
        self,
        *,
        json_data: dict | list | None = None,
        text_data: str | None = None,
        status: int = 200,
        reason: str = "OK",
    ) -> None:
        self._json_data = json_data
        self._text_data = text_data
        self.status = status
        self.reason = reason
        self.released = False

    async def json(self) -> dict | list | None:
        return self._json_data

    async def text(self) -> str:
        return self._text_data or ""

    def release(self) -> None:
        self.released = True


def token_response(token: str = "token") -> MockResponse:
    return MockResponse(text_data=json.dumps({"access_token": token}))


def endpoint(url: str) -> str:
    return urlsplit(url).path.rsplit("/", 1)[-1]


class FakeCloud:
    """In-memory Consumer API that routes GET requests by URL."""

    def __init__(self) -> None:
        self.accounts = ["acc1"]
        self.devices = {
            "2930000001": {
                "account": "acc1",
                "name": "Kitchen",
                "home": "Home",
                "type": "WAVE_PLUS",
                "productName": "Wave Plus",
            },
        }
        self.unlisted: set[str] = set()
        self.unsampled: set[str] = set()
        self.forbidden_sensors: set[str] = set()
        self.forbidden_devices: set[str] = set()
        self.page_size = 50
        self.urls: list[str] = []

    def result(self, serial: str) -> dict:
        return {
            "serialNumber": serial,
            "recorded": "2026-10-22T11:12:08",
            "batteryPercentage": 77,
            "sensors": [{"sensorType": "temp", "value": 20.5, "unit": "c"}],
        }

    async def get(self, url: str, headers: dict) -> MockResponse:
        self.urls.append(url)
        parts = urlsplit(url)
        path = parts.path.removeprefix(urlsplit(API_URL).path).split("/")
        query = parse_qs(parts.query)
        if path == ["accounts"]:
            assert "_" in query
            return MockResponse(json_data={"accounts": [{"id": a} for a in self.accounts]})
        account_id = path[1]
        serials = [s for s, d in self.devices.items() if d["account"] == account_id]
        if path[2] == "devices":
            assert "_" in query
            if account_id in self.forbidden_devices:
                return MockResponse(status=403, reason="Forbidden")
            return MockResponse(
                json_data={
                    "devices": [
                        {
                            "serialNumber": s,
                            "name": self.devices[s]["name"],
                            "home": self.devices[s]["home"],
                            "type": self.devices[s]["type"],
                            "productName": self.devices[s]["productName"],
                            "sensors": ["temp"],
                        }
                        for s in serials
                        if s not in self.unlisted
                    ]
                }
            )
        assert query["unit"] == ["metric"]
        if account_id in self.forbidden_sensors:
            return MockResponse(status=403, reason="Forbidden")
        page = int(query["pageNumber"][0])
        results = [self.result(s) for s in serials if s not in self.unsampled]
        chunk = results[(page - 1) * self.page_size : page * self.page_size]
        return MockResponse(
            json_data={"results": chunk, "hasNext": page * self.page_size < len(results)}
        )

    def count(self, part: str) -> int:
        return sum(part in url for url in self.urls)


def make_airthings(cloud: FakeCloud | None = None) -> tuple[Airthings, MagicMock]:
    websession = MagicMock()
    websession.post = AsyncMock(return_value=token_response())
    if cloud is not None:
        websession.get = AsyncMock(side_effect=cloud.get)
    return Airthings("client_id", "secret", websession), websession


def add_device(cloud: FakeCloud, serial: str, account: str = "acc1", **fields: str | None) -> None:
    cloud.devices[serial] = {
        "account": account,
        "name": "Basement",
        "home": "Home",
        "type": "WAVE_GEN2",
        "productName": "Wave",
    } | fields


@pytest.fixture
def clock():
    now = [1000.0]
    with patch("airthings.time.monotonic", side_effect=lambda: now[0]):
        yield now


@pytest.mark.asyncio
async def test_update_devices_returns_device_data() -> None:
    cloud = FakeCloud()
    airthings, websession = make_airthings(cloud)

    with patch("airthings.time.time_ns", return_value=42):
        result = await airthings.update_devices()

    assert result == {
        "2930000001": AirthingsDevice(
            device_id="2930000001",
            name="Kitchen",
            sensors={"temp": 20.5, "battery": 77},
            is_active=True,
            location_name="Home",
            device_type="WAVE_PLUS",
            product_name="Wave Plus",
        )
    }
    assert list(result["2930000001"].sensor_types) == ["temp", "battery"]
    assert websession.get.await_args.kwargs["headers"] == {"Authorization": "Bearer token"}
    assert cloud.urls == [
        API_URL + "accounts?_=42",
        API_URL + "accounts/acc1/devices?_=42",
        API_URL + "accounts/acc1/sensors?unit=metric&pageNumber=1",
    ]


@pytest.mark.asyncio
async def test_update_devices_bypasses_cached_device_list_on_every_refresh(clock) -> None:
    cloud = FakeCloud()
    airthings, _ = make_airthings(cloud)

    with patch("airthings.time.time_ns", side_effect=[1, 2, 3, 4]):
        await airthings.update_devices()
        clock[0] += CACHE_TTL + 1
        await airthings.update_devices()

    assert [url for url in cloud.urls if "/devices" in url] == [
        API_URL + "accounts/acc1/devices?_=2",
        API_URL + "accounts/acc1/devices?_=4",
    ]


@pytest.mark.asyncio
async def test_update_devices_reuses_cache_and_token(clock) -> None:
    cloud = FakeCloud()
    airthings, websession = make_airthings(cloud)

    await airthings.update_devices()
    cloud.urls.clear()
    clock[0] += CACHE_TTL
    await airthings.update_devices()

    assert cloud.urls == [API_URL + "accounts/acc1/sensors?unit=metric&pageNumber=1"]
    assert websession.post.await_count == 1


@pytest.mark.asyncio
async def test_update_devices_refreshes_cache_after_ttl(clock) -> None:
    cloud = FakeCloud()
    airthings, _ = make_airthings(cloud)

    await airthings.update_devices()
    cloud.devices["2930000001"] |= {"name": "Renamed", "home": "Cabin"}
    clock[0] += CACHE_TTL + 1
    result = await airthings.update_devices()

    assert result["2930000001"].name == "Renamed"
    assert result["2930000001"].location_name == "Cabin"
    assert cloud.count("/devices") == 2


@pytest.mark.asyncio
async def test_update_devices_combines_accounts() -> None:
    cloud = FakeCloud()
    cloud.accounts = ["acc1", "acc2"]
    add_device(cloud, "2960000002", "acc2", name="Office", home=None, type="VIEW_PLUS", productName="View Plus")
    airthings, _ = make_airthings(cloud)

    result = await airthings.update_devices()

    assert list(result) == ["2930000001", "2960000002"]
    assert result["2960000002"].location_name == ""
    assert result["2960000002"].product_name == "View Plus"


@pytest.mark.asyncio
async def test_update_devices_refreshes_when_device_is_added(clock, caplog) -> None:
    cloud = FakeCloud()
    airthings, _ = make_airthings(cloud)
    await airthings.update_devices()

    add_device(cloud, "2950000002")
    with caplog.at_level("DEBUG", logger="airthings"):
        result = await airthings.update_devices()

    assert result["2950000002"].name == "Basement"
    assert result["2950000002"].product_name == "Wave"
    assert cloud.count("/devices") == 2
    assert "Unknown device '2950000002', refreshing cache" in caplog.text


@pytest.mark.asyncio
async def test_update_devices_removes_deleted_device_without_refresh(clock) -> None:
    cloud = FakeCloud()
    add_device(cloud, "2950000002")
    airthings, _ = make_airthings(cloud)
    assert set(await airthings.update_devices()) == {"2930000001", "2950000002"}

    del cloud.devices["2950000002"]
    result = await airthings.update_devices()

    assert set(result) == {"2930000001"}
    assert cloud.count("/devices") == 1


@pytest.mark.asyncio
async def test_update_devices_skips_device_missing_from_device_list(clock, caplog) -> None:
    cloud = FakeCloud()
    airthings, _ = make_airthings(cloud)
    await airthings.update_devices()

    add_device(cloud, "2950000002")
    cloud.unlisted = {"2950000002"}
    with caplog.at_level("DEBUG", logger="airthings"):
        result = await airthings.update_devices()
        await airthings.update_devices()

    assert set(result) == {"2930000001"}
    assert cloud.count("/devices") == 2
    assert "Skipping device '2950000002' missing from the device list" in caplog.text

    cloud.unlisted = set()
    clock[0] += CACHE_TTL + 1
    assert set(await airthings.update_devices()) == {"2930000001", "2950000002"}


@pytest.mark.asyncio
async def test_update_devices_returns_device_without_readings() -> None:
    cloud = FakeCloud()
    airthings, websession = make_airthings(cloud)

    async def without_readings(url: str, headers: dict) -> MockResponse:
        response = await cloud.get(url, headers)
        if endpoint(url) == "sensors":
            response._json_data["results"][0] |= {"sensors": [], "recorded": None, "batteryPercentage": None}
        return response

    websession.get = AsyncMock(side_effect=without_readings)

    device = (await airthings.update_devices())["2930000001"]

    assert (device.name, device.sensors) == ("Kitchen", {})


@pytest.mark.asyncio
async def test_update_devices_skips_device_without_sensors(clock) -> None:
    cloud = FakeCloud()
    add_device(cloud, "2810000002", name="Hub", type="HUB")
    airthings, websession = make_airthings(cloud)

    async def hub_has_no_sensors(url: str, headers: dict) -> MockResponse:
        response = await cloud.get(url, headers)
        if endpoint(url) == "devices":
            response._json_data["devices"][1]["sensors"] = []
        return response

    websession.get = AsyncMock(side_effect=hub_has_no_sensors)

    result = await airthings.update_devices()
    await airthings.update_devices()

    assert set(result) == {"2930000001"}
    assert cloud.count("/devices") == 1


@pytest.mark.asyncio
async def test_update_devices_omits_listed_device_without_sensors_result() -> None:
    cloud = FakeCloud()
    add_device(cloud, "2950000002")
    cloud.unsampled = {"2950000002"}
    airthings, _ = make_airthings(cloud)

    assert set(await airthings.update_devices()) == {"2930000001"}
    assert cloud.count("/devices") == 1


@pytest.mark.asyncio
async def test_update_devices_refreshes_when_account_is_inaccessible(clock, caplog) -> None:
    cloud = FakeCloud()
    cloud.accounts = ["acc1", "acc2"]
    add_device(cloud, "2960000002", "acc2")
    airthings, _ = make_airthings(cloud)
    await airthings.update_devices()

    cloud.accounts = ["acc1"]
    cloud.forbidden_sensors = {"acc2"}
    with caplog.at_level("DEBUG", logger="airthings"):
        result = await airthings.update_devices()
    await airthings.update_devices()

    assert set(result) == {"2930000001"}
    assert cloud.count("/accounts?") == 2
    assert cloud.count("/accounts/acc2/sensors") == 2
    assert "Account is not accessible" in caplog.text


@pytest.mark.asyncio
async def test_update_devices_skips_account_without_access_to_devices(clock) -> None:
    cloud = FakeCloud()
    cloud.accounts = ["acc1", "acc2"]
    add_device(cloud, "2960000002", "acc2")
    cloud.forbidden_devices = {"acc2"}
    airthings, _ = make_airthings(cloud)

    result = await airthings.update_devices()
    await airthings.update_devices()

    assert set(result) == {"2930000001"}
    assert cloud.count("/accounts/acc2/sensors") == 0


@pytest.mark.asyncio
async def test_update_devices_skips_account_still_inaccessible_after_refresh(clock) -> None:
    cloud = FakeCloud()
    cloud.accounts = ["acc1", "acc2"]
    add_device(cloud, "2960000002", "acc2")
    airthings, _ = make_airthings(cloud)
    await airthings.update_devices()

    cloud.forbidden_sensors = {"acc2"}
    result = await airthings.update_devices()
    cloud.urls.clear()
    await airthings.update_devices()

    assert set(result) == {"2930000001"}
    assert cloud.count("/devices") == 0
    assert cloud.count("/accounts/acc2/sensors") == 1


@pytest.mark.asyncio
async def test_update_devices_skips_inaccessible_account_on_fresh_cache() -> None:
    cloud = FakeCloud()
    cloud.accounts = ["acc1", "acc2"]
    cloud.forbidden_sensors = {"acc2"}
    airthings, _ = make_airthings(cloud)

    result = await airthings.update_devices()

    assert set(result) == {"2930000001"}
    assert cloud.count("/accounts?") == 1


@pytest.mark.asyncio
async def test_update_devices_raises_when_no_account_has_access_to_devices() -> None:
    cloud = FakeCloud()
    cloud.forbidden_devices = {"acc1"}
    airthings, _ = make_airthings(cloud)

    with pytest.raises(AirthingsError, match="No accessible Airthings account"):
        await airthings.update_devices()

    cloud.forbidden_devices = set()
    assert set(await airthings.update_devices()) == {"2930000001"}


@pytest.mark.asyncio
async def test_update_devices_raises_when_no_account_has_access_to_sensors(clock) -> None:
    cloud = FakeCloud()
    airthings, _ = make_airthings(cloud)
    await airthings.update_devices()

    cloud.forbidden_sensors = {"acc1"}
    for _ in range(2):
        with pytest.raises(AirthingsError, match="No accessible Airthings account"):
            await airthings.update_devices()

    assert cloud.count("/accounts?") == 2
    cloud.forbidden_sensors = set()
    assert set(await airthings.update_devices()) == {"2930000001"}


@pytest.mark.asyncio
async def test_update_devices_follows_pages() -> None:
    cloud = FakeCloud()
    cloud.page_size = 1
    add_device(cloud, "2950000002")
    airthings, _ = make_airthings(cloud)

    result = await airthings.update_devices()

    assert set(result) == {"2930000001", "2950000002"}
    assert cloud.count("pageNumber=2") == 1
    assert cloud.count("pageNumber=3") == 0


@pytest.mark.asyncio
async def test_update_devices_stops_paging_on_empty_page() -> None:
    airthings, websession = make_airthings()
    payloads = {
        "accounts": {"accounts": [{"id": "acc1"}]},
        "devices": {"devices": []},
        "sensors": {"results": [], "hasNext": True},
    }
    websession.get = AsyncMock(side_effect=lambda url, headers: MockResponse(json_data=payloads[endpoint(url)]))

    assert await airthings.update_devices() == {}
    assert websession.get.await_count == 3


@pytest.mark.asyncio
async def test_update_devices_raises_when_pages_exceed_max_pages() -> None:
    cloud = FakeCloud()
    cloud.page_size = 1
    add_device(cloud, "2950000002")
    airthings, _ = make_airthings(cloud)

    with patch("airthings.MAX_PAGES", 1), pytest.raises(AirthingsError, match="More than 1 pages"):
        await airthings.update_devices()

    assert cloud.count("pageNumber=2") == 0


@pytest.mark.asyncio
async def test_update_devices_raises_when_a_later_page_fails(clock) -> None:
    cloud = FakeCloud()
    cloud.page_size = 1
    add_device(cloud, "2950000002")
    airthings, websession = make_airthings(cloud)
    await airthings.update_devices()
    cached = airthings._devices

    async def broken_page_two(url: str, headers: dict) -> MockResponse:
        if "pageNumber=2" in url:
            return MockResponse(json_data={"results": None})
        return await cloud.get(url, headers)

    websession.get = AsyncMock(side_effect=broken_page_two)
    with pytest.raises(AirthingsError, match="Invalid sensors"):
        await airthings.update_devices()

    assert airthings._devices is cached


@pytest.mark.asyncio
async def test_update_devices_refreshes_when_unresolved_device_moves_to_another_account(clock) -> None:
    cloud = FakeCloud()
    cloud.accounts = ["acc1", "acc2"]
    airthings, _ = make_airthings(cloud)
    await airthings.update_devices()

    add_device(cloud, "2950000002")
    cloud.unlisted = {"2950000002"}
    assert set(await airthings.update_devices()) == {"2930000001"}

    cloud.unlisted = set()
    cloud.devices["2950000002"]["account"] = "acc2"
    result = await airthings.update_devices()

    assert set(result) == {"2930000001", "2950000002"}
    assert cloud.count("/devices") == 6


@pytest.mark.asyncio
async def test_update_devices_uses_one_cache_snapshot_while_fetching(clock) -> None:
    cloud = FakeCloud()
    cloud.accounts = ["acc1", "acc2"]
    add_device(cloud, "2960000002", "acc2")
    airthings, websession = make_airthings(cloud)
    await airthings.update_devices()

    async def cache_replaced_mid_poll(url: str, headers: dict) -> MockResponse:
        if "/acc1/sensors" in url:
            airthings._accounts, airthings._devices = [], {}
        return await cloud.get(url, headers)

    websession.get = AsyncMock(side_effect=cache_replaced_mid_poll)

    assert set(await airthings.update_devices()) == {"2930000001", "2960000002"}


@pytest.mark.asyncio
async def test_update_devices_refreshes_when_device_moves_to_another_account(clock, caplog) -> None:
    cloud = FakeCloud()
    cloud.accounts = ["acc1", "acc2"]
    airthings, _ = make_airthings(cloud)
    await airthings.update_devices()

    cloud.devices["2930000001"] |= {"account": "acc2", "name": "Moved", "home": "Cabin"}
    with caplog.at_level("DEBUG", logger="airthings"):
        result = await airthings.update_devices()

    assert (result["2930000001"].name, result["2930000001"].location_name) == ("Moved", "Cabin")
    assert cloud.count("/devices") == 4
    assert "Unknown device '2930000001', refreshing cache" in caplog.text


@pytest.mark.asyncio
async def test_update_devices_skips_malformed_entries(clock) -> None:
    cloud = FakeCloud()
    airthings, websession = make_airthings(cloud)

    async def with_malformed(url: str, headers: dict) -> MockResponse:
        response = await cloud.get(url, headers)
        data = response._json_data
        name = endpoint(url)
        if name == "accounts":
            data["accounts"] += ["acc2", {"name": "no id"}]
        elif name == "devices":
            data["devices"] += ["2950000002", {"name": "no serial"}]
        else:
            data["results"] += [None, {"sensors": []}]
            data["results"][0]["sensors"] += [None, {"value": 1.0}, {"sensorType": "co2", "value": None}]
        return response

    websession.get = AsyncMock(side_effect=with_malformed)

    result = await airthings.update_devices()
    await airthings.update_devices()

    assert list(result) == ["2930000001"]
    assert result["2930000001"].sensors == {"temp": 20.5, "co2": None, "battery": 77}
    assert cloud.count("/devices") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "payload", "error"),
    [
        ("accounts", [], "Invalid accounts"),
        ("accounts", {"accounts": None}, "Invalid accounts"),
        ("accounts", {"accounts": [{"name": "no id"}]}, "Invalid accounts"),
        ("devices", {"devices": {}}, "Invalid devices"),
    ],
)
async def test_update_devices_keeps_cache_on_malformed_metadata(clock, name, payload, error) -> None:
    cloud = FakeCloud()
    airthings, websession = make_airthings(cloud)
    await airthings.update_devices()
    cached = airthings._devices

    async def broken(url: str, headers: dict) -> MockResponse:
        if endpoint(url) == name:
            return MockResponse(json_data=payload)
        return await cloud.get(url, headers)

    websession.get = AsyncMock(side_effect=broken)
    clock[0] += CACHE_TTL + 1
    with pytest.raises(AirthingsError, match=error):
        await airthings.update_devices()

    assert airthings._devices is cached


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [{"results": None}, []])
async def test_update_devices_raises_on_malformed_sensors(payload) -> None:
    cloud = FakeCloud()
    airthings, websession = make_airthings(cloud)

    async def broken(url: str, headers: dict) -> MockResponse:
        if endpoint(url) == "sensors":
            return MockResponse(json_data=payload)
        return await cloud.get(url, headers)

    websession.get = AsyncMock(side_effect=broken)
    with pytest.raises(AirthingsError, match="Invalid sensors"):
        await airthings.update_devices()


@pytest.mark.asyncio
async def test_update_devices_keeps_whole_cache_when_refresh_fails(clock) -> None:
    cloud = FakeCloud()
    airthings, websession = make_airthings(cloud)
    await airthings.update_devices()
    cached = (airthings._accounts, airthings._devices)

    cloud.accounts = ["acc1", "acc2"]

    async def broken_acc2(url: str, headers: dict) -> MockResponse:
        if "/acc2/devices" in url:
            return MockResponse(json_data={})
        return await cloud.get(url, headers)

    websession.get = AsyncMock(side_effect=broken_acc2)
    clock[0] += CACHE_TTL + 1
    with pytest.raises(AirthingsError, match="Invalid devices"):
        await airthings.update_devices()

    assert (airthings._accounts, airthings._devices) == cached
    assert airthings._accounts is cached[0]


@pytest.mark.parametrize(
    ("device", "expected"),
    [({"productName": "Corentium Home 2"}, "Corentium Home 2"), ({}, None)],
)
def test_device_product_name(device: dict, expected: str | None) -> None:
    result = AirthingsDevice.init_from_response({"serialNumber": "1"}, "", device)

    assert result.product_name == expected


def test_device_without_metadata_or_readings() -> None:
    device = AirthingsDevice.init_from_response({"serialNumber": "1", "batteryPercentage": None}, "", {})

    assert device.sensors == {}
    assert device.name == "1"


def test_location_init_from_response() -> None:
    assert AirthingsLocation.init_from_response({"id": "loc", "name": "Home"}) == AirthingsLocation("loc", "Home")


@pytest.mark.asyncio
async def test_update_devices_maps_every_device_type(snapshot) -> None:
    payloads = json.loads((Path(__file__).parent / "fixtures" / "account.json").read_text())
    airthings, websession = make_airthings()
    websession.get = AsyncMock(side_effect=lambda url, headers: MockResponse(json_data=payloads[endpoint(url)]))

    assert await airthings.update_devices() == snapshot


@pytest.mark.asyncio
async def test_request_refreshes_token_and_retries_on_unauthorized() -> None:
    airthings, websession = make_airthings()
    websession.post = AsyncMock(side_effect=[token_response("old"), token_response("new")])
    ok = MockResponse(json_data={})
    websession.get = AsyncMock(side_effect=[MockResponse(status=401, reason="Unauthorized"), ok])

    assert await airthings._request("url") is ok
    assert websession.get.await_args_list[-1].kwargs["headers"] == {"Authorization": "Bearer new"}


@pytest.mark.asyncio
async def test_request_raises_after_retries_are_exhausted() -> None:
    airthings, websession = make_airthings()
    response = MockResponse(status=500, reason="Server Error")
    response.release = MagicMock()
    websession.get = AsyncMock(return_value=response)

    with pytest.raises(AirthingsError, match="Server Error"):
        await airthings._request("url")
    assert websession.get.await_count == 4
    assert response.release.call_count == 4


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "error"),
    [(403, AirthingsNotFoundError), (404, AirthingsNotFoundError), (429, AirthingsError)],
)
async def test_request_does_not_retry(status: int, error: type[Exception]) -> None:
    airthings, websession = make_airthings()
    response = MockResponse(status=status, reason="Nope")
    websession.get = AsyncMock(return_value=response)

    with pytest.raises(error):
        await airthings._request("url")
    assert websession.get.await_count == 1
    assert websession.post.await_count == 1
    assert response.released


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
    assert websession.post.await_args.args == ("https://accounts-api.airthings.com/v1/token",)
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
