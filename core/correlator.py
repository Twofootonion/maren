# Python 3.11+
# core/correlator.py — Telemetry correlation engine.
#
# For each Marvis Action, fetches correlated signals from four sources:
#   1. Client stats         GET /sites/{site_id}/stats/clients
#   2. Device stats         GET /sites/{site_id}/stats/devices
#   3. Client event history GET /sites/{site_id}/events/client
#   4. Device event history GET /sites/{site_id}/events/device
#
# The correlated telemetry dict is attached to the action under the key
# "correlated_telemetry" and is used by scorer.py to compute blast_radius
# and recurrence_factor.
#
# All API calls respect rate limiting via @with_retries. Failures on any
# individual telemetry fetch are non-fatal — the correlator logs a warning
# and returns partial data so the scorer can still operate.

from __future__ import annotations

from datetime import datetime, timezone, timedelta
from typing import Any

import requests

from utils.auth import build_session, get_base_url
from utils.logger import get_logger
from utils.rate_limiter import with_retries

logger = get_logger(__name__)

# How far back to look when counting recurrence in client/device event history.
RECURRENCE_WINDOW_HOURS: int = 24


# --------------------------------------------------------------------------- #
# Exceptions
# --------------------------------------------------------------------------- #

class CorrelatorError(Exception):
    """Raised when a telemetry fetch fails unrecoverably."""


# --------------------------------------------------------------------------- #
# Internal HTTP helper
# --------------------------------------------------------------------------- #

def _handle_http_error(resp: requests.Response, context: str) -> None:
    """Log a warning for known HTTP error codes and raise CorrelatorError.

    Parameters
    ----------
    resp : requests.Response
        Response to inspect.
    context : str
        Human-readable description of the call for log messages.

    Raises
    ------
    CorrelatorError
        Always raised when status indicates an error.
    """
    code = resp.status_code
    messages: dict[int, str] = {
        400: "Bad request — check site_id or query parameters",
        401: "Unauthorized — token may have expired",
        403: "Forbidden — token lacks read access to this site",
        404: "Not found — site_id does not exist or endpoint unavailable",
        429: "Rate limit — should have been caught by retry decorator",
    }
    detail = messages.get(code, f"server error (HTTP {code})")
    raise CorrelatorError(f"HTTP {code} during {context}: {detail}")


@with_retries
def _get(
    session: requests.Session,
    url: str,
    params: dict | None = None,
) -> requests.Response:
    """Rate-limited GET helper.

    Parameters
    ----------
    session : requests.Session
        Authenticated Mist API session.
    url : str
        Full endpoint URL.
    params : dict | None
        Optional query parameters.

    Returns
    -------
    requests.Response
        Raw response; caller checks status.
    """
    logger.debug("GET %s params=%s", url, params)
    return session.get(url, params=params, timeout=30)


# --------------------------------------------------------------------------- #
# Individual telemetry fetchers
# --------------------------------------------------------------------------- #

def fetch_client_stats(
    session: requests.Session,
    site_id: str,
    base_url: str,
) -> list[dict[str, Any]]:
    """Fetch current client stats for a site.

    Parameters
    ----------
    session : requests.Session
        Authenticated Mist API session.
    site_id : str
        Mist site ID.
    base_url : str
        API base URL.

    Returns
    -------
    list[dict[str, Any]]
        List of client stat objects.  Empty list on non-fatal errors.
    """
    url = f"{base_url}/sites/{site_id}/stats/clients"
    try:
        resp = _get(session, url)
    except requests.RequestException as exc:
        logger.warning("Network error fetching client stats: %s", exc,
                       extra={"site_id": site_id})
        return []

    if not resp.ok:
        try:
            _handle_http_error(resp, f"client stats site={site_id}")
        except CorrelatorError as exc:
            logger.warning(str(exc))
            return []

    try:
        data = resp.json()
    except ValueError:
        logger.warning("Invalid JSON in client stats response",
                       extra={"site_id": site_id})
        return []

    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("results", "clients", "data"):
            if key in data and isinstance(data[key], list):
                return data[key]
    return []


