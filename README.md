# pyAirthings
Python library for the [Airthings Consumer API](https://consumer-api-doc.airthings.com/).

Create an API client at https://consumer-api-doc.airthings.com/dashboard and use its id and secret:

```python
import asyncio

import aiohttp
from airthings import Airthings


async def main():
    async with aiohttp.ClientSession() as session:
        airthings = Airthings("client_id", "secret", session)
        devices = await airthings.update_devices()
    for serial_number, device in devices.items():
        print(serial_number, device.name, device.sensors)


asyncio.run(main())
```

`update_devices()` returns the devices on the account that have sensors, keyed by serial number, with the latest sensor values.

Example usage in Home Assistant:
https://github.com/home-assistant/core/tree/dev/homeassistant/components/airthings
