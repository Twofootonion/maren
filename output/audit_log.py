# Python 3.11+
# output/audit_log.py — Structured JSON audit log writer.
#
# Writes one JSON object per action to a newline-delimited JSON (NDJSON) file
# (maren_audit.json by default).  Each entry exactly matches the schema from
# the spec plus an execution_result block from executor.py.
#
# The file is appended to on every run so historical records are preserved.
# A run_id (UUID4) groups all entries from a single execution cycle.
#
# Schema per entry:
# {
#   "run_id":                 "uuid4",
#   "timestamp":              "ISO8601 UTC",
#   "mode":                   "dry_run | live",
#   "site_id":                "...",
#   "site_name":              "...",
#   "issue_type":             "...",
#   "marvis_action":          { ...raw marvis action object... },
#   "correlated_telemetry":   { ...enrichment data... },
#   "priority_score":         12.0,
#   "severity":               "critical",
#   "blast_radius":           52,
#   "recurrence":             4,
#   "action_tier":            1,
#   "action_taken":           "cleared_client_session | would_clear_client_session",
#   "action_target":          "...",
#   "action_result":          "success | dry_run | failed | skipped",
#   "error":                  null,
#   "remediation_reasoning":  "Human-readable explanation"
# }

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any

from utils.logger import get_logger

logger = get_logger(__name__)

# Thread lock — multiple coroutines/threads must not interleave writes.
_write_lock = Lock()

# Map executor action_type strings → past-tense audit log labels.
_ACTION_TAKEN_MAP: dict[str, tuple[str, str]] = {
    # (live_label, dry_run_label)
    "clear_client_session":   ("cleared_client_session",   "would_clear_client_session"),
    "marvis_rca_query":       ("triggered_marvis_rca",     "would_trigger_marvis_rca"),
    "bounce_port":            ("bounced_switch_port",      "would_bounce_switch_port"),
    "push_ap_config":         ("pushed_ap_config",         "would_push_ap_config"),
    "disable_reenable_wlan":  ("disabled_reenabled_wlan",  "would_disable_reenable_wlan"),
    "restart_device":         ("restarted_device",         "would_restart_device"),
    "bulk_config_push":       ("bulk_config_pushed",       "would_bulk_config_push"),
    "none":                   ("skipped",                  "skipped"),
}


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #

def new_run_id() -> str:
    """Generate a fresh UUID4 run identifier.

    Parameters
    ----------
    None.

    Returns
    -------
    str
        A UUID4 string, e.g. ``"3f2504e0-4f89-11d3-9a0c-0305e82c3301"``.
    """
    return str(uuid.uuid4())


def build_entry(
    action: dict[str, Any],
    run_id: str,
    dry_run: bool,
) -> dict[str, Any]:
    """Construct a single audit log entry from a fully-processed action.

    Reads scoring, decision, and execution fields that were attached by
    scorer.py, decision.py, and executor.py respectively.

    Parameters
    ----------
    action : dict[str, Any]
        A fully-processed Marvis Action dict.
    run_id : str
        UUID4 run identifier from :func:`new_run_id`.
    dry_run : bool
        Whether the run was in dry-run mode.

    Returns
    -------
    dict[str, Any]
        Audit log entry conforming to the spec schema.
    """
    mode         = "dry_run" if dry_run else "live"
    action_type  = action.get("action_type", "none")
    exec_result  = action.get("execution_result") or {}
    raw_result   = exec_result.get("action_result", "skipped")

    # Build human-readable action_taken label.
    label_pair   = _ACTION_TAKEN_MAP.get(action_type, (action_type, f"would_{action_type}"))
    action_taken = label_pair[1] if dry_run else label_pair[0]
    # Skipped/failed actions override the label regardless of mode.
    if raw_result == "skipped":
        action_taken = "skipped"
    elif raw_result == "failed":
        action_taken = f"failed_{action_type}"

    # Strip out internal processing keys that shouldn't appear in the audit
    # log's marvis_action block.  We preserve the original API fields.
    _internal_keys = {
        "site_name", "site_timezone", "correlated_telemetry",
        "severity_weight", "blast_radius_factor", "recurrence_factor",
        "below_threshold", "action_tier", "action_type", "action_target",
        "action_permitted", "skip_reason", "remediation_reasoning",
        "execution_result", "priority_score", "severity", "blast_radius",
        "recurrence_count",
    }
    raw_marvis_action = {k: v for k, v in action.items() if k not in _internal_keys}

    # Strip large stats arrays from correlated_telemetry in the audit log to
    # keep file size manageable — keep the computed summary fields only.
    telemetry_raw  = action.get("correlated_telemetry") or {}
    telemetry_summary: dict[str, Any] = {
        "blast_radius":      telemetry_raw.get("blast_radius", 1),
        "recurrence_count":  telemetry_raw.get("recurrence_count", 0),
        "telemetry_partial": telemetry_raw.get("telemetry_partial", False),
        "client_count":      len(telemetry_raw.get("client_stats", [])),
        "device_count":      len(telemetry_raw.get("device_stats", [])),
        "client_event_count": len(telemetry_raw.get("client_events", [])),
        "device_event_count": len(telemetry_raw.get("device_events", [])),
    }

    entry: dict[str, Any] = {
        "run_id":                run_id,
        "timestamp":             datetime.now(tz=timezone.utc).isoformat(),
        "mode":                  mode,
        "site_id":               action.get("site_id", "unknown"),
        "site_name":             action.get("site_name", "unknown"),
        "issue_type":            (
            action.get("issue_type")
            or action.get("category")
            or "unknown"
        ),
        "marvis_action":         raw_marvis_action,
        "correlated_telemetry":  telemetry_summary,
        "priority_score":        action.get("priority_score", 0.0),
        "severity":              action.get("severity", "unknown"),
        "blast_radius":          action.get("blast_radius", 1),
        "recurrence":            action.get("recurrence_count", 0),
        "action_tier":           action.get("action_tier", 0),
        "action_taken":          action_taken,
        "action_target":         action.get("action_target", "unknown"),
        "action_result":         raw_result,
        "error":                 exec_result.get("error"),
        "remediation_reasoning": action.get("remediation_reasoning", ""),
        # Extra fields beyond the spec minimum — useful for debugging.
        "api_endpoint":          exec_result.get("api_endpoint"),
        "http_method":           exec_result.get("http_method"),
        "http_status":           exec_result.get("http_status"),
        "executed_at":           exec_result.get("executed_at"),
    }

    return entry


