# Python 3.11+
# output/webhook.py — Optional webhook dispatcher.
#
# If webhook_url is configured in config.yaml, POSTs a structured JSON
# payload to that URL after every run.  Failures are logged as warnings
# and never propagate — a broken webhook must never abort a remediation run.
#
# Payload structure:
# {
#   "run_id":        "uuid4",
#   "timestamp":     "ISO8601 UTC",
#   "mode":          "dry_run | live",
#   "org_id":        "...",
#   "duration_seconds": float,
#   "summary": {
#     "total_actions":   int,
#     "success":         int,
#     "dry_run":         int,
#     "skipped":         int,
#     "failed":          int,
#     "tier_1":          int,
#     "tier_2":          int,
#     "tier_3":          int,
#   },
#   "failures": [ ...subset of entries where action_result == "failed"... ],
#   "entries":  [ ...all audit log entries... ],
# }
#
# Retries: up to 3 attempts with exponential backoff on connection errors
# or HTTP 5xx responses.  HTTP 4xx is not retried (misconfiguration).

from __future__ import annotations

import time
import random
from datetime import datetime, timezone
from typing import Any

import requests

from utils.logger import get_logger

logger = get_logger(__name__)

# Retry configuration — intentionally separate from the Mist API rate limiter.
_MAX_RETRIES:        int   = 3
_BASE_BACKOFF:       float = 1.0
_MAX_BACKOFF:        float = 16.0
_CONNECT_TIMEOUT:    int   = 5    # seconds to establish connection
_READ_TIMEOUT:       int   = 15   # seconds to receive full response


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #

def post_results(
    entries: list[dict[str, Any]],
    run_id: str,
    dry_run: bool,
    org_id: str,
    started_at: datetime,
    finished_at: datetime,
    webhook_url: str,
) -> bool:
    """POST run results to the configured webhook URL.

    Non-fatal: all exceptions are caught and logged.  Returns False on any
    failure so the caller can log the outcome without crashing the run.

    Parameters
    ----------
    entries : list[dict[str, Any]]
        Audit log entries from ``output.audit_log.write_run()``.
    run_id : str
        UUID4 run identifier.
    dry_run : bool
        Whether the run executed in dry-run mode.
    org_id : str
        Mist organisation ID.
    started_at : datetime
        UTC datetime when the run started.
    finished_at : datetime
        UTC datetime when the run finished.
    webhook_url : str
        Full URL to POST to.  If empty or blank, this function is a no-op.

    Returns
    -------
    bool
        True if the webhook received a 2xx response, False otherwise.
    """
    if not webhook_url or not webhook_url.strip():
        logger.debug("Webhook URL not configured — skipping")
        return False

    payload = _build_payload(
        entries=entries,
        run_id=run_id,
        dry_run=dry_run,
        org_id=org_id,
        started_at=started_at,
        finished_at=finished_at,
    )

    return _dispatch(webhook_url.strip(), payload)


# --------------------------------------------------------------------------- #
# Payload builder
# --------------------------------------------------------------------------- #

def _build_payload(
    entries: list[dict[str, Any]],
    run_id: str,
    dry_run: bool,
    org_id: str,
    started_at: datetime,
    finished_at: datetime,
) -> dict[str, Any]:
    """Construct the webhook POST body.

    Parameters
    ----------
    entries : list[dict[str, Any]]
        Audit log entries.
    run_id : str
        Run UUID.
    dry_run : bool
        Run mode flag.
    org_id : str
        Org identifier.
    started_at : datetime
        Run start time (UTC).
    finished_at : datetime
        Run finish time (UTC).

    Returns
    -------
    dict[str, Any]
        JSON-serialisable payload dict.
    """
    duration = (finished_at - started_at).total_seconds()

    # Result counts.
    result_counts: dict[str, int] = {
        "success": 0, "dry_run": 0, "skipped": 0, "failed": 0,
    }
    tier_counts: dict[str, int] = {"tier_1": 0, "tier_2": 0, "tier_3": 0}

    for e in entries:
        result = e.get("action_result", "unknown")
        result_counts[result] = result_counts.get(result, 0) + 1
        tier = e.get("action_tier", 0)
        if tier in (1, 2, 3):
            tier_counts[f"tier_{tier}"] += 1

    # Only include failed entries in the failures block to keep payload size
    # reasonable.  Full entries list is included for consumers that want it.
    failures = [e for e in entries if e.get("action_result") == "failed"]

    payload: dict[str, Any] = {
        "run_id":            run_id,
        "timestamp":         finished_at.isoformat(),
        "mode":              "dry_run" if dry_run else "live",
        "org_id":            org_id,
        "duration_seconds":  round(duration, 2),
        "summary": {
            "total_actions": len(entries),
            **result_counts,
            **tier_counts,
        },
        "failures": failures,
        "entries":  entries,
    }

    return payload