def fetch_device_stats(
    session: requests.Session,
    site_id: str,
    base_url: str,
) -> list[dict[str, Any]]:
    """Fetch current device (AP / switch) stats for a site.

    Parameters
    ----------
    session : requests.Session
        Authenticated Mist API session.
    site_id : str
        Mist site ID.
    base_url : str
        API base URL.

    Returns
    -------
    list[dict[str, Any]]
        List of device stat objects.  Empty list on non-fatal errors.
    """
    url = f"{base_url}/sites/{site_id}/stats/devices"
    try:
        resp = _get(session, url)
    except requests.RequestException as exc:
        logger.warning("Network error fetching device stats: %s", exc,
                       extra={"site_id": site_id})
        return []

    if not resp.ok:
        try:
            _handle_http_error(resp, f"device stats site={site_id}")
        except CorrelatorError as exc:
            logger.warning(str(exc))
            return []

    try:
        data = resp.json()
    except ValueError:
        logger.warning("Invalid JSON in device stats response",
                       extra={"site_id": site_id})
        return []

    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("results", "devices", "data"):
            if key in data and isinstance(data[key], list):
                return data[key]
    return []


def fetch_client_events(
    session: requests.Session,
    site_id: str,
    base_url: str,
    hours: int = RECURRENCE_WINDOW_HOURS,
) -> list[dict[str, Any]]:
    """Fetch client event history for a site within a lookback window.

    Parameters
    ----------
    session : requests.Session
        Authenticated Mist API session.
    site_id : str
        Mist site ID.
    base_url : str
        API base URL.
    hours : int
        How many hours back to fetch events.  Defaults to
        ``RECURRENCE_WINDOW_HOURS`` (24).

    Returns
    -------
    list[dict[str, Any]]
        List of client event objects.  Empty list on non-fatal errors.
    """
    url = f"{base_url}/sites/{site_id}/clients/events/search"
    now = datetime.now(tz=timezone.utc)
    start = int((now - timedelta(hours=hours)).timestamp())
    end = int(now.timestamp())

    try:
        resp = _get(session, url, params={"start": start, "end": end})
    except requests.RequestException as exc:
        logger.warning("Network error fetching client events: %s", exc,
                       extra={"site_id": site_id})
        return []

    if not resp.ok:
        try:
            _handle_http_error(resp, f"client events site={site_id}")
        except CorrelatorError as exc:
            logger.warning(str(exc))
            return []

    try:
        data = resp.json()
    except ValueError:
        logger.warning("Invalid JSON in client events response",
                       extra={"site_id": site_id})
        return []

    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("results", "events", "data"):
            if key in data and isinstance(data[key], list):
                return data[key]
    return []


def fetch_device_events(
    session: requests.Session,
    site_id: str,
    base_url: str,
    hours: int = RECURRENCE_WINDOW_HOURS,
) -> list[dict[str, Any]]:
    """Fetch device event history for a site within a lookback window.

    Parameters
    ----------
    session : requests.Session
        Authenticated Mist API session.
    site_id : str
        Mist site ID.
    base_url : str
        API base URL.
    hours : int
        How many hours back to fetch events.  Defaults to
        ``RECURRENCE_WINDOW_HOURS`` (24).

    Returns
    -------
    list[dict[str, Any]]
        List of device event objects.  Empty list on non-fatal errors.
    """
    url = f"{base_url}/sites/{site_id}/devices/events/search"
    now = datetime.now(tz=timezone.utc)
    start = int((now - timedelta(hours=hours)).timestamp())
    end = int(now.timestamp())

    try:
        resp = _get(session, url, params={"start": start, "end": end})
    except requests.RequestException as exc:
        logger.warning("Network error fetching device events: %s", exc,
                       extra={"site_id": site_id})
        return []

    if not resp.ok:
        try:
            _handle_http_error(resp, f"device events site={site_id}")
        except CorrelatorError as exc:
            logger.warning(str(exc))
            return []

    try:
        data = resp.json()
    except ValueError:
        logger.warning("Invalid JSON in device events response",
                       extra={"site_id": site_id})
        return []

    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("results", "events", "data"):
            if key in data and isinstance(data[key], list):
                return data[key]
    return []


# --------------------------------------------------------------------------- #
# Recurrence counter
# --------------------------------------------------------------------------- #

