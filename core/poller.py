# Python 3.11+
# core/poller.py — Fetches Marvis Actions and site inventory from the Mist API.
#
# Responsibilities:
#   - Retrieve all sites for the org
#   - Retrieve all Marvis Actions for the org
#   - Attach site metadata (name, timezone) to each action for downstream use
#   - Handle all Mist API HTTP error codes explicitly
#   - Respect rate limiting via utils.rate_limiter.with_retries

from __future__ import annotations

from typing import Any

import requests

from utils.auth import build_session, get_base_url, get_org_id
from utils.logger import get_logger
from utils.rate_limiter import with_retries

logger = get_logger(__name__)


# --------------------------------------------------------------------------- #
# Exceptions
# --------------------------------------------------------------------------- #

class PollerError(Exception):
    """Raised when a polling operation fails unrecoverably."""


# --------------------------------------------------------------------------- #
# Internal HTTP helpers
# --------------------------------------------------------------------------- #

def _handle_http_error(resp: requests.Response, context: str) -> None:
    """Raise a descriptive PollerError for known Mist HTTP error codes.

    Parameters
    ----------
    resp : requests.Response
        The response object to inspect.
    context : str
        Human-readable description of the call (e.g. "fetch sites") for
        inclusion in the error message.

    Raises
    ------
    PollerError
        Always raised when the status code indicates an error.
    """
    code = resp.status_code
    messages: dict[int, str] = {
        400: "Bad request — check org_id format or query parameters",
        401: "Unauthorized — MIST_API_TOKEN is invalid or expired",
        403: "Forbidden — token lacks permission for this resource",
        404: "Not found — org_id or endpoint does not exist",
        429: "Rate limit exceeded — this should have been handled by the retry decorator",
    }
    if code in messages:
        raise PollerError(f"HTTP {code} during {context}: {messages[code]}")
    if code >= 500:
        raise PollerError(
            f"HTTP {code} server error during {context} — Mist API may be degraded"
        )


@with_retries
def _get(session: requests.Session, url: str, params: dict | None = None) -> requests.Response:
    """Execute a GET request with rate-limit retry wrapping.

    Parameters
    ----------
    session : requests.Session
        Authenticated session from :func:`utils.auth.build_session`.
    url : str
        Full URL to request.
    params : dict | None
        Optional query parameters.

    Returns
    -------
    requests.Response
        The raw response object. Callers are responsible for status checking.
    """
    logger.debug("GET %s params=%s", url, params)
    return session.get(url, params=params, timeout=30)


# --------------------------------------------------------------------------- #
# Site fetching
# --------------------------------------------------------------------------- #

def fetch_sites(
    session: requests.Session,
    org_id: str,
    base_url: str,
) -> list[dict[str, Any]]:
    """Retrieve all sites for the org.

    Parameters
    ----------
    session : requests.Session
        Authenticated Mist API session.
    org_id : str
        Mist organisation ID.
    base_url : str
        API base URL, e.g. ``https://api.mist.com/api/v1``.

    Returns
    -------
    list[dict[str, Any]]
        List of site objects as returned by the Mist API.  Each dict
        contains at minimum ``id``, ``name``, and ``timezone``.

    Raises
    ------
    PollerError
        On any HTTP error or unexpected response shape.
    """
    url = f"{base_url}/orgs/{org_id}/sites"
    logger.info("Fetching site list", extra={"org_id": org_id})

    try:
        resp = _get(session, url)
    except requests.RequestException as exc:
        raise PollerError(f"Network error fetching sites: {exc}") from exc

    if not resp.ok:
        _handle_http_error(resp, "fetch sites")

    try:
        sites: list[dict[str, Any]] = resp.json()
    except ValueError as exc:
        raise PollerError(f"Invalid JSON in sites response: {exc}") from exc

    if not isinstance(sites, list):
        raise PollerError(
            f"Unexpected sites response shape — expected list, got {type(sites).__name__}"
        )

    logger.info("Sites retrieved", extra={"site_count": len(sites)})
    return sites


# --------------------------------------------------------------------------- #
# Marvis Actions fetching
# --------------------------------------------------------------------------- #

