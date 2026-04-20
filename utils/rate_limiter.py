# Python 3.11+
# utils/rate_limiter.py — Sliding-window rate limiter and exponential-backoff
# retry decorator for Mist API calls.
#
# Mist enforces a limit of 5000 requests/hour per org. This module tracks
# all outbound requests in a deque-based sliding window and blocks callers
# before the limit is hit. HTTP 429 responses trigger exponential backoff
# with full jitter, up to MAX_RETRIES attempts.

import time
import random
import functools
from collections import deque
from threading import Lock
from typing import Callable, Any

import requests

from utils.logger import get_logger

logger = get_logger(__name__)

# --------------------------------------------------------------------------- #
# Constants — all sourced from Mist documentation / conservative headroom
# --------------------------------------------------------------------------- #

WINDOW_SECONDS: int = 3600          # 1-hour sliding window
MAX_REQUESTS_PER_WINDOW: int = 4800 # 5000 limit; 200-request safety buffer
MAX_RETRIES: int = 3                # maximum 429 retry attempts
BASE_BACKOFF_SECONDS: float = 1.0   # initial backoff before jitter
MAX_BACKOFF_SECONDS: float = 32.0   # ceiling for any single wait


# --------------------------------------------------------------------------- #
# RateLimiter
# --------------------------------------------------------------------------- #

class RateLimiter:
    """Thread-safe sliding-window rate limiter for Mist API requests.

    Tracks the timestamps of outbound requests in a rolling deque. Before
    each request, expired entries (older than WINDOW_SECONDS) are evicted
    and the current count is checked against MAX_REQUESTS_PER_WINDOW.  If
    the window is full, the caller is blocked until enough entries expire.

    Parameters
    ----------
    max_requests : int
        Maximum requests allowed within the window. Defaults to
        ``MAX_REQUESTS_PER_WINDOW``.
    window_seconds : int
        Length of the sliding window in seconds. Defaults to
        ``WINDOW_SECONDS``.

    Examples
    --------
    >>> limiter = RateLimiter()
    >>> limiter.acquire()   # blocks if at capacity, then records the call
    """

    def __init__(
        self,
        max_requests: int = MAX_REQUESTS_PER_WINDOW,
        window_seconds: int = WINDOW_SECONDS,
    ) -> None:
        self._max_requests = max_requests
        self._window_seconds = window_seconds
        self._timestamps: deque[float] = deque()
        self._lock = Lock()

    # ---------------------------------------------------------------------- #
    # Public interface
    # ---------------------------------------------------------------------- #

    def acquire(self) -> None:
        """Block until a request slot is available, then claim it.

        Evicts timestamps outside the current window, then either records
        the current time immediately (slot available) or sleeps until the
        oldest in-window timestamp expires (window full).

        Parameters
        ----------
        None.

        Returns
        -------
        None
        """
        with self._lock:
            while True:
                now = time.monotonic()
                self._evict(now)

                if len(self._timestamps) < self._max_requests:
                    self._timestamps.append(now)
                    logger.debug(
                        "Rate limiter: slot acquired",
                        extra={
                            "requests_in_window": len(self._timestamps),
                            "capacity": self._max_requests,
                        },
                    )
                    return

                # Window is full — calculate how long until the oldest slot
                # expires and sleep for that duration.
                oldest = self._timestamps[0]
                sleep_for = self._window_seconds - (now - oldest) + 0.01
                logger.warning(
                    "Rate limit window full — sleeping before next request",
                    extra={
                        "sleep_seconds": round(sleep_for, 2),
                        "requests_in_window": len(self._timestamps),
                    },
                )
                # Release the lock while sleeping so other threads can evict.
                self._lock.release()
                try:
                    time.sleep(max(sleep_for, 0))
                finally:
                    self._lock.acquire()

    def current_count(self) -> int:
        """Return the number of requests recorded in the current window.

        Parameters
        ----------
        None.

        Returns
        -------
        int
            Count of requests within the last ``window_seconds`` seconds.
        """
        with self._lock:
            self._evict(time.monotonic())
            return len(self._timestamps)

    def remaining(self) -> int:
        """Return the number of available request slots in the current window.

        Parameters
        ----------
        None.

        Returns
        -------
        int
            ``max_requests - current_count``, floored at 0.
        """
        return max(0, self._max_requests - self.current_count())

    # ---------------------------------------------------------------------- #
    # Private helpers
    # ---------------------------------------------------------------------- #

    def _evict(self, now: float) -> None:
        """Remove timestamps that have fallen outside the sliding window.

        Parameters
        ----------
        now : float
            Current monotonic clock value.

        Returns
        -------
        None
        """
        cutoff = now - self._window_seconds
        while self._timestamps and self._timestamps[0] < cutoff:
            self._timestamps.popleft()