def count_recurrences(
    action: dict[str, Any],
    client_events: list[dict[str, Any]],
    device_events: list[dict[str, Any]],
) -> int:
    """Count how many times this action's issue type recurred in event history.

    Matches events whose ``type`` or ``reason`` field contains keywords from
    the action's ``category`` or ``issue_type``.  This is a best-effort
    heuristic — Mist event schemas vary by firmware and category.

    Parameters
    ----------
    action : dict[str, Any]
        Marvis Action object (must have at least ``category`` or
        ``issue_type``).
    client_events : list[dict[str, Any]]
        Client events from :func:`fetch_client_events`.
    device_events : list[dict[str, Any]]
        Device events from :func:`fetch_device_events`.

    Returns
    -------
    int
        Number of matching events in the lookback window.
    """
    # Build a set of lowercase keywords to match against event type/reason.
    keywords: set[str] = set()
    for field in ("category", "issue_type", "action_type"):
        val = action.get(field, "")
        if val:
            keywords.update(val.lower().split("_"))
            keywords.add(val.lower())

    if not keywords:
        return 0

    count = 0
    all_events = client_events + device_events
    for event in all_events:
        event_type = str(event.get("type", "") or event.get("reason", "")).lower()
        if any(kw in event_type for kw in keywords):
            count += 1

    return count


# --------------------------------------------------------------------------- #
# Blast-radius calculator
# --------------------------------------------------------------------------- #

def calculate_blast_radius(
    action: dict[str, Any],
    client_stats: list[dict[str, Any]],
    device_stats: list[dict[str, Any]],
) -> int:
    """Estimate the number of clients or devices affected by an action.

    Checks the action's own ``affected_count`` / ``details`` field first.
    Falls back to counting clients/devices on the affected AP or switch port
    using the stats lists.

    Parameters
    ----------
    action : dict[str, Any]
        Marvis Action object.
    client_stats : list[dict[str, Any]]
        Client stat objects from :func:`fetch_client_stats`.
    device_stats : list[dict[str, Any]]
        Device stat objects from :func:`fetch_device_stats`.

    Returns
    -------
    int
        Estimated number of affected endpoints.  Minimum 1.
    """
    # 1. Trust explicit counts embedded in the action if present.
    for key in ("affected_count", "num_clients", "client_count", "count"):
        val = action.get(key)
        if isinstance(val, int) and val > 0:
            return val

    # Check inside nested details dict.
    details = action.get("details") or {}
    if isinstance(details, dict):
        for key in ("affected_count", "num_clients", "client_count", "count"):
            val = details.get(key)
            if isinstance(val, int) and val > 0:
                return val

    # 2. Count clients currently associated to the affected AP.
    affected_ap = (
        action.get("ap_id")
        or action.get("device_id")
        or (details.get("ap_id") if isinstance(details, dict) else None)
    )
    if affected_ap and client_stats:
        ap_clients = [
            c for c in client_stats
            if c.get("ap_id") == affected_ap or c.get("ap_mac") == affected_ap
        ]
        if ap_clients:
            return len(ap_clients)

    # 3. Fall back to total clients on site as a conservative upper bound.
    if client_stats:
        return len(client_stats)

    # 4. Count affected devices if no client data.
    if device_stats:
        return len(device_stats)

    return 1  # minimum blast radius


# --------------------------------------------------------------------------- #
# Main correlation function
# --------------------------------------------------------------------------- #

