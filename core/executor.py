# Python 3.11+
# core/executor.py — Remediation action executor.
#
# In DRY_RUN mode (default): logs exactly what would be done, including the
# full API endpoint, HTTP method, and payload — no live calls made.
#
# In LIVE mode: executes the action against the Mist API, captures the
# response, and records the outcome.
#
# Each action type maps to a dedicated handler function that:
#   - Builds the exact API request (URL + method + payload)
#   - In dry-run: logs the request and returns a dry_run result
#   - In live:    executes the request via the authenticated session,
#                 handles HTTP errors explicitly (400/401/403/404/429/500+),
#                 and returns a structured result dict
#
# Result dict structure (attached to action as "execution_result"):
#   {
#       "action_result":  "success" | "dry_run" | "failed" | "skipped",
#       "http_status":    int | None,
#       "response_body":  dict | None,
#       "error":          str | None,
#       "api_endpoint":   str,
#       "http_method":    str,
#       "payload":        dict | None,
#       "executed_at":    ISO8601 str,
#   }

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import requests

from utils.auth import build_session, get_base_url
from utils.logger import get_logger
from utils.rate_limiter import with_retries

logger = get_logger(__name__)


# --------------------------------------------------------------------------- #
# Exceptions
# --------------------------------------------------------------------------- #

class ExecutorError(Exception):
    """Raised when an execution attempt fails unrecoverably."""


# --------------------------------------------------------------------------- #
# HTTP helpers
# --------------------------------------------------------------------------- #

@with_retries
def _post(
    session: requests.Session,
    url: str,
    payload: dict | None = None,
) -> requests.Response:
    """Rate-limited POST request.

    Parameters
    ----------
    session : requests.Session
        Authenticated Mist API session.
    url : str
        Full endpoint URL.
    payload : dict | None
        JSON request body.

    Returns
    -------
    requests.Response
    """
    logger.debug("POST %s payload=%s", url, payload)
    return session.post(url, json=payload or {}, timeout=30)


@with_retries
def _put(
    session: requests.Session,
    url: str,
    payload: dict,
) -> requests.Response:
    """Rate-limited PUT request.

    Parameters
    ----------
    session : requests.Session
        Authenticated Mist API session.
    url : str
        Full endpoint URL.
    payload : dict
        JSON request body.

    Returns
    -------
    requests.Response
    """
    logger.debug("PUT %s payload=%s", url, payload)
    return session.put(url, json=payload, timeout=30)


def _handle_http_error(resp: requests.Response, context: str) -> str:
    """Map a non-2xx response to a human-readable error string.

    Parameters
    ----------
    resp : requests.Response
        The response to inspect.
    context : str
        Human-readable description of the attempted operation.

    Returns
    -------
    str
        Error description (does not raise — callers record and continue).
    """
    code = resp.status_code
    messages: dict[int, str] = {
        400: "Bad request — check payload structure or IDs",
        401: "Unauthorized — token invalid or expired",
        403: "Forbidden — token lacks write permission for this resource",
        404: "Not found — device/client/site ID does not exist",
        429: "Rate limited — retry decorator should have handled this",
    }
    detail = messages.get(code, f"server error (HTTP {code})")
    return f"HTTP {code} during {context}: {detail}"


def _now_iso() -> str:
    """Return the current UTC time as an ISO 8601 string.

    Returns
    -------
    str
        e.g. ``"2025-06-01T12:34:56.789012+00:00"``
    """
    return datetime.now(tz=timezone.utc).isoformat()