# --------------------------------------------------------------------------- #
# HTTP dispatch with retry
# --------------------------------------------------------------------------- #

def _dispatch(url: str, payload: dict[str, Any]) -> bool:
    """POST the payload to the webhook URL with exponential-backoff retries.

    Retries on connection errors and HTTP 5xx responses.  Does not retry on
    HTTP 4xx (caller misconfiguration — retrying would not help).

    Parameters
    ----------
    url : str
        Destination URL.
    payload : dict[str, Any]
        JSON-serialisable body.

    Returns
    -------
    bool
        True if a 2xx response was received, False on all failure paths.
    """
    last_error: str = ""

    for attempt in range(_MAX_RETRIES + 1):
        try:
            resp = requests.post(
                url,
                json=payload,
                timeout=(_CONNECT_TIMEOUT, _READ_TIMEOUT),
                headers={"Content-Type": "application/json"},
            )

            if resp.ok:
                logger.info(
                    "Webhook delivered successfully",
                    extra={
                        "url":         _redact_url(url),
                        "http_status": resp.status_code,
                        "attempt":     attempt + 1,
                    },
                )
                return True

            # 4xx — misconfiguration, do not retry.
            if 400 <= resp.status_code < 500:
                logger.warning(
                    "Webhook returned client error — not retrying",
                    extra={
                        "url":         _redact_url(url),
                        "http_status": resp.status_code,
                        "body":        resp.text[:200],
                    },
                )
                return False

            # 5xx — transient server error, retry with backoff.
            last_error = f"HTTP {resp.status_code}"
            logger.warning(
                "Webhook server error — will retry",
                extra={
                    "url":         _redact_url(url),
                    "http_status": resp.status_code,
                    "attempt":     attempt + 1,
                    "max_retries": _MAX_RETRIES,
                },
            )

        except requests.Timeout:
            last_error = "connection or read timeout"
            logger.warning(
                "Webhook timed out",
                extra={
                    "url":         _redact_url(url),
                    "attempt":     attempt + 1,
                    "max_retries": _MAX_RETRIES,
                },
            )

        except requests.ConnectionError as exc:
            last_error = f"connection error: {exc}"
            logger.warning(
                "Webhook connection error",
                extra={
                    "url":         _redact_url(url),
                    "error":       str(exc)[:120],
                    "attempt":     attempt + 1,
                    "max_retries": _MAX_RETRIES,
                },
            )

        except Exception as exc:  # pragma: no cover — defensive catch-all
            last_error = f"unexpected error: {exc}"
            logger.error(
                "Unexpected webhook error",
                extra={"url": _redact_url(url), "error": str(exc)},
            )
            return False

        # Sleep before retry (not after the final attempt).
        if attempt < _MAX_RETRIES:
            sleep = _jitter_backoff(attempt)
            logger.debug(
                "Retrying webhook after backoff",
                extra={"sleep_seconds": round(sleep, 2), "attempt": attempt + 1},
            )
            time.sleep(sleep)

    logger.error(
        "Webhook delivery failed after all retries",
        extra={
            "url":         _redact_url(url),
            "last_error":  last_error,
            "max_retries": _MAX_RETRIES,
        },
    )
    return False


# --------------------------------------------------------------------------- #
# Private helpers
# --------------------------------------------------------------------------- #

def _jitter_backoff(attempt: int) -> float:
    """Compute a jittered backoff duration for a given attempt index.

    Parameters
    ----------
    attempt : int
        Zero-based attempt index.

    Returns
    -------
    float
        Seconds to sleep (full jitter in [0, cap]).
    """
    cap = min(_MAX_BACKOFF, _BASE_BACKOFF * (2 ** attempt))
    return random.uniform(0, cap)


def _redact_url(url: str) -> str:
    """Redact query-string parameters from a URL before logging.

    Webhook URLs sometimes contain API keys in the query string
    (e.g. Slack, PagerDuty, Teams).  Strip everything after ``?``.

    Parameters
    ----------
    url : str
        The URL to sanitise.

    Returns
    -------
    str
        URL with query string removed.
    """
    return url.split("?")[0]