def append_entry(
    entry: dict[str, Any],
    audit_log_path: str = "maren_audit.json",
) -> None:
    """Append a single audit entry to the NDJSON log file.

    Creates the file (and any parent directories) if it does not exist.
    Each call appends one JSON line terminated by a newline.

    Parameters
    ----------
    entry : dict[str, Any]
        Audit log entry from :func:`build_entry`.
    audit_log_path : str
        Path to the audit log file.  Defaults to ``"maren_audit.json"``.

    Raises
    ------
    OSError
        If the file cannot be opened for writing.
    """
    path = Path(audit_log_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    line = json.dumps(entry, default=str)

    with _write_lock:
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    logger.debug(
        "Audit entry written",
        extra={
            "run_id":        entry.get("run_id"),
            "action_result": entry.get("action_result"),
            "issue_type":    entry.get("issue_type"),
            "site_name":     entry.get("site_name"),
        },
    )


def write_run(
    actions: list[dict[str, Any]],
    run_id: str,
    dry_run: bool,
    audit_log_path: str = "maren_audit.json",
) -> list[dict[str, Any]]:
    """Write audit log entries for all actions in a run.

    Parameters
    ----------
    actions : list[dict[str, Any]]
        Fully-processed Marvis Actions (scorer + decision + executor applied).
    run_id : str
        UUID4 run identifier.
    dry_run : bool
        Whether the run was in dry-run mode.
    audit_log_path : str
        Path to the audit log file.

    Returns
    -------
    list[dict[str, Any]]
        The list of audit entries written (useful for summary generation).
    """
    entries: list[dict[str, Any]] = []

    for action in actions:
        try:
            entry = build_entry(action, run_id, dry_run)
            append_entry(entry, audit_log_path)
            entries.append(entry)
        except Exception as exc:
            logger.error(
                "Failed to write audit entry",
                extra={
                    "action_id": action.get("id", "unknown"),
                    "error": str(exc),
                },
            )

    logger.info(
        "Audit log written",
        extra={
            "run_id":      run_id,
            "entries":     len(entries),
            "log_path":    audit_log_path,
            "mode":        "dry_run" if dry_run else "live",
        },
    )

    return entries


def read_run(
    run_id: str,
    audit_log_path: str = "maren_audit.json",
) -> list[dict[str, Any]]:
    """Read all audit entries for a specific run_id from the log file.

    Parameters
    ----------
    run_id : str
        The run UUID to filter by.
    audit_log_path : str
        Path to the audit log file.

    Returns
    -------
    list[dict[str, Any]]
        All entries with matching run_id.  Empty list if file not found.
    """
    path = Path(audit_log_path)
    if not path.exists():
        logger.warning("Audit log not found", extra={"path": str(path)})
        return []

    entries: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as fh:
        for line_num, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                if entry.get("run_id") == run_id:
                    entries.append(entry)
            except json.JSONDecodeError as exc:
                logger.warning(
                    "Malformed JSON line in audit log",
                    extra={"line": line_num, "error": str(exc)},
                )

    return entries