def _build_result(
    action_result: str,
    api_endpoint: str,
    http_method: str,
    payload: dict | None = None,
    http_status: int | None = None,
    response_body: dict | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    """Construct a standardised execution result dict.

    Parameters
    ----------
    action_result : str
        One of ``"success"``, ``"dry_run"``, ``"failed"``, ``"skipped"``.
    api_endpoint : str
        The full URL that was (or would have been) called.
    http_method : str
        HTTP verb (``"POST"``, ``"PUT"``, etc.).
    payload : dict | None
        Request body used.
    http_status : int | None
        HTTP response status code, if a live call was made.
    response_body : dict | None
        Parsed JSON response body, if available.
    error : str | None
        Error description if action_result is ``"failed"``.

    Returns
    -------
    dict[str, Any]
        Standardised result structure.
    """
    return {
        "action_result":  action_result,
        "http_status":    http_status,
        "response_body":  response_body,
        "error":          error,
        "api_endpoint":   api_endpoint,
        "http_method":    http_method,
        "payload":        payload,
        "executed_at":    _now_iso(),
    }


# --------------------------------------------------------------------------- #
# Action handlers — one per action_type
# --------------------------------------------------------------------------- #

def _exec_clear_client_session(
    action: dict[str, Any],
    session: requests.Session,
    base_url: str,
    dry_run: bool,
) -> dict[str, Any]:
    """Clear (disconnect) a client session — Tier 1.

    Endpoint: POST /sites/{site_id}/clients/{client_id}/disconnect

    Parameters
    ----------
    action : dict[str, Any]
        Marvis Action with decision fields attached.
    session : requests.Session
        Authenticated Mist API session.
    base_url : str
        API base URL.
    dry_run : bool
        If True, log intent only — no live call.

    Returns
    -------
    dict[str, Any]
        Execution result dict.
    """
    site_id   = action.get("site_id", "unknown")
    client_id = action.get("action_target", action.get("client_id", "unknown"))
    url       = f"{base_url}/sites/{site_id}/clients/{client_id}/disconnect"
    payload: dict = {}

    if dry_run:
        logger.info(
            "[DRY RUN] Would POST to disconnect client",
            extra={"url": url, "site_id": site_id, "client_id": client_id},
        )
        return _build_result("dry_run", url, "POST", payload)

    try:
        resp = _post(session, url, payload)
    except requests.RequestException as exc:
        err = f"Network error during clear_client_session: {exc}"
        logger.error(err, extra={"site_id": site_id, "client_id": client_id})
        return _build_result("failed", url, "POST", payload, error=err)

    if not resp.ok:
        err = _handle_http_error(resp, "clear_client_session")
        logger.error(err)
        return _build_result("failed", url, "POST", payload,
                             http_status=resp.status_code, error=err)

    logger.info("Client session cleared",
                extra={"site_id": site_id, "client_id": client_id})
    return _build_result("success", url, "POST", payload,
                         http_status=resp.status_code,
                         response_body=_safe_json(resp))


def _exec_marvis_rca_query(
    action: dict[str, Any],
    session: requests.Session,
    base_url: str,
    dry_run: bool,
    org_id: str,
) -> dict[str, Any]:
    """Trigger a Marvis RCA query for the issue — Tier 1.

    Endpoint: POST /orgs/{org_id}/marvis/auto_rules

    Parameters
    ----------
    action : dict[str, Any]
        Marvis Action with decision fields attached.
    session : requests.Session
        Authenticated Mist API session.
    base_url : str
        API base URL.
    dry_run : bool
        If True, log intent only — no live call.
    org_id : str
        Mist organisation ID.

    Returns
    -------
    dict[str, Any]
        Execution result dict.
    """
    url = f"{base_url}/orgs/{org_id}/marvis/auto_rules"
    payload = {
        "site_id":    action.get("site_id", ""),
        "category":   action.get("category", ""),
        "issue_type": action.get("issue_type", action.get("category", "")),
        "action_id":  action.get("id", ""),
    }

    if dry_run:
        logger.info(
            "[DRY RUN] Would POST Marvis RCA query",
            extra={"url": url, "payload": payload},
        )
        return _build_result("dry_run", url, "POST", payload)

    try:
        resp = _post(session, url, payload)
    except requests.RequestException as exc:
        err = f"Network error during marvis_rca_query: {exc}"
        logger.error(err)
        return _build_result("failed", url, "POST", payload, error=err)

    if not resp.ok:
        err = _handle_http_error(resp, "marvis_rca_query")
        logger.error(err)
        return _build_result("failed", url, "POST", payload,
                             http_status=resp.status_code, error=err)

    logger.info("Marvis RCA query submitted",
                extra={"site_id": action.get("site_id"), "org_id": org_id})
    return _build_result("success", url, "POST", payload,
                         http_status=resp.status_code,
                         response_body=_safe_json(resp))


def _exec_bounce_port(
    action: dict[str, Any],
    session: requests.Session,
    base_url: str,
    dry_run: bool,
) -> dict[str, Any]:
    """Bounce a switch port (disable then re-enable) — Tier 2.

    Endpoint: PUT /sites/{site_id}/devices/{device_id}

    The port is disabled via a config PUT setting ``poe_disabled: true`` on
    the port, then immediately re-enabled.  This mimics a physical port cycle
    without requiring a full device restart.

    Parameters
    ----------
    action : dict[str, Any]
        Marvis Action with decision fields attached.
    session : requests.Session
        Authenticated Mist API session.
    base_url : str
        API base URL.
    dry_run : bool
        If True, log intent only — no live calls.

    Returns
    -------
    dict[str, Any]
        Execution result dict.
    """
    site_id   = action.get("site_id", "unknown")
    device_id = action.get("action_target",
                           action.get("device_id", "unknown"))
    port_id   = action.get("port_id", "0")

    disable_url = f"{base_url}/sites/{site_id}/devices/{device_id}"
    disable_payload = {
        "port_config": {
            port_id: {"usage": "access", "disabled": True}
        }
    }
    enable_payload = {
        "port_config": {
            port_id: {"usage": "access", "disabled": False}
        }
    }

    if dry_run:
        logger.info(
            "[DRY RUN] Would bounce switch port — disable then re-enable",
            extra={
                "url": disable_url,
                "device_id": device_id,
                "port_id": port_id,
                "disable_payload": disable_payload,
                "enable_payload": enable_payload,
            },
        )
        return _build_result("dry_run", disable_url, "PUT", disable_payload)

    # Step 1: disable port
    try:
        resp = _put(session, disable_url, disable_payload)
    except requests.RequestException as exc:
        err = f"Network error disabling port during bounce: {exc}"
        logger.error(err)
        return _build_result("failed", disable_url, "PUT", disable_payload, error=err)

    if not resp.ok:
        err = _handle_http_error(resp, "bounce_port (disable)")
        logger.error(err)
        return _build_result("failed", disable_url, "PUT", disable_payload,
                             http_status=resp.status_code, error=err)

    logger.info("Port disabled", extra={"device_id": device_id, "port_id": port_id})

    # Step 2: re-enable port
    try:
        resp2 = _put(session, disable_url, enable_payload)
    except requests.RequestException as exc:
        err = f"Network error re-enabling port during bounce: {exc}"
        logger.error(err)
        return _build_result("failed", disable_url, "PUT", enable_payload, error=err)

    if not resp2.ok:
        err = _handle_http_error(resp2, "bounce_port (re-enable)")
        logger.error(err)
        return _build_result("failed", disable_url, "PUT", enable_payload,
                             http_status=resp2.status_code, error=err)

    logger.info("Port re-enabled", extra={"device_id": device_id, "port_id": port_id})
    return _build_result("success", disable_url, "PUT", enable_payload,
                         http_status=resp2.status_code,
                         response_body=_safe_json(resp2))


def _exec_push_ap_config(
    action: dict[str, Any],
    session: requests.Session,
    base_url: str,
    dry_run: bool,
) -> dict[str, Any]:
    """Push an AP config change (channel/power) — Tier 2.

    Endpoint: PUT /sites/{site_id}/devices/{device_id}

    Parameters
    ----------
    action : dict[str, Any]
        Marvis Action with decision fields attached.
    session : requests.Session
        Authenticated Mist API session.
    base_url : str
        API base URL.
    dry_run : bool
        If True, log intent only.

    Returns
    -------
    dict[str, Any]
        Execution result dict.
    """
    site_id   = action.get("site_id", "unknown")
    device_id = action.get("action_target",
                           action.get("ap_id", action.get("device_id", "unknown")))
    url       = f"{base_url}/sites/{site_id}/devices/{device_id}"

    # Request auto-channel/auto-power assignment by clearing overrides.
    payload = {
        "radio_config": {
            "band_24": {"channel": 0, "power": 0},   # 0 = auto
            "band_5":  {"channel": 0, "power": 0},
        }
    }

    if dry_run:
        logger.info(
            "[DRY RUN] Would push AP config (auto channel/power reset)",
            extra={"url": url, "device_id": device_id, "payload": payload},
        )
        return _build_result("dry_run", url, "PUT", payload)

    try:
        resp = _put(session, url, payload)
    except requests.RequestException as exc:
        err = f"Network error during push_ap_config: {exc}"
        logger.error(err)
        return _build_result("failed", url, "PUT", payload, error=err)

    if not resp.ok:
        err = _handle_http_error(resp, "push_ap_config")
        logger.error(err)
        return _build_result("failed", url, "PUT", payload,
                             http_status=resp.status_code, error=err)

    logger.info("AP config pushed",
                extra={"site_id": site_id, "device_id": device_id})
    return _build_result("success", url, "PUT", payload,
                         http_status=resp.status_code,
                         response_body=_safe_json(resp))


def _exec_disable_reenable_wlan(
    action: dict[str, Any],
    session: requests.Session,
    base_url: str,
    dry_run: bool,
) -> dict[str, Any]:
    """Disable then re-enable a WLAN — Tier 2.

    Endpoint: PUT /sites/{site_id}/devices/{device_id}  (WLAN via device config)

    Parameters
    ----------
    action : dict[str, Any]
        Marvis Action with decision fields attached.
    session : requests.Session
        Authenticated Mist API session.
    base_url : str
        API base URL.
    dry_run : bool
        If True, log intent only.

    Returns
    -------
    dict[str, Any]
        Execution result dict.
    """
    site_id = action.get("site_id", "unknown")
    wlan_id = action.get("action_target",
                         action.get("wlan_id", "unknown"))
    url     = f"{base_url}/sites/{site_id}/wlans/{wlan_id}"

    disable_payload = {"enabled": False}
    enable_payload  = {"enabled": True}

    if dry_run:
        logger.info(
            "[DRY RUN] Would disable then re-enable WLAN",
            extra={
                "url": url,
                "wlan_id": wlan_id,
                "disable_payload": disable_payload,
                "enable_payload": enable_payload,
            },
        )
        return _build_result("dry_run", url, "PUT", disable_payload)

    # Disable
    try:
        resp = _put(session, url, disable_payload)
    except requests.RequestException as exc:
        err = f"Network error disabling WLAN: {exc}"
        logger.error(err)
        return _build_result("failed", url, "PUT", disable_payload, error=err)

    if not resp.ok:
        err = _handle_http_error(resp, "disable_wlan")
        logger.error(err)
        return _build_result("failed", url, "PUT", disable_payload,
                             http_status=resp.status_code, error=err)

    logger.info("WLAN disabled", extra={"wlan_id": wlan_id})

    # Re-enable
    try:
        resp2 = _put(session, url, enable_payload)
    except requests.RequestException as exc:
        err = f"Network error re-enabling WLAN: {exc}"
        logger.error(err)
        return _build_result("failed", url, "PUT", enable_payload, error=err)

    if not resp2.ok:
        err = _handle_http_error(resp2, "reenable_wlan")
        logger.error(err)
        return _build_result("failed", url, "PUT", enable_payload,
                             http_status=resp2.status_code, error=err)

    logger.info("WLAN re-enabled", extra={"wlan_id": wlan_id})
    return _build_result("success", url, "PUT", enable_payload,
                         http_status=resp2.status_code,
                         response_body=_safe_json(resp2))


def _exec_restart_device(
    action: dict[str, Any],
    session: requests.Session,
    base_url: str,
    dry_run: bool,
) -> dict[str, Any]:
    """Restart a device — Tier 3.

    Endpoint: POST /sites/{site_id}/devices/{device_id}/restart

    Parameters
    ----------
    action : dict[str, Any]
        Marvis Action with decision fields attached.
    session : requests.Session
        Authenticated Mist API session.
    base_url : str
        API base URL.
    dry_run : bool
        If True, log intent only.

    Returns
    -------
    dict[str, Any]
        Execution result dict.
    """
    site_id   = action.get("site_id", "unknown")
    device_id = action.get("action_target",
                           action.get("device_id", "unknown"))
    url       = f"{base_url}/sites/{site_id}/devices/{device_id}/restart"
    payload: dict = {}

    if dry_run:
        logger.info(
            "[DRY RUN] Would POST device restart",
            extra={"url": url, "site_id": site_id, "device_id": device_id},
        )
        return _build_result("dry_run", url, "POST", payload)

    try:
        resp = _post(session, url, payload)
    except requests.RequestException as exc:
        err = f"Network error during restart_device: {exc}"
        logger.error(err)
        return _build_result("failed", url, "POST", payload, error=err)

    if not resp.ok:
        err = _handle_http_error(resp, "restart_device")
        logger.error(err)
        return _build_result("failed", url, "POST", payload,
                             http_status=resp.status_code, error=err)

    logger.info("Device restart triggered",
                extra={"site_id": site_id, "device_id": device_id})
    return _build_result("success", url, "POST", payload,
                         http_status=resp.status_code,
                         response_body=_safe_json(resp))


def _exec_bulk_config_push(
    action: dict[str, Any],
    session: requests.Session,
    base_url: str,
    dry_run: bool,
) -> dict[str, Any]:
    """Push a bulk config update across a site — Tier 3.

    Endpoint: PUT /sites/{site_id}/devices/{device_id} applied to each
    device in the site's device stats.  In practice this flags the site
    for a config sync push via the Mist API.

    Parameters
    ----------
    action : dict[str, Any]
        Marvis Action with decision fields attached.
    session : requests.Session
        Authenticated Mist API session.
    base_url : str
        API base URL.
    dry_run : bool
        If True, log intent only.

    Returns
    -------
    dict[str, Any]
        Execution result dict.
    """
    site_id = action.get("site_id", "unknown")
    # Bulk push uses the site-level config sync endpoint.
    url     = f"{base_url}/sites/{site_id}/devices/config_sync"
    payload = {"force": True}

    if dry_run:
        logger.info(
            "[DRY RUN] Would trigger bulk config sync for site",
            extra={"url": url, "site_id": site_id, "payload": payload},
        )
        return _build_result("dry_run", url, "POST", payload)

    try:
        resp = _post(session, url, payload)
    except requests.RequestException as exc:
        err = f"Network error during bulk_config_push: {exc}"
        logger.error(err)
        return _build_result("failed", url, "POST", payload, error=err)

    if not resp.ok:
        err = _handle_http_error(resp, "bulk_config_push")
        logger.error(err)
        return _build_result("failed", url, "POST", payload,
                             http_status=resp.status_code, error=err)

    logger.info("Bulk config push triggered", extra={"site_id": site_id})
    return _build_result("success", url, "POST", payload,
                         http_status=resp.status_code,
                         response_body=_safe_json(resp))


# --------------------------------------------------------------------------- #
# Dispatch table
# --------------------------------------------------------------------------- #

_HANDLERS = {
    "clear_client_session":   _exec_clear_client_session,
    "marvis_rca_query":       _exec_marvis_rca_query,
    "bounce_port":            _exec_bounce_port,
    "push_ap_config":         _exec_push_ap_config,
    "disable_reenable_wlan":  _exec_disable_reenable_wlan,
    "restart_device":         _exec_restart_device,
    "bulk_config_push":       _exec_bulk_config_push,
}


# --------------------------------------------------------------------------- #
# Public executor interface
# --------------------------------------------------------------------------- #

def execute(
    action: dict[str, Any],
    dry_run: bool = True,
    session: requests.Session | None = None,
    base_url: str | None = None,
    org_id: str | None = None,
) -> dict[str, Any]:
    """Execute (or simulate) the remediation action for a single Marvis Action.

    Reads ``action_type``, ``action_permitted``, and ``action_target`` from
    the action dict (attached by decision.py) and dispatches to the
    appropriate handler.

    Parameters
    ----------
    action : dict[str, Any]
        Marvis Action with decision fields attached.
    dry_run : bool
        If True (default), no live API calls are made.  The audit log will
        show exactly what would have been called.
    session : requests.Session | None
        Authenticated session.  Created if not provided.
    base_url : str | None
        API base URL.  Read from environment if not provided.
    org_id : str | None
        Mist org ID.  Read from environment if not provided.

    Returns
    -------
    dict[str, Any]
        The action dict with ``execution_result`` key attached.
    """
    if session is None:
        session = build_session()
    if base_url is None:
        base_url = get_base_url()
    if org_id is None:
        from utils.auth import get_org_id
        org_id = get_org_id()

    action_type = action.get("action_type", "unknown")
    permitted   = action.get("action_permitted", False)
    action_id   = action.get("id", "unknown")

    # --- Skip actions that were not permitted by decision.py ---
    if not permitted:
        skip_reason = action.get("skip_reason", "action not permitted")
        logger.info(
            "Execution skipped — action not permitted",
            extra={"action_id": action_id, "reason": skip_reason},
        )
        action["execution_result"] = _build_result(
            "skipped",
            api_endpoint="n/a",
            http_method="n/a",
            error=skip_reason,
        )
        return action

    # --- Look up handler ---
    handler = _HANDLERS.get(action_type)
    if handler is None:
        err = f"No handler registered for action_type='{action_type}'"
        logger.error(err, extra={"action_id": action_id})
        action["execution_result"] = _build_result(
            "failed",
            api_endpoint="n/a",
            http_method="n/a",
            error=err,
        )
        return action

    logger.info(
        "Executing action",
        extra={
            "action_id":   action_id,
            "action_type": action_type,
            "target":      action.get("action_target"),
            "dry_run":     dry_run,
            "tier":        action.get("action_tier"),
        },
    )

    # --- Dispatch ---
    if action_type == "marvis_rca_query":
        result = handler(action, session, base_url, dry_run, org_id)
    else:
        result = handler(action, session, base_url, dry_run)

    action["execution_result"] = result

    log_level = "info" if result["action_result"] in ("success", "dry_run") else "error"
    getattr(logger, log_level)(
        "Execution complete",
        extra={
            "action_id":     action_id,
            "action_type":   action_type,
            "action_result": result["action_result"],
            "http_status":   result.get("http_status"),
            "error":         result.get("error"),
        },
    )

    return action


def execute_all(
    actions: list[dict[str, Any]],
    dry_run: bool = True,
    session: requests.Session | None = None,
    base_url: str | None = None,
    org_id: str | None = None,
) -> list[dict[str, Any]]:
    """Execute remediation for all actions in the list.

    Parameters
    ----------
    actions : list[dict[str, Any]]
        Marvis Actions with decision fields attached.
    dry_run : bool
        Passed through to :func:`execute`.
    session : requests.Session | None
        Shared authenticated session.
    base_url : str | None
        API base URL.
    org_id : str | None
        Mist org ID.

    Returns
    -------
    list[dict[str, Any]]
        Actions with ``execution_result`` attached to each.
    """
    if session is None:
        session = build_session()
    if base_url is None:
        base_url = get_base_url()
    if org_id is None:
        from utils.auth import get_org_id
        org_id = get_org_id()

    mode = "DRY RUN" if dry_run else "LIVE"
    logger.info(
        "Execution run started",
        extra={"mode": mode, "action_count": len(actions)},
    )

    results = {"success": 0, "dry_run": 0, "failed": 0, "skipped": 0}

    for action in actions:
        execute(action, dry_run=dry_run, session=session,
                base_url=base_url, org_id=org_id)
        outcome = action.get("execution_result", {}).get("action_result", "failed")
        results[outcome] = results.get(outcome, 0) + 1

    logger.info(
        "Execution run complete",
        extra={"mode": mode, "results": results},
    )

    return actions


# --------------------------------------------------------------------------- #
# Private helpers
# --------------------------------------------------------------------------- #

def _safe_json(resp: requests.Response) -> dict | None:
    """Safely parse a response body as JSON.

    Parameters
    ----------
    resp : requests.Response
        Response to parse.

    Returns
    -------
    dict | None
        Parsed body, or None if parsing fails or body is empty.
    """
    try:
        body = resp.json()
        return body if isinstance(body, dict) else {"raw": body}
    except ValueError:
        return None