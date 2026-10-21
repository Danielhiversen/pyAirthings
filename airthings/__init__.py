"""Support for Airthings sensor."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import logging
import time

from aiohttp import ClientError

_LOGGER = logging.getLogger(__name__)

API_URL = "https://ext-api.airthings.com/v1/"
TIMEOUT = 10
CACHE_TTL = 3600


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


class AirthingsNotFoundError(AirthingsError):
    """Requested resource does not exist or is no longer accessible."""


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
        self._cache_updated = None

    async def _fetch_locations(self):
        response = await self._request(API_URL + "locations")
        json_data = await response.json()
        self._locations = [
            AirthingsLocation.init_from_response(location)
            for location in json_data.get("locations") or []
        ]

    async def _fetch_devices(self):
        response = await self._request(API_URL + "devices")
        json_data = await response.json()
        self._devices = {}
        devices = json_data.get("devices")
        if not isinstance(devices, list):
            _LOGGER.debug("Missing or empty devices payload")
            return
        for device in devices:
            if not isinstance(device, dict) or "id" not in device:
                _LOGGER.debug("Skipping device metadata entry without id")
                continue
            self._devices[device["id"]] = device

    async def _refresh_cache(self):
        await self._fetch_locations()
        await self._fetch_devices()
        self._cache_updated = time.monotonic()

    def _cache_expired(self):
        return (
            self._cache_updated is None
            or time.monotonic() - self._cache_updated > CACHE_TTL
        )

    async def update_devices(self):
        """Update data."""
        refreshed = self._cache_expired()
        if refreshed:
            await self._refresh_cache()
        res = await self._fetch_latest_samples(skip_stale=refreshed)
        if res is None:
            _LOGGER.debug("Cached locations or devices are stale, refreshing")
            await self._refresh_cache()
            res = await self._fetch_latest_samples(skip_stale=True)
        return res

    async def _fetch_latest_samples(self, skip_stale):
        """Return None if the cache is stale and skip_stale is False."""
        res = {}
        for location in self._locations:
            if not location.location_id:
                continue
            try:
                response = await self._request(
                    API_URL + f"/locations/{location.location_id}/latest-samples"
                )
            except AirthingsNotFoundError:
                if not skip_stale:
                    return None
                _LOGGER.debug("Skipping inaccessible location '%s'", location.name)
                continue
            json_data = await response.json()
            devices = (json_data or {}).get("devices")
            if not devices:
                _LOGGER.debug("No devices in location '%s'", location.name)
                continue
            for device in devices:
                if not isinstance(device, dict) or "id" not in device:
                    _LOGGER.debug("Skipping device without id in location '%s'", location.name)
                    continue
                device_id = device["id"]
                if device_id not in self._devices:
                    if not skip_stale:
                        return None
                    _LOGGER.debug(
                        "Skipping device '%s' missing from cached metadata", device_id
                    )
                    continue
                res[device_id] = AirthingsDevice.init_from_response(
                    device,
                    location.name,
                    self._devices[device_id],
                )
        return res

    async def _request(self, url, retry=3):
        _LOGGER.debug("Request %s %s", url, retry)
        if self._access_token is None:
            self._access_token = await get_token(
                self._websession, self._client_id, self._secret
            )

        headers = {"Authorization": self._access_token}
        try:
            async with asyncio.timeout(TIMEOUT):
                response = await self._websession.get(url, headers=headers)
            if response.status in (403, 404):
                raise AirthingsNotFoundError(
                    f"No access to {url}, response: {response.status} {response.reason}"
                )
            if response.status != 200:
                if retry > 0 and response.status != 429:
                    self._access_token = None
                    return await self._request(url, retry=retry - 1)
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
                return await self._request(url, retry=retry - 1)
            _LOGGER.error("Timed out when connecting to Airthings")
            raise AirthingsError from err
        return response


async def get_token(websession, client_id, secret, retry=3, timeout=10):
    """Get token for Airthings."""
    try:
        async with asyncio.timeout(timeout):
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
    access_token = token_data.get("access_token")
    if not access_token:
        _LOGGER.error("Airthings: Token response did not contain an access token")
        raise AirthingsAuthError("Token response did not contain an access token")
    return access_token
