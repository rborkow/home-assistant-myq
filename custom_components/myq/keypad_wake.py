"""Bounded on-demand keypad wake; no background polling or credential ownership."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable

from .const import TEND_API_BASE_URL
from .exceptions import MyQApiError

_WAKE_TIMEOUT = 35
_POLL_INTERVAL = 1

type WakeRequest = Callable[[str, str, dict[str, object] | None], Awaitable[bytes]]


async def async_wake_keypad(device_id: str, request: WakeRequest) -> None:
    """Use the caller's authenticated request lifecycle to reach full power."""
    if not device_id.isdecimal():
        raise MyQApiError("Invalid keypad video identifier")
    endpoint = f"{TEND_API_BASE_URL}/devices/{device_id}"
    try:
        async with asyncio.timeout(_WAKE_TIMEOUT):
            await request("POST", f"{endpoint}/do/wakeup?timestamp={int(time.time() * 1000)}", {})
            while True:
                body = await request("GET", f"{endpoint}?timestamp={int(time.time() * 1000)}", None)
                if len(body) > 1_024_000:
                    raise MyQApiError("Keypad power response exceeds size limit")
                try:
                    device = json.loads(body)
                except UnicodeDecodeError, json.JSONDecodeError:
                    raise MyQApiError("Keypad returned an invalid power response") from None
                if not isinstance(device, dict):
                    raise MyQApiError("Keypad returned an invalid power response")
                status = device.get("status")
                power = status.get("pwr") if isinstance(status, dict) else None
                if isinstance(power, dict) and power.get("sleepMode") == "FP":
                    return
                await asyncio.sleep(_POLL_INTERVAL)
    except TimeoutError:
        raise MyQApiError("Keypad did not reach full power in time") from None
