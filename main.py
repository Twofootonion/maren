# Python 3.11+
# main.py — MAREN entry point and orchestration loop.
#
# Wires together all core and output modules into a single run cycle:
#
#   1. Load config (config.yaml + environment variables)
#   2. Build authenticated session
#   3. Poll Marvis Actions (poller)
#   4. Correlate telemetry (correlator)
#   5. Score and rank actions (scorer)
#   6. Decide tier and action type (decision)
#   7. Execute or simulate actions (executor)
#   8. Write audit log entries (audit_log)
#   9. Generate Markdown run summary (summary)
#  10. POST results to webhook if configured (webhook)
#
# Modes:
#   DRY RUN (default): steps 1-6 run normally; step 7 logs exact API calls
#                      that would be made without executing them.
#   LIVE:              all steps execute; tier gates enforced by decision.py.
#
# Invocation:
#   python main.py                        # single dry-run cycle
#   python main.py --live                 # single live cycle
#   python main.py --loop                 # continuous dry-run polling loop
#   python main.py --live --loop          # continuous live polling loop
#   python main.py --config path/to.yaml  # custom config file

from __future__ import annotations

import argparse
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from core.correlator import correlate_all
from core.decision import DecisionConfig, decide_all
from core.executor import execute_all
from core.poller import poll, PollerError
from core.scorer import score_all
from output.audit_log import new_run_id, write_run
from output.summary import generate_summary
from output.webhook import post_results
from utils.auth import AuthError, build_session, get_base_url, get_org_id
from utils.logger import get_logger, configure_root_level

logger = get_logger(__name__)

# Set when SIGINT/SIGTERM received — loop checks this to exit cleanly.
_shutdown_requested = False


# --------------------------------------------------------------------------- #
# Config loading
# --------------------------------------------------------------------------- #

DEFAULT_CONFIG: dict[str, Any] = {
    "dry_run":                True,
    "poll_interval_seconds":  300,
    "min_score_threshold":    2.0,
    "enable_tier2":           False,
    "enable_tier3":           False,
    "tier3_confirm":          False,
    "webhook_url":            "",
    "log_level":              "INFO",
    "audit_log_path":         "maren_audit.json",
    "summary_output_dir":     "summaries",
}


def load_config(config_path: str = "config.yaml") -> dict[str, Any]:
    """Load configuration from a YAML file, falling back to defaults.

    Values in the YAML file override defaults.  Command-line flags (--live,
    --loop) are merged later in :func:`main`.

    Parameters
    ----------
    config_path : str
        Path to the YAML config file.  Missing file is non-fatal — defaults
        are used and a warning is logged.

    Returns
    -------
    dict[str, Any]
        Merged configuration dictionary.
    """
    config = DEFAULT_CONFIG.copy()

    path = Path(config_path)
    if not path.exists():
        logger.warning(
            "Config file not found — using defaults",
            extra={"config_path": config_path},
        )
        return config

    try:
        with path.open("r", encoding="utf-8") as fh:
            file_config = yaml.safe_load(fh) or {}
        if not isinstance(file_config, dict):
            logger.warning("config.yaml did not parse to a dict — using defaults")
            return config
        config.update(file_config)
        logger.info("Config loaded", extra={"config_path": config_path})
    except yaml.YAMLError as exc:
        logger.warning(
            "Failed to parse config.yaml — using defaults",
            extra={"error": str(exc)},
        )

    return config


# --------------------------------------------------------------------------- #
# Single run cycle
# --------------------------------------------------------------------------- #