# --------------------------------------------------------------------------- #
# Module-level singleton — shared across all callers in the process
# --------------------------------------------------------------------------- #

_default_limiter = RateLimiter()


def acquire_slot() -> None:
    """Acquire a request slot from the module-level rate limiter.

    Convenience wrapper around ``_default_limiter.acquire()``.  All Mist
    API call sites should call this before every outbound request.

    Parameters
    ----------
    None.

    Returns
    -------
    None
    """
    _default_limiter.acquire()


def get_limiter() -> RateLimiter:
    """Return the module-level RateLimiter singleton.

    Parameters
    ----------
    None.

    Returns
    -------
    RateLimiter
        The shared limiter instance.
    """
    return _default_limiter


# --------------------------------------------------------------------------- #
# Exponential backoff with jitter
# --------------------------------------------------------------------------- #

def _backoff_seconds(attempt: int) -> float:
    """Calculate backoff duration for a given retry attempt using full jitter.

    Formula: min(MAX_BACKOFF, BASE * 2^attempt) * random(0, 1)
    "Full jitter" (random in [0, cap]) avoids thundering-herd on 429 storms.

    Parameters
    ----------
    attempt : int
        Zero-based attempt index (0 = first retry).

    Returns
    -------
    float
        Seconds to sleep before the next attempt.
    """
    cap = min(MAX_BACKOFF_SECONDS, BASE_BACKOFF_SECONDS * (2 ** attempt))
    return random.uniform(0, cap)


def with_retries(func: Callable[..., requests.Response]) -> Callable[..., requests.Response]:
    """Decorator that retries a function on HTTP 429 with exponential backoff.

    Wraps any callable that returns a ``requests.Response``.  On a 429
    response the decorator sleeps for a jittered backoff period and retries
    up to MAX_RETRIES times.  All other HTTP errors (400, 401, 403, 404,
    500+) are not retried — they are re-raised immediately for the caller to
    handle.

    Parameters
    ----------
    func : Callable[..., requests.Response]
        The function to wrap. Must return a ``requests.Response``.

    Returns
    -------
    Callable[..., requests.Response]
        The wrapped function with retry logic applied.

    Raises
    ------
    requests.HTTPError
        Re-raised after MAX_RETRIES exhausted on 429, or immediately for
        non-retryable HTTP errors.
    requests.RequestException
        Re-raised on network-level failures (timeout, connection error).

    Examples
    --------
    >>> @with_retries
    ... def call_api(session, url):
    ...     response = session.get(url)
    ...     response.raise_for_status()
    ...     return response
    """
    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> requests.Response:
        last_exc: Exception | None = None

        for attempt in range(MAX_RETRIES + 1):  # attempts 0..MAX_RETRIES
            try:
                acquire_slot()
                response: requests.Response = func(*args, **kwargs)

                if response.status_code == 429:
                    # Respect Retry-After header if the server provides one.
                    retry_after = response.headers.get("Retry-After")
                    if retry_after:
                        try:
                            sleep_time = float(retry_after)
                        except ValueError:
                            sleep_time = _backoff_seconds(attempt)
                    else:
                        sleep_time = _backoff_seconds(attempt)

                    if attempt < MAX_RETRIES:
                        logger.warning(
                            "HTTP 429 received — backing off before retry",
                            extra={
                                "attempt": attempt + 1,
                                "max_retries": MAX_RETRIES,
                                "sleep_seconds": round(sleep_time, 2),
                                "url": getattr(response, "url", "unknown"),
                            },
                        )
                        time.sleep(sleep_time)
                        last_exc = requests.HTTPError(
                            f"429 Too Many Requests (attempt {attempt + 1})",
                            response=response,
                        )
                        continue
                    else:
                        logger.error(
                            "HTTP 429 — max retries exhausted",
                            extra={
                                "max_retries": MAX_RETRIES,
                                "url": getattr(response, "url", "unknown"),
                            },
                        )
                        response.raise_for_status()

                # All non-429 responses returned directly to caller.
                return response

            except requests.RequestException as exc:
                # Network-level error (timeout, DNS, etc.) — do not retry.
                logger.error(
                    "Network error during API call",
                    extra={"error": str(exc), "attempt": attempt + 1},
                )
                raise

        # Should only reach here if MAX_RETRIES was 0 and 429 was hit.
        if last_exc:
            raise last_exc
        raise RuntimeError("with_retries: unexpected exit from retry loop")  # pragma: no cover

    return wrapper