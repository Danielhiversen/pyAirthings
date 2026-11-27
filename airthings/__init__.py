"""Support for Airthings sensor."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import logging
import time

from aiohttp import ClientError

_LOGGER = logging.getLogger(__name__)

API_URL = "https://consumer-api.airthings.com/v1/"
TIMEOUT = 10
CACHE_TTL = 3600
MAX_PAGES = 100


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
        """Create a device from a sensors result and its device metadata."""
        serial_number = response["serialNumber"]
        sensors = {}
        for sensor in response.get("sensors") or []:
            if isinstance(sensor, dict) and sensor.get("sensorType"):
                sensors[sensor["sensorType"]] = sensor.get("value")
        if response.get("batteryPercentage") is not None:
            sensors["battery"] = response["batteryPercentage"]
        return cls(
            serial_number,
            device.get("name") or serial_number,
            sensors,
            True,
            location_name,
            device.get("type"),
            device.get("productName"),
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
        self._accounts: list[str] = []
        self._devices: dict[str, dict[str, dict]] = {}
        self._cache_updated = None
        self._unresolved_accounts: set[str] = set()
        self._unresolved_devices: set[tuple[str, str]] = set()

    async def _fetch_list(self, path, key):
        response = await self._request(f"{API_URL}{path}?_={time.time_ns()}")
        json_data = await response.json()
        items = json_data.get(key) if isinstance(json_data, dict) else None
        if not isinstance(items, list):
            raise AirthingsError(f"Invalid {key} response from Airthings")
        return items

    async def _fetch_accounts(self):
        items = await self._fetch_list("accounts", "accounts")
        accounts = []
        for account in items:
            if not isinstance(account, dict) or not account.get("id"):
                _LOGGER.debug("Skipping account without id")
                continue
            accounts.append(account["id"])
        if items and not accounts:
            raise AirthingsError("Invalid accounts response from Airthings")
        return accounts

    async def _fetch_devices(self, account_id):
        devices = {}
        for device in await self._fetch_list(f"accounts/{account_id}/devices", "devices"):
            if not isinstance(device, dict) or not device.get("serialNumber"):
                _LOGGER.debug("Skipping device metadata entry without serial number")
                continue
            devices[device["serialNumber"]] = device
        return devices

    async def _fetch_sensors(self, account_id):
        samples = {}
        for page in range(1, MAX_PAGES + 1):
            response = await self._request(
                API_URL + f"accounts/{account_id}/sensors?unit=metric&pageNumber={page}"
            )
            json_data = await response.json()
            results = json_data.get("results") if isinstance(json_data, dict) else None
            if not isinstance(results, list):
                raise AirthingsError("Invalid sensors response from Airthings")
            for result in results:
                if not isinstance(result, dict) or not result.get("serialNumber"):
                    _LOGGER.debug("Skipping sensors result without serial number")
                    continue
                samples[result["serialNumber"]] = result
            if json_data.get("hasNext") is not True or not results:
                return samples
        raise AirthingsError(f"More than {MAX_PAGES} pages of sensors from Airthings")

    async def _refresh_cache(self):
        accounts = await self._fetch_accounts()
        devices = {}
        accessible = []
        for account_id in accounts:
            try:
                devices[account_id] = await self._fetch_devices(account_id)
            except AirthingsNotFoundError:
                _LOGGER.debug("Skipping account without access to its devices")
                continue
            accessible.append(account_id)
        if accounts and not accessible:
            raise AirthingsError("No accessible Airthings account")
        self._accounts = accessible
        self._devices = devices
        self._unresolved_accounts = set()
        self._unresolved_devices = set()
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
            await self._refresh_cache()
            res = await self._fetch_latest_samples(skip_stale=True)
        return res

    async def _fetch_latest_samples(self, skip_stale):
        """Return None on a new unknown device or inaccessible account unless skip_stale."""
        res = {}
        accounts, devices = self._accounts, self._devices
        inaccessible = 0
        for account_id in accounts:
            try:
                samples = await self._fetch_sensors(account_id)
            except AirthingsNotFoundError as err:
                if not skip_stale and account_id not in self._unresolved_accounts:
                    _LOGGER.debug("Account is not accessible (%s), refreshing cache", err)
                    return None
                self._unresolved_accounts.add(account_id)
                _LOGGER.debug("Skipping inaccessible account")
                inaccessible += 1
                continue
            for serial_number, sample in samples.items():
                device = devices[account_id].get(serial_number)
                if device is None:
                    if not skip_stale and (account_id, serial_number) not in self._unresolved_devices:
                        _LOGGER.debug("Unknown device '%s', refreshing cache", serial_number)
                        return None
                    self._unresolved_devices.add((account_id, serial_number))
                    _LOGGER.debug("Skipping device '%s' missing from the device list", serial_number)
                    continue
                if device.get("sensors") == []:
                    continue
                res[serial_number] = AirthingsDevice.init_from_response(
                    sample, device.get("home") or "", device
                )
        if accounts and inaccessible == len(accounts):
            raise AirthingsError("No accessible Airthings account")
        return res

    async def _request(self, url, retry=3):
        _LOGGER.debug("Request %s %s", url, retry)
        if self._access_token is None:
            self._access_token = await get_token(
                self._websession, self._client_id, self._secret
            )

        headers = {"Authorization": f"Bearer {self._access_token}"}
        try:
            async with asyncio.timeout(TIMEOUT):
                response = await self._websession.get(url, headers=headers)
            if response.status != 200:
                response.release()
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
