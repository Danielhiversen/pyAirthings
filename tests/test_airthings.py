from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from airthings import Airthings


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


def make_airthings() -> tuple[Airthings, MagicMock]:
    websession = MagicMock()
    websession.post = AsyncMock(
        return_value=MockResponse(text_data='{"access_token": "token"}')
    )
    return Airthings("client_id", "secret", websession), websession


@pytest.mark.asyncio
async def test_update_devices_returns_device_data() -> None:
    airthings, websession = make_airthings()
    websession.get = AsyncMock(
        side_effect=[
            MockResponse(json_data={"locations": [{"id": "loc1", "name": "Living room"}]}),
            MockResponse(
                json_data={
                    "devices": [
                        {
                            "id": "dev1",
                            "deviceType": "WAVE_PLUS",
                            "productName": "Wave+",
                        }
                    ]
                }
            ),
            MockResponse(
                json_data={
                    "devices": [
                        {
                            "id": "dev1",
                            "segment": {"name": "Wave+", "isActive": True},
                            "data": {"temp": 20.0},
                        }
                    ]
                }
            ),
        ]
    )

    result = await airthings.update_devices()

    assert result["dev1"].device_id == "dev1"
    assert result["dev1"].location_name == "Living room"
    assert result["dev1"].sensors["temp"] == 20.0


@pytest.mark.asyncio
async def test_update_devices_recovers_when_latest_samples_device_has_no_id() -> None:
    airthings, websession = make_airthings()
    websession.get = AsyncMock(
        side_effect=[
            MockResponse(json_data={"locations": [{"id": "loc1", "name": "Old"}]}),
            MockResponse(
                json_data={
                    "devices": [
                        {
                            "id": "dev1",
                            "deviceType": "WAVE_PLUS",
                            "productName": "Wave+",
                        }
                    ]
                }
            ),
            MockResponse(
                json_data={
                    "devices": [
                        {
                            "id": "dev1",
                            "segment": {"name": "Device 1", "isActive": True},
                            "data": {"temp": 20.0},
                        }
                    ]
                }
            ),
            MockResponse(
                json_data={
                    "devices": [
                        {
                            "segment": {"name": "Device 1", "isActive": True},
                            "data": {"temp": 21.0},
                        }
                    ]
                }
            ),
            MockResponse(json_data={"locations": [{"id": "loc2", "name": "New"}]}),
            MockResponse(
                json_data={
                    "devices": [
                        {
                            "id": "dev1",
                            "deviceType": "WAVE_PLUS",
                            "productName": "Wave+",
                        }
                    ]
                }
            ),
            MockResponse(
                json_data={
                    "devices": [
                        {
                            "id": "dev1",
                            "segment": {"name": "Device 1", "isActive": True},
                            "data": {"temp": 21.0},
                        }
                    ]
                }
            ),
        ]
    )

    first_result = await airthings.update_devices()
    assert first_result["dev1"].location_name == "Old"

    second_result = await airthings.update_devices()

    assert second_result["dev1"].location_name == "New"
    assert second_result["dev1"].sensors["temp"] == 21.0


@pytest.mark.asyncio
async def test_update_devices_recovers_when_device_metadata_is_stale() -> None:
    airthings, websession = make_airthings()
    websession.get = AsyncMock(
        side_effect=[
            MockResponse(json_data={"locations": [{"id": "loc1", "name": "Living room"}]}),
            MockResponse(
                json_data={
                    "devices": [
                        {
                            "id": "dev1",
                            "deviceType": "WAVE_PLUS",
                            "productName": "Wave+",
                        }
                    ]
                }
            ),
            MockResponse(
                json_data={
                    "devices": [
                        {
                            "id": "dev1",
                            "segment": {"name": "Wave+", "isActive": True},
                            "data": {"temp": 20.0},
                        }
                    ]
                }
            ),
            MockResponse(
                json_data={
                    "devices": [
                        {
                            "id": "dev2",
                            "segment": {"name": "View Plus", "isActive": True},
                            "data": {"temp": 21.0},
                        }
                    ]
                }
            ),
            MockResponse(json_data={"locations": [{"id": "loc1", "name": "Living room"}]}),
            MockResponse(
                json_data={
                    "devices": [
                        {
                            "id": "dev1",
                            "deviceType": "WAVE_PLUS",
                            "productName": "Wave+",
                        },
                        {
                            "id": "dev2",
                            "deviceType": "VIEW_PLUS",
                            "productName": "View Plus",
                        },
                    ]
                }
            ),
            MockResponse(
                json_data={
                    "devices": [
                        {
                            "id": "dev2",
                            "segment": {"name": "View Plus", "isActive": True},
                            "data": {"temp": 21.0},
                        }
                    ]
                }
            ),
        ]
    )

    first_result = await airthings.update_devices()
    assert first_result["dev1"].product_name == "Wave+"

    second_result = await airthings.update_devices()

    assert second_result["dev2"].device_id == "dev2"
    assert second_result["dev2"].product_name == "View Plus"
    assert second_result["dev2"].sensors["temp"] == 21.0


@pytest.mark.asyncio
async def test_fetch_devices_handles_missing_or_empty_device_entries() -> None:
    airthings, websession = make_airthings()
    websession.get = AsyncMock(
        side_effect=[
            MockResponse(json_data={"devices": None}),
            MockResponse(
                json_data={
                    "devices": [
                        {
                            "deviceType": "WAVE_PLUS",
                        }
                    ]
                }
            ),
        ]
    )

    await airthings._fetch_devices()
    assert airthings._devices == {}

    await airthings._fetch_devices()
    assert airthings._devices == {}