def run_cycle(
    config: dict[str, Any],
    session: Any,
    org_id: str,
    base_url: str,
    run_id: str,
) -> dict[str, Any]:
    """Execute one complete remediation cycle.

    Parameters
    ----------
    config : dict[str, Any]
        Merged configuration dict from :func:`load_config`.
    session : requests.Session
        Authenticated Mist API session.
    org_id : str
        Mist organisation ID.
    base_url : str
        Mist API base URL.
    run_id : str
        UUID4 for this run — groups all audit log entries.

    Returns
    -------
    dict[str, Any]
        Run result metadata::

            {
                "run_id":       str,
                "started_at":   datetime,
                "finished_at":  datetime,
                "action_count": int,
                "entries":      list,
                "summary_path": str,
                "webhook_ok":   bool,
            }
    """
    dry_run      = config["dry_run"]
    mode_label   = "DRY RUN" if dry_run else "LIVE"
    started_at   = datetime.now(tz=timezone.utc)

    logger.info(
        "=" * 60,
    )
    logger.info(
        "MAREN run cycle starting",
        extra={
            "run_id":    run_id,
            "mode":      mode_label,
            "org_id":    org_id,
            "threshold": config["min_score_threshold"],
        },
    )

    decision_config = DecisionConfig(
        dry_run=dry_run,
        enable_tier2=config["enable_tier2"],
        enable_tier3=config["enable_tier3"],
        tier3_confirm=config["tier3_confirm"],
        min_score_threshold=config["min_score_threshold"],
    )

    # ------------------------------------------------------------------ #
    # Step 1: Poll
    # ------------------------------------------------------------------ #
    try:
        actions = poll(session=session, org_id=org_id, base_url=base_url)
    except PollerError as exc:
        logger.error("Poll failed — aborting run cycle", extra={"error": str(exc)})
        finished_at = datetime.now(tz=timezone.utc)
        return {
            "run_id":       run_id,
            "started_at":   started_at,
            "finished_at":  finished_at,
            "action_count": 0,
            "entries":      [],
            "summary_path": None,
            "webhook_ok":   False,
        }

    if not actions:
        logger.info("No Marvis Actions found — run cycle complete")
        finished_at = datetime.now(tz=timezone.utc)
        entries = write_run([], run_id, dry_run, config["audit_log_path"])
        summary_path = generate_summary(
            entries=entries,
            run_id=run_id,
            dry_run=dry_run,
            org_id=org_id,
            started_at=started_at,
            finished_at=finished_at,
            output_dir=config["summary_output_dir"],
        )
        return {
            "run_id":       run_id,
            "started_at":   started_at,
            "finished_at":  finished_at,
            "action_count": 0,
            "entries":      [],
            "summary_path": summary_path,
            "webhook_ok":   False,
        }

    # ------------------------------------------------------------------ #
    # Step 2: Correlate
    # ------------------------------------------------------------------ #
    correlate_all(actions, session=session, base_url=base_url)

    # ------------------------------------------------------------------ #
    # Step 3: Score
    # ------------------------------------------------------------------ #
    scored = score_all(actions, min_score_threshold=config["min_score_threshold"])

    # ------------------------------------------------------------------ #
    # Step 4: Decide
    # ------------------------------------------------------------------ #
    decided = decide_all(scored, config=decision_config)

    # ------------------------------------------------------------------ #
    # Step 5: Execute
    # ------------------------------------------------------------------ #
    executed = execute_all(
        decided,
        dry_run=dry_run,
        session=session,
        base_url=base_url,
        org_id=org_id,
    )

    # ------------------------------------------------------------------ #
    # Step 6: Audit log
    # ------------------------------------------------------------------ #
    finished_at = datetime.now(tz=timezone.utc)
    entries = write_run(
        executed,
        run_id=run_id,
        dry_run=dry_run,
        audit_log_path=config["audit_log_path"],
    )

    # ------------------------------------------------------------------ #
    # Step 7: Summary
    # ------------------------------------------------------------------ #
    summary_path = generate_summary(
        entries=entries,
        run_id=run_id,
        dry_run=dry_run,
        org_id=org_id,
        started_at=started_at,
        finished_at=finished_at,
        output_dir=config["summary_output_dir"],
    )

    # ------------------------------------------------------------------ #
    # Step 8: Webhook
    # ------------------------------------------------------------------ #
    webhook_ok = False
    if config.get("webhook_url"):
        webhook_ok = post_results(
            entries=entries,
            run_id=run_id,
            dry_run=dry_run,
            org_id=org_id,
            started_at=started_at,
            finished_at=finished_at,
            webhook_url=config["webhook_url"],
        )

    # ------------------------------------------------------------------ #
    # Run summary log line
    # ------------------------------------------------------------------ #
    duration = (finished_at - started_at).total_seconds()
    result_counts = {r: 0 for r in ("success", "dry_run", "skipped", "failed")}
    for e in entries:
        k = e.get("action_result", "unknown")
        result_counts[k] = result_counts.get(k, 0) + 1

    logger.info(
        "MAREN run cycle complete",
        extra={
            "run_id":       run_id,
            "mode":         mode_label,
            "duration_s":   round(duration, 2),
            "actions":      len(entries),
            "results":      result_counts,
            "summary":      summary_path,
            "webhook_ok":   webhook_ok,
        },
    )

    return {
        "run_id":       run_id,
        "started_at":   started_at,
        "finished_at":  finished_at,
        "action_count": len(entries),
        "entries":      entries,
        "summary_path": summary_path,
        "webhook_ok":   webhook_ok,
    }