def correlate(
    action: dict[str, Any],
    session: requests.Session | None = None,
    base_url: str | None = None,
) -> dict[str, Any]:
    """Fetch and attach correlated telemetry to a single Marvis Action.

    Fetches client stats, device stats, client events, and device events for
    the action's site.  Computes blast_radius and recurrence count and
    attaches everything under ``action["correlated_telemetry"]``.

    Parameters
    ----------
    action : dict[str, Any]
        A single Marvis Action object (enriched with site metadata by
        ``poller.enrich_actions_with_site_metadata``).
    session : requests.Session | None
        Authenticated session.  Created if not provided.
    base_url : str | None
        API base URL.  Read from environment if not provided.

    Returns
    -------
    dict[str, Any]
        The action dict with ``correlated_telemetry`` key added.  Structure::

            {
                "client_stats":    [...],
                "device_stats":    [...],
                "client_events":   [...],
                "device_events":   [...],
                "blast_radius":    <int>,
                "recurrence_count": <int>,
                "telemetry_partial": <bool>,   # True if any fetch failed
            }
    """
    if session is None:
        session = build_session()
    if base_url is None:
        base_url = get_base_url()

    site_id: str = action.get("site_id", "")
    site_name: str = action.get("site_name", "unknown")

    if not site_id:
        logger.warning(
            "Action has no site_id — skipping telemetry fetch",
            extra={"action_id": action.get("id", "unknown")},
        )
        action["correlated_telemetry"] = _empty_telemetry(partial=True)
        return action

    logger.debug(
        "Correlating telemetry",
        extra={"site_id": site_id, "site_name": site_name,
               "action_id": action.get("id", "unknown")},
    )

    client_stats  = fetch_client_stats(session, site_id, base_url)
    device_stats  = fetch_device_stats(session, site_id, base_url)
    client_events = fetch_client_events(session, site_id, base_url)
    device_events = fetch_device_events(session, site_id, base_url)

    blast_radius  = calculate_blast_radius(action, client_stats, device_stats)
    recurrence    = count_recurrences(action, client_events, device_events)

    # Mark partial if any fetch returned empty unexpectedly — caller can
    # decide whether to trust the scores or flag for review.
    partial = not any([client_stats, device_stats, client_events, device_events])

    telemetry: dict[str, Any] = {
        "client_stats":     client_stats,
        "device_stats":     device_stats,
        "client_events":    client_events,
        "device_events":    device_events,
        "blast_radius":     blast_radius,
        "recurrence_count": recurrence,
        "telemetry_partial": partial,
    }

    action["correlated_telemetry"] = telemetry

    logger.info(
        "Telemetry correlated",
        extra={
            "site_id": site_id,
            "blast_radius": blast_radius,
            "recurrence_count": recurrence,
            "clients": len(client_stats),
            "devices": len(device_stats),
            "client_events": len(client_events),
            "device_events": len(device_events),
            "partial": partial,
        },
    )

    return action


def correlate_all(
    actions: list[dict[str, Any]],
    session: requests.Session | None = None,
    base_url: str | None = None,
) -> list[dict[str, Any]]:
    """Correlate telemetry for a list of Marvis Actions.

    Fetches are not parallelised — this keeps the rate limiter simple and
    avoids hammering the API during a full-org poll.  The rate limiter's
    sliding window enforces the 5000 req/hr ceiling regardless.

    Parameters
    ----------
    actions : list[dict[str, Any]]
        Marvis Action objects from ``poller.poll()``.
    session : requests.Session | None
        Authenticated session.  Created once and reused if not provided.
    base_url : str | None
        API base URL.

    Returns
    -------
    list[dict[str, Any]]
        Actions with ``correlated_telemetry`` attached to each.
    """
    if session is None:
        session = build_session()
    if base_url is None:
        base_url = get_base_url()

    logger.info(
        "Starting telemetry correlation for all actions",
        extra={"action_count": len(actions)},
    )

    for i, action in enumerate(actions):
        logger.debug(
            "Correlating action %d/%d", i + 1, len(actions),
            extra={"action_id": action.get("id", "unknown")},
        )
        correlate(action, session=session, base_url=base_url)

    logger.info("Telemetry correlation complete",
                extra={"action_count": len(actions)})
    return actions


# --------------------------------------------------------------------------- #
# Helper
# --------------------------------------------------------------------------- #

def _empty_telemetry(partial: bool = False) -> dict[str, Any]:
    """Return a zeroed-out telemetry dict.

    Parameters
    ----------
    partial : bool
        Whether to flag this as a partial (failed) fetch.

    Returns
    -------
    dict[str, Any]
        Empty telemetry structure.
    """
    return {
        "client_stats":     [],
        "device_stats":     [],
        "client_events":    [],
        "device_events":    [],
        "blast_radius":     1,
        "recurrence_count": 0,
        "telemetry_partial": partial,
    }
