"""Explicit media transport policy; LAN-only never opens a relay session."""

from collections.abc import Callable

from .keypad_lan_session import KeypadLanSession
from .keypad_session import KeypadSession
from .keypad_state import KeypadPower
from .models import MyQCamera
from .tend_protocol import TendStreamError

POLICIES = ("lan_only", "lan_preferred", "cloud_relay")
DEFAULT_POLICY = "lan_only"


async def open_transport(
    policy: str,
    camera: MyQCamera,
    token: str,
    on_frame: Callable[[bytes], None],
    *,
    on_power: Callable[[KeypadPower], None],
    on_phase: Callable[[str], None],
    on_finished: Callable[[str | None], None],
    on_fallback: Callable[[], None],
) -> KeypadSession:
    if policy not in POLICIES:
        raise TendStreamError("Unknown keypad transport policy")

    async def open_one(implementation: type[KeypadSession]) -> KeypadSession:
        return await implementation.open(
            camera, token, on_frame, on_power=on_power, on_phase=on_phase, on_finished=on_finished
        )

    if policy == "cloud_relay":
        return await open_one(KeypadSession)
    try:
        return await open_one(KeypadLanSession)
    except TendStreamError, OSError, ValueError, TimeoutError:
        if policy != "lan_preferred":
            raise
    on_fallback()
    return await open_one(KeypadSession)
