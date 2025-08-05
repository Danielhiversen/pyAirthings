"""Support for Airthings sensor."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import logging

from aiohttp import ClientError
import async_timeout

_LOGGER = logging.getLogger(__name__)

API_URL = "https://ext-api.airthings.com/v1/"
TIMEOUT = 10


@dataclass
class AirthingsLocation:
    """Airthings location."""
    location_id: str
    name: str

    @classmethod
    def init_from_response(cls, response):
        """Class method."""
        return cls(
            response.get("id"),
            response.get("name"),
        )


@dataclass
class AirthingsDevice:
    """Airthings device."""
    device_id: str
    name: str
    sensors: dict[str, float | None]
    is_active: bool
    location_name: str
    device_type: str | None
    product_name: str | None

    @classmethod
    def init_from_response(cls, response, location_name, device):
        """Class method."""
        return cls(
            response.get("id"),
            response.get("segment").get("name"),
            response.get("data"),
            response.get("segment").get("isActive"),
            location_name,
            device.get('deviceType'),
            device.get('productName'),
        )

    @property
    def sensor_types(self):
        """Sensor types."""
        return self.sensors.keys()


class AirthingsError(Exception):
    """General Airthings exception occurred."""


class AirthingsConnectionError(AirthingsError):
    """ConnectionError Airthings occurred."""


class AirthingsAuthError(AirthingsError):
    """AirthingsAuthError Airthings occurred."""


class Airthings:
    """Airthings data handler."""

    def __init__(self, client_id, secret, websession):
        """Init Airthings data handler."""
        self._client_id = client_id
        self._secret = secret
        self._websession = websession
        self._access_token = None
        self._locations = []
        self._devices = {}

    async def update_devices(self):
        """Update data."""
        if not self._locations:
            response = await self._request(API_URL + "locations")
            json_data = await response.json()
            self._locations = []
            for location in json_data.get("locations"):
                self._locations.append(AirthingsLocation.init_from_response(location))
        if not self._devices:
            response = await self._request(API_URL + "devices")
            json_data = await response.json()
            self._devices = {}
            for device in json_data.get("devices"):
                self._devices[device['id']] = device
        res = {}
        for location in self._locations:
            if not location.location_id:
                continue
            response = await self._request(
                API_URL + f"/locations/{location.location_id}/latest-samples"
            )
            if response is None:
                continue
            json_data = await response.json()
            if json_data is None:
                continue
            if devices := json_data.get("devices"):
                for device in devices:
                    device_id = device.get('id')
                    res[device_id] = AirthingsDevice.init_from_response(
                        device,
                        location.name,
                        self._devices.get(device_id)
                    )
            else:
                _LOGGER.debug("No devices in location '%s'", location.name)
        return res

    async def _request(self, url, json_data=None, retry=3):
        _LOGGER.debug("Request %s %s, %s", url, retry, json_data)
        if self._access_token is None:
            self._access_token = await get_token(
                self._websession, self._client_id, self._secret
            )
            if self._access_token is None:
                return None

        headers = {"Authorization": self._access_token}
        try:
            async with async_timeout.timeout(TIMEOUT):
                if json_data:
                    response = await self._websession.post(
                        url, json=json_data, headers=headers
                    )
                else:
                    response = await self._websession.get(url, headers=headers)
            if response.status != 200:
                if retry > 0 and response.status != 429:
                    self._access_token = None
                    return await self._request(url, json_data, retry=retry - 1)
                _LOGGER.error(
                    "Error connecting to Airthings, response: %s %s",
                    response.status,
                    response.reason,
                )
                raise AirthingsError(
                    f"Error connecting to Airthings, response: {response.reason}"
                )
        except ClientError as err:
            self._access_token = None
            _LOGGER.error("Error connecting to Airthings: %s ", err, exc_info=True)
            raise AirthingsError from err
        except asyncio.TimeoutError as err:
            self._access_token = None
            if retry > 0:
                return await self._request(url, json_data, retry=retry - 1)
            _LOGGER.error("Timed out when connecting to Airthings")
            raise AirthingsError from err
        return response


async def get_token(websession, client_id, secret, retry=3, timeout=10):
    """Get token for Airthings."""
    try:
        async with async_timeout.timeout(timeout):
            response = await websession.post(
                "https://accounts-api.airthings.com/v1/token",
                headers={
                    "Content-type": "application/x-www-form-urlencoded",
                    "Accept": "application/json",
                },
                data={
                    "grant_type": "client_credentials",
                    "client_id": client_id,
                    "client_secret": secret,
                },
            )
    except ClientError as err:
        if retry > 0:
            return await get_token(websession, client_id, secret, retry - 1, timeout)
        _LOGGER.error("Error getting token Airthings: %s ", err, exc_info=True)
        raise AirthingsConnectionError from err
    except asyncio.TimeoutError as err:
        if retry > 0:
            return await get_token(websession, client_id, secret, retry - 1, timeout)
        _LOGGER.error("Timed out when connecting to Airthings for token")
        raise AirthingsConnectionError from err
    if response.status != 200:
        _LOGGER.error(
            "Airthings: Failed to login to retrieve token: %s %s",
            response.status,
            response.reason,
        )
        raise AirthingsAuthError(f"Failed to login to retrieve token {response.reason}")
    token_data = json.loads(await response.text())
    return token_data.get("access_token")
