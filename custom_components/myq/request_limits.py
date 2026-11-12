"""Honor server-requested pauses separately for each cloud service."""

import math
from collections.abc import Mapping
from email.utils import parsedate_to_datetime
from time import monotonic, time
from urllib.parse import urlsplit

from .const import RATE_LIMIT_RETRY_INTERVAL
from .exceptions import MyQRateLimitError


class RequestCooldown:
    """Prevent new requests to a service until its cooldown expires."""

    def __init__(self) -> None:
        self._retry_at: dict[str, float] = {}

    def check(self, url: str) -> None:
        """Raise locally while the requested service is paused."""
        remaining = self._retry_at.get(urlsplit(url).netloc, 0.0) - monotonic()
        if remaining > 0:
            raise MyQRateLimitError(remaining)

    def check_response(self, url: str, status: int, headers: Mapping[str, str]) -> None:
        """Record rate limits and explicit service-unavailable retry delays."""
        if status != 429 and not (status == 503 and "Retry-After" in headers):
            return
        host = urlsplit(url).netloc
        now = monotonic()
        self._retry_at[host] = max(
            self._retry_at.get(host, 0.0), now + _retry_delay(headers.get("Retry-After"))
        )
        raise MyQRateLimitError(self._retry_at[host] - now)


def _retry_delay(value: str | None) -> float:
    minimum = RATE_LIMIT_RETRY_INTERVAL.total_seconds()
    if value is None:
        return minimum
    try:
        seconds = float(value)
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(value)
            if retry_at.tzinfo is None:
                return minimum
            seconds = retry_at.timestamp() - time()
        except ValueError, TypeError, OverflowError:
            return minimum
    return max(minimum, seconds) if math.isfinite(seconds) else minimum