# --------------------------------------------------------------------------- #
# Signal handlers
# --------------------------------------------------------------------------- #

def _handle_signal(signum: int, frame: Any) -> None:  # noqa: ARG001
    """Set the shutdown flag on SIGINT or SIGTERM.

    Parameters
    ----------
    signum : int
        Signal number.
    frame : Any
        Current stack frame (unused).
    """
    global _shutdown_requested
    logger.info(
        "Shutdown signal received — will exit after current cycle",
        extra={"signal": signum},
    )
    _shutdown_requested = True


# --------------------------------------------------------------------------- #
# CLI and main loop
# --------------------------------------------------------------------------- #

def parse_args() -> argparse.Namespace:
    """Parse command-line arguments.

    Returns
    -------
    argparse.Namespace
        Parsed arguments with attributes: live, loop, config.
    """
    parser = argparse.ArgumentParser(
        prog="maren",
        description=(
            "MAREN — Marvis Autonomous Remediation Engine for Networks. "
            "Defaults to dry-run mode. Pass --live to execute remediations."
        ),
    )
    parser.add_argument(
        "--live",
        action="store_true",
        default=False,
        help="Execute live remediation actions (default: dry-run only).",
    )
    parser.add_argument(
        "--loop",
        action="store_true",
        default=False,
        help="Run continuously, polling at poll_interval_seconds from config.",
    )
    parser.add_argument(
        "--config",
        default="config.yaml",
        help="Path to config YAML file (default: config.yaml).",
    )
    return parser.parse_args()


def main() -> int:
    """Main entry point for MAREN.

    Loads config, validates credentials, then runs one cycle or a continuous
    polling loop depending on CLI flags.

    Returns
    -------
    int
        Exit code: 0 on success, 1 on startup failure.
    """
    global _shutdown_requested

    args = parse_args()

    # Load config first so we can set log level before doing anything else.
    config = load_config(args.config)

    # CLI --live flag overrides config file dry_run setting.
    if args.live:
        config["dry_run"] = False
        logger.warning(
            "LIVE MODE ENABLED — remediation actions will be executed",
        )
    else:
        config["dry_run"] = True

    # Apply log level from config.
    configure_root_level(config.get("log_level", "INFO"))

    # ------------------------------------------------------------------ #
    # Credential validation — fail fast before any polling.
    # ------------------------------------------------------------------ #
    try:
        org_id   = get_org_id()
        base_url = get_base_url()
        session  = build_session()
    except AuthError as exc:
        logger.error("Authentication failed — cannot start", extra={"error": str(exc)})
        return 1

    # Register signal handlers for clean loop exit.
    signal.signal(signal.SIGINT,  _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    mode_label = "DRY RUN" if config["dry_run"] else "LIVE"
    logger.info(
        "MAREN starting",
        extra={
            "mode":             mode_label,
            "loop":             args.loop,
            "poll_interval_s":  config["poll_interval_seconds"],
            "org_id":           org_id,
            "tier2_enabled":    config["enable_tier2"],
            "tier3_enabled":    config["enable_tier3"],
        },
    )

    # ------------------------------------------------------------------ #
    # Run loop
    # ------------------------------------------------------------------ #
    exit_code = 0

    while not _shutdown_requested:
        run_id = new_run_id()

        try:
            run_cycle(
                config=config,
                session=session,
                org_id=org_id,
                base_url=base_url,
                run_id=run_id,
            )
        except Exception as exc:  # pragma: no cover — unexpected top-level error
            logger.error(
                "Unexpected error in run cycle",
                extra={"run_id": run_id, "error": str(exc)},
                exc_info=True,
            )
            exit_code = 1

        if not args.loop or _shutdown_requested:
            break

        interval = config["poll_interval_seconds"]
        logger.info(
            "Sleeping until next poll",
            extra={"interval_seconds": interval},
        )

        # Sleep in 1-second increments so SIGINT is handled promptly.
        for _ in range(interval):
            if _shutdown_requested:
                break
            time.sleep(1)

    logger.info("MAREN exiting", extra={"exit_code": exit_code})
    return exit_code


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #

if __name__ == "__main__":
    sys.exit(main())