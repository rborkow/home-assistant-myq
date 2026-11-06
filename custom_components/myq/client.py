from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Mapping
from typing import cast

from aiohttp import ClientSession

from .const import (
    ACCOUNTS_BASE_URL,
    APP_VERSION,
    BRAND_ID,
    DEVICES_BASE_URL,
    GARAGE_DEVICES_BASE_URL,
    TEND_API_BASE_URL,
    TEND_LOCALE,
    TEND_PARTNER_ID,
    USER_AGENT,
)
from .exceptions import MyQApiError, MyQAuthenticationError
from .keypad_wake import async_wake_keypad
from .models import GarageDoor, MyQAccount, MyQCamera, MyQDevices
from .oauth import MyQAuth

type HeadersFn = Callable[[str], dict[str, str]]


class MyQClient:
    def __init__(self, session: ClientSession, auth: MyQAuth) -> None:
        self._session = session
        self._auth = auth
        # Cameras attached to no door (e.g. the Video Keypad), from the last door fetch.
        self.unattached_cameras: tuple[MyQCamera, ...] = ()

    async def async_access_token(self) -> str:
        """Return the current OAuth token for Tend camera signaling."""
        return await self._auth.async_access_token()

    async def async_prepare_keypad(self, camera: MyQCamera) -> None:
        """Prepare only a battery keypad, retaining this client's refresh ownership."""
        if camera.device_model != "vkp-camera":
            return

        async def request(method: str, url: str, body: dict[str, object] | None) -> bytes:
            return await self._async_request(
                method,
                url,
                _tend_headers,
                json_body=body,
                allow_redirects=False,
                max_body_bytes=1_024_000,
            )

        await async_wake_keypad(camera.tend_device_id, request)

    async def async_get_accounts(self) -> tuple[MyQAccount, ...]:
        payload = await self._async_request_json(
            "GET",
            f"{ACCOUNTS_BASE_URL}/api/v6.0/accounts",
        )
        raw_accounts = payload.get("accounts")
        if not isinstance(raw_accounts, list):
            raise MyQApiError("MyQ account discovery returned no account list")

        accounts: list[MyQAccount] = []
        for raw_account in raw_accounts:
            if not isinstance(raw_account, dict):
                raise MyQApiError("MyQ returned an invalid account")
            account = cast(dict[str, object], raw_account)
            account_id = account.get("id")
            name = account.get("name")
            if not isinstance(account_id, str):
                raise MyQApiError("MyQ returned an account without an ID")
            accounts.append(
                MyQAccount(
                    account_id=account_id,
                    name=name if isinstance(name, str) else account_id,
                )
            )
        return tuple(accounts)

    async def async_get_garage_doors(self) -> tuple[GarageDoor, ...]:
        """Return all garage doors; also refreshes ``unattached_cameras``."""
        accounts = await self.async_get_accounts()
        groups = await asyncio.gather(
            *(self._async_get_account_devices(account) for account in accounts)
        )
        self.unattached_cameras = tuple(camera for group in groups for camera in group.cameras)
        return tuple(door for group in groups for door in group.doors)

    async def async_open_door(self, door: GarageDoor) -> None:
        await self._async_command(door, "open")

    async def async_close_door(self, door: GarageDoor) -> None:
        await self._async_command(door, "close")

    async def async_get_camera_image(self, camera: MyQCamera) -> bytes:
        """Return the latest JPEG image supplied by Tend."""
        image = await self._async_request(
            "GET",
            f"{TEND_API_BASE_URL}/devices/{camera.tend_device_id}/cam/recentImage.jpg",
            _tend_headers,
        )
        if not image.startswith(b"\xff\xd8"):
            raise MyQApiError("Tend returned a camera snapshot that is not a JPEG")
        return image

    async def _async_get_account_devices(
        self,
        account: MyQAccount,
    ) -> MyQDevices:
        payload = await self._async_request_json(
            "GET",
            f"{DEVICES_BASE_URL}/api/v6.2/Accounts/{account.account_id}/Devices",
        )
        raw_items = payload.get("items")
        if not isinstance(raw_items, list):
            raise MyQApiError("MyQ device discovery returned no item list")

        door_items: list[dict[str, object]] = []
        cameras: dict[str, MyQCamera] = {}
        for raw_item in raw_items:
            if not isinstance(raw_item, dict):
                raise MyQApiError("MyQ returned an invalid device")
            item = cast(dict[str, object], raw_item)
            match item.get("device_family"):
                case "garagedoor":
                    door_items.append(item)
                case "camera":
                    serial_number = item.get("serial_number")
                    if isinstance(serial_number, str):
                        model = item.get("device_model")
                        cameras[serial_number] = MyQCamera(
                            serial_number,
                            _online(item.get("state")),
                            model if isinstance(model, str) else None,
                        )
        doors = tuple(_garage_door(account.account_id, item, cameras) for item in door_items)
        attached = {door.camera.serial_number for door in doors if door.camera is not None}
        return MyQDevices(
            doors=doors,
            cameras=tuple(camera for serial, camera in cameras.items() if serial not in attached),
        )

    async def _async_command(self, door: GarageDoor, command: str) -> None:
        url = (
            f"{GARAGE_DEVICES_BASE_URL}/api/v6.0/accounts/{door.account_id}/"
            f"door_openers/{door.serial_number}/{command}"
        )
        await self._async_request("PUT", url)

    async def _async_request_json(self, method: str, url: str) -> dict[str, object]:
        body = await self._async_request(method, url)
        try:
            parsed = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise MyQApiError("MyQ returned an invalid JSON response") from error
        if not isinstance(parsed, dict):
            raise MyQApiError("MyQ returned an unexpected JSON response")
        return cast(dict[str, object], parsed)

    async def _async_request(
        self,
        method: str,
        url: str,
        headers: HeadersFn | None = None,
        *,
        json_body: dict[str, object] | None = None,
        allow_redirects: bool | None = None,
        max_body_bytes: int | None = None,
    ) -> bytes:
        access_token = await self._auth.async_access_token()
        request_headers = (headers or _api_headers)(access_token)
        if allow_redirects is None:
            request = self._session.request(method, url, headers=request_headers)
        else:
            request = self._session.request(
                method,
                url,
                headers=request_headers,
                json=json_body,
                allow_redirects=allow_redirects,
            )
        async with request as response:
            if max_body_bytes is None:
                body = await response.read()
            else:
                received = bytearray()
                async for chunk in response.content.iter_chunked(65536):
                    received.extend(chunk)
                    if len(received) > max_body_bytes:
                        raise MyQApiError("MyQ response exceeds size limit")
                body = bytes(received)
            if response.status in {401, 403}:
                raise MyQAuthenticationError(f"MyQ returned HTTP {response.status}")
            if response.status >= (300 if allow_redirects is False else 400):
                raise MyQApiError(f"MyQ returned HTTP {response.status}")
            return body