def fetch_marvis_actions(
    session: requests.Session,
    org_id: str,
    base_url: str,
    site_ids: list[str],
) -> list[dict]:
    """Synthesize Marvis-equivalent actions from device events per site.

    Queries /sites/{site_id}/devices/events/search for each site and
    builds actionable issue dicts from CONNECTIVITY_TEST failures,
    SW_DISCONNECTED events, and port flap events in the last 24 hours.

    Parameters
    ----------
    session : requests.Session
        Authenticated Mist API session.
    org_id : str
        Mist organisation ID.
    base_url : str
        API base URL.
    site_ids : list[str]
        Site UUIDs to poll.

    Returns
    -------
    list[dict]
        Synthesized action dicts in Marvis Action format.
    """
    import re as _re
    import time as _time

    actions = []
    since = int(_time.time()) - 86400  # last 24 hours

    event_types = ["CONNECTIVITY_TEST", "SW_DISCONNECTED", "SW_PORT_UP", "SW_PORT_DOWN"]

    for site_id in site_ids:
        url = f"{base_url}/sites/{site_id}/devices/events/search"
        all_events = []

        for evt_type in event_types:
            params = {"type": evt_type, "start": since, "limit": 100}
            try:
                resp = _get(session, url, params=params)
                _handle_http_error(resp, f"fetch device events ({evt_type})")
                data = resp.json()
                all_events.extend(data.get("results", []))
            except (PollerError, requests.RequestException, ValueError) as exc:
                logger.warning(
                    "Failed to fetch device events",
                    extra={"site_id": site_id, "event_type": evt_type, "error": str(exc)},
                )
                continue

        # --- CONNECTIVITY_TEST failures → dhcp_failure actions ---
        vlan_failures: dict[str, list] = {}
        for evt in all_events:
            if evt.get("type") == "CONNECTIVITY_TEST" and "failure" in evt.get("text", "").lower():
                vlans = _re.findall(r'\b\d{2,4}\b', evt.get("text", ""))
                for vlan in vlans:
                    vlan_failures.setdefault(vlan, []).append(evt)

        for vlan, evts in vlan_failures.items():
            actions.append({
                "id":            f"{site_id}_dhcp_vlan_{vlan}",
                "category":      "connectivity",
                "symptom":       "dhcp_failure",
                "issue_type":    "dhcp_failure",
                "severity":      "high",
                "site_id":       site_id,
                "status":        "open",
                "batch_count":   len(evts),
                "self_drivable": False,
                "details": {
                    "failure_reason": f"DHCP Failure On VLAN {vlan}",
                    "impacted_vlans": [vlan],
                },
            })

        # --- SW_DISCONNECTED → sw_offline actions ---
        seen_switches: set = set()
        for evt in all_events:
            if evt.get("type") == "SW_DISCONNECTED":
                mac = evt.get("mac", "")
                if mac and mac not in seen_switches:
                    seen_switches.add(mac)
                    actions.append({
                        "id":            f"{site_id}_sw_offline_{mac}",
                        "category":      "switch",
                        "symptom":       "sw_offline",
                        "issue_type":    "switch_disconnect",
                        "severity":      "high",
                        "site_id":       site_id,
                        "device_id":     mac,
                        "status":        "open",
                        "batch_count":   1,
                        "self_drivable": False,
                        "details":       {"event_type": "switch_disconnect"},
                    })

        # --- Port flap (SW_PORT_UP + SW_PORT_DOWN pairs) → port_flap actions ---
        port_events: dict[str, list] = {}
        for evt in all_events:
            if evt.get("type") in ("SW_PORT_UP", "SW_PORT_DOWN"):
                key = f"{evt.get('mac','')}_{evt.get('port_id','')}"
                port_events.setdefault(key, []).append(evt)

        for key, evts in port_events.items():
            if len(evts) >= 2:  # at least one flap cycle
                mac, port_id = key.split("_", 1)
                actions.append({
                    "id":            f"{site_id}_port_flap_{key}",
                    "category":      "switch",
                    "symptom":       "port_flap",
                    "issue_type":    "switch_port",
                    "severity":      "medium",
                    "site_id":       site_id,
                    "device_id":     mac,
                    "port_id":       port_id,
                    "status":        "open",
                    "batch_count":   len(evts),
                    "self_drivable": False,
                    "details":       {"event_type": "port_flap"},
                })
        # --- MARVIS_EVENT_STA_LEAVING → roaming_failure actions ---
        # Fetch wireless client events separately using the correct endpoint
        client_url = f"{base_url}/sites/{site_id}/clients/events/search"
        client_params = {
            "type": "MARVIS_EVENT_STA_LEAVING",
            "start": since,
            "limit": 100,
        }
        roaming_events: dict[str, list] = {}
        try:
            resp = _get(session, client_url, params=client_params)
            # 404 means no client events endpoint — skip silently
            if resp.ok:
                client_data = resp.json()
                for evt in client_data.get("results", []):
                    mac = evt.get("mac", "")
                    if mac:
                        roaming_events.setdefault(mac, []).append(evt)
        except (PollerError, requests.RequestException, ValueError) as exc:
            logger.warning(
                "Failed to fetch roaming events",
                extra={"site_id": site_id, "error": str(exc)},
            )

        # Only flag as roaming failure if client left 3+ APs in the window
        # — filters out normal single-AP roams
        for mac, evts in roaming_events.items():
            unique_aps = len(set(e.get("ap", "") for e in evts))
            if unique_aps >= 3:
                # Get latest RSSI and SSID for context
                latest = sorted(evts, key=lambda e: e.get("timestamp", 0))[-1]
                rssi = latest.get("rssi", 0)
                ssid = latest.get("ssid", "unknown")
                actions.append({
                    "id":            f"{site_id}_roaming_{mac}",
                    "category":      "client_connectivity",
                    "symptom":       "roaming_failure",
                    "issue_type":    "roaming_failure",
                    "severity":      "high" if rssi < -75 else "medium",
                    "site_id":       site_id,
                    "client_id":     mac,
                    "status":        "open",
                    "batch_count":   len(evts),
                    "self_drivable": False,
                    "details": {
                        "client_mac":  mac,
                        "ssid":        ssid,
                        "unique_aps":  unique_aps,
                        "roam_count":  len(evts),
                        "latest_rssi": rssi,
                        "failure_reason": (
                            f"Client roamed across {unique_aps} APs "
                            f"{len(evts)} times — RSSI {rssi}dBm"
                        ),
                    },
                })

    logger.info(
        "Synthesized actions from device events",
        extra={"site_count": len(site_ids), "action_count": len(actions)},
    )
    return actions