def _online(raw_state: object) -> bool | None:
    if not isinstance(raw_state, dict):
        return None
    online = cast(dict[str, object], raw_state).get("online")
    return online if isinstance(online, bool) else None


def _garage_door(
    account_id: str,
    item: Mapping[str, object],
    cameras: Mapping[str, MyQCamera],
) -> GarageDoor:
    serial_number = item.get("serial_number")
    name = item.get("name")
    model = item.get("device_model")
    raw_state = item.get("state")
    if not isinstance(serial_number, str):
        raise MyQApiError("MyQ returned a garage door without a serial number")
    if raw_state is not None and not isinstance(raw_state, dict):
        raise MyQApiError("MyQ returned an invalid garage door state")
    state = cast(dict[str, object], raw_state or {})
    door_state = state.get("door_state")
    online = state.get("online")
    battery_backup_state = state.get("battery_backup_state")
    in_vacation_mode = state.get("in_vacation_mode")
    attached_worklight_on = state.get("attached_worklight_on")
    raw_fault_codes = state.get("active_fault_codes")
    absolute_cycle_count = state.get("absolute_cycle_count")
    service_cycle_count = state.get("service_cycle_count")
    last_device_activation_source = state.get("last_device_activation_source")
    camera_serial_number = state.get("attached_camera_serial_number")
    fault_codes = (
        tuple(code for code in raw_fault_codes if isinstance(code, str))
        if isinstance(raw_fault_codes, list)
        else ()
    )
    return GarageDoor(
        account_id=account_id,
        serial_number=serial_number,
        name=name if isinstance(name, str) else serial_number,
        device_model=model if isinstance(model, str) else None,
        door_state=door_state if isinstance(door_state, str) else None,
        online=online if isinstance(online, bool) else None,
        battery_backup_state=(
            battery_backup_state if isinstance(battery_backup_state, str) else None
        ),
        in_vacation_mode=(in_vacation_mode if isinstance(in_vacation_mode, bool) else None),
        attached_worklight_on=(
            attached_worklight_on if isinstance(attached_worklight_on, bool) else None
        ),
        active_fault_codes=fault_codes,
        absolute_cycle_count=(
            absolute_cycle_count
            if isinstance(absolute_cycle_count, int) and not isinstance(absolute_cycle_count, bool)
            else None
        ),
        service_cycle_count=(
            service_cycle_count
            if isinstance(service_cycle_count, int) and not isinstance(service_cycle_count, bool)
            else None
        ),
        last_device_activation_source=(
            last_device_activation_source
            if isinstance(last_device_activation_source, str)
            else None
        ),
        camera=(
            cameras.get(camera_serial_number, MyQCamera(camera_serial_number))
            if isinstance(camera_serial_number, str) and camera_serial_number
            else None
        ),
    )


def _api_headers(access_token: str) -> dict[str, str]:
    return {
        "Accept": "application/json",
        "App-Version": APP_VERSION,
        "Authorization": f"Bearer {access_token}",
        "BrandId": BRAND_ID,
        "User-Agent": USER_AGENT,
    }


def _tend_headers(access_token: str) -> dict[str, str]:
    return {
        **_api_headers(access_token),
        "AntiCSRF": "AntiCSRF",
        "Authorization": f"Bearer {access_token}",
        "SeedonkLocale": TEND_LOCALE,
        "partnerId": TEND_PARTNER_ID,
    }