# --------------------------------------------------------------------------- #
# Main poll entry point
# --------------------------------------------------------------------------- #

def poll(
    session: requests.Session,
    org_id: str,
    base_url: str,
) -> list[dict[str, Any]]:
    """Run a full poll cycle — sites then actions.

    Parameters
    ----------
    session : requests.Session
        Authenticated Mist API session.
    org_id : str
        Mist organisation ID.
    base_url : str
        API base URL.

    Returns
    -------
    list[dict[str, Any]]
        Enriched action list with site metadata attached.

    Raises
    ------
    PollerError
        If site fetch fails or response is malformed.
    """
    logger.info(
        "Poll cycle started",
        extra={"org_id": org_id, "base_url": base_url},
    )

    # Step 1: get sites
    sites = fetch_sites(session, org_id, base_url)
    site_lookup = {s["id"]: s for s in sites}
    site_ids = [s["id"] for s in sites]

    # Step 2: get actions from device events
    logger.info("Fetching Marvis Actions", extra={"org_id": org_id})
    actions = fetch_marvis_actions(
        session=session,
        org_id=org_id,
        base_url=base_url,
        site_ids=site_ids,
    )

    # Step 3: attach site metadata to each action
    for action in actions:
        site_id = action.get("site_id", "")
        site = site_lookup.get(site_id, {})
        action["site_name"] = site.get("name", "unknown")
        action["site_timezone"] = site.get("timezone", "UTC")

    logger.info(
        "Poll cycle complete",
        extra={"site_count": len(sites), "action_count": len(actions)},
    )

    return actions