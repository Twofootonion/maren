# Python 3.11+
# output/summary.py — Markdown run summary writer.
#
# Produces a human-readable Markdown file after every run containing:
#   - Run metadata (timestamp, mode, org, duration)
#   - Issues found table (site, type, score, action taken)
#   - Tier breakdown (how many Tier 1/2/3 actions)
#   - Failures and errors
#   - Skipped actions and reasons
#
# Output file: summaries/maren_summary_YYYYMMDD_HHMMSS.md
# The summaries/ directory is created if it does not exist.

from __future__ import annotations

import os
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from utils.logger import get_logger

logger = get_logger(__name__)


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #

def generate_summary(
    entries: list[dict[str, Any]],
    run_id: str,
    dry_run: bool,
    org_id: str,
    started_at: datetime,
    finished_at: datetime,
    output_dir: str = "summaries",
) -> str:
    """Generate and write a Markdown run summary file.

    Parameters
    ----------
    entries : list[dict[str, Any]]
        Audit log entries returned by ``output.audit_log.write_run()``.
    run_id : str
        UUID4 run identifier.
    dry_run : bool
        Whether the run executed in dry-run mode.
    org_id : str
        Mist organisation ID for display in the header.
    started_at : datetime
        UTC datetime when the run started.
    finished_at : datetime
        UTC datetime when the run finished.
    output_dir : str
        Directory to write summary files into.  Created if absent.

    Returns
    -------
    str
        Absolute path to the written summary file.
    """
    Path(output_dir).mkdir(parents=True, exist_ok=True)

    ts_str   = finished_at.strftime("%Y%m%d_%H%M%S")
    filename = f"maren_summary_{ts_str}.md"
    filepath = Path(output_dir) / filename

    md = _build_markdown(
        entries=entries,
        run_id=run_id,
        dry_run=dry_run,
        org_id=org_id,
        started_at=started_at,
        finished_at=finished_at,
    )

    filepath.write_text(md, encoding="utf-8")

    logger.info(
        "Run summary written",
        extra={"path": str(filepath), "entries": len(entries)},
    )

    return str(filepath)


# --------------------------------------------------------------------------- #
# Markdown builder
# --------------------------------------------------------------------------- #

def _build_markdown(
    entries: list[dict[str, Any]],
    run_id: str,
    dry_run: bool,
    org_id: str,
    started_at: datetime,
    finished_at: datetime,
) -> str:
    """Compose the full Markdown document as a string.

    Parameters
    ----------
    entries : list[dict[str, Any]]
        Audit log entries for this run.
    run_id : str
        Run UUID.
    dry_run : bool
        Run mode flag.
    org_id : str
        Org ID for display.
    started_at : datetime
        Run start time.
    finished_at : datetime
        Run finish time.

    Returns
    -------
    str
        Complete Markdown document.
    """
    mode_label = "🔵 DRY RUN" if dry_run else "🟢 LIVE"
    duration   = finished_at - started_at
    dur_str    = _format_duration(duration.total_seconds())

    sections: list[str] = []

    # ------------------------------------------------------------------ #
    # Header
    # ------------------------------------------------------------------ #
    sections.append(f"# MAREN Run Summary\n")
    sections.append(
        f"| Field        | Value |\n"
        f"|--------------|-------|\n"
        f"| **Run ID**   | `{run_id}` |\n"
        f"| **Mode**     | {mode_label} |\n"
        f"| **Org ID**   | `{org_id}` |\n"
        f"| **Started**  | `{started_at.strftime('%Y-%m-%dT%H:%M:%SZ')}` |\n"
        f"| **Finished** | `{finished_at.strftime('%Y-%m-%dT%H:%M:%SZ')}` |\n"
        f"| **Duration** | {dur_str} |\n"
        f"| **Actions**  | {len(entries)} |\n"
    )

    if not entries:
        sections.append("\n> No Marvis Actions found in this run.\n")
        return "\n".join(sections)

    # ------------------------------------------------------------------ #
    # Result counts
    # ------------------------------------------------------------------ #
    counts = _count_results(entries)
    sections.append("\n## Result Summary\n")
    sections.append(
        f"| Outcome     | Count |\n"
        f"|-------------|-------|\n"
        f"| ✅ Success  | {counts.get('success', 0)} |\n"
        f"| 🔵 Dry Run  | {counts.get('dry_run', 0)} |\n"
        f"| ⏭️ Skipped  | {counts.get('skipped', 0)} |\n"
        f"| ❌ Failed   | {counts.get('failed', 0)} |\n"
    )

    # ------------------------------------------------------------------ #
    # Issues table
    # ------------------------------------------------------------------ #
    sections.append("\n## Issues Found\n")
    sections.append(
        "| Site | Issue Type | Severity | Score | Tier | Action Taken | Result |\n"
        "|------|------------|----------|-------|------|--------------|--------|\n"
    )
    for e in sorted(entries, key=lambda x: x.get("priority_score", 0), reverse=True):
        result_icon = _result_icon(e.get("action_result", ""))
        sections.append(
            f"| {e.get('site_name', 'unknown')} "
            f"| {e.get('issue_type', 'unknown')} "
            f"| {e.get('severity', 'unknown')} "
            f"| {e.get('priority_score', 0):.2f} "
            f"| {e.get('action_tier', '—')} "
            f"| `{e.get('action_taken', 'unknown')}` "
            f"| {result_icon} {e.get('action_result', 'unknown')} |\n"
        )

    # ------------------------------------------------------------------ #
    # Tier breakdown
    # ------------------------------------------------------------------ #
    tier_counts = _count_tiers(entries)
    sections.append("\n## Tier Breakdown\n")
    sections.append(
        f"| Tier | Description | Count |\n"
        f"|------|-------------|-------|\n"
        f"| **1** | Clear client / Marvis RCA (always allowed) | {tier_counts[1]} |\n"
        f"| **2** | Bounce port / Push AP config / WLAN cycle | {tier_counts[2]} |\n"
        f"| **3** | Device restart / Bulk config push | {tier_counts[3]} |\n"
        f"| **—** | Below threshold / Not permitted | {tier_counts[0]} |\n"
    )

    # ------------------------------------------------------------------ #
    # Per-site breakdown
    # ------------------------------------------------------------------ #
    site_map = _group_by_site(entries)
    if len(site_map) > 1:
        sections.append("\n## Per-Site Breakdown\n")
        sections.append(
            "| Site | Actions | Avg Score | Top Issue |\n"
            "|------|---------|-----------|----------|\n"
        )
        for site_name, site_entries in sorted(site_map.items()):
            avg_score  = sum(e.get("priority_score", 0) for e in site_entries) / len(site_entries)
            top        = max(site_entries, key=lambda x: x.get("priority_score", 0))
            top_issue  = top.get("issue_type", "unknown")
            sections.append(
                f"| {site_name} | {len(site_entries)} | {avg_score:.2f} | {top_issue} |\n"
            )

    # ------------------------------------------------------------------ #
    # Failures
    # ------------------------------------------------------------------ #
    failures = [e for e in entries if e.get("action_result") == "failed"]
    if failures:
        sections.append("\n## ❌ Failures\n")
        for e in failures:
            sections.append(
                f"### {e.get('issue_type', 'unknown')} — {e.get('site_name', 'unknown')}\n\n"
                f"- **Action:** `{e.get('action_taken', 'unknown')}`\n"
                f"- **Target:** `{e.get('action_target', 'unknown')}`\n"
                f"- **Error:** {e.get('error', 'no error detail')}\n"
                f"- **Endpoint:** `{e.get('api_endpoint', 'n/a')}`\n\n"
            )

    # ------------------------------------------------------------------ #
    # Skipped actions
    # ------------------------------------------------------------------ #
    skipped = [e for e in entries if e.get("action_result") == "skipped"]
    if skipped:
        sections.append("\n## ⏭️ Skipped Actions\n")

        # Group by skip reason category for readability.
        below_threshold = [e for e in skipped
                           if "threshold" in (e.get("remediation_reasoning") or "").lower()
                           or "threshold" in (e.get("error") or "").lower()]
        tier_blocked    = [e for e in skipped if e not in below_threshold]

        if below_threshold:
            sections.append(
                f"\n### Below Score Threshold ({len(below_threshold)} actions)\n\n"
                "| Site | Issue Type | Score | Threshold |\n"
                "|------|------------|-------|-----------|\n"
            )
            for e in below_threshold:
                reasoning = e.get("remediation_reasoning", "")
                # Extract threshold value from reasoning string if present.
                threshold_str = "—"
                if "threshold" in reasoning.lower():
                    import re
                    m = re.search(r"threshold of ([\d.]+)", reasoning)
                    if m:
                        threshold_str = m.group(1)
                sections.append(
                    f"| {e.get('site_name', '—')} "
                    f"| {e.get('issue_type', '—')} "
                    f"| {e.get('priority_score', 0):.2f} "
                    f"| {threshold_str} |\n"
                )

        if tier_blocked:
            sections.append(
                f"\n### Tier Blocked ({len(tier_blocked)} actions)\n\n"
                "| Site | Issue Type | Tier | Reason |\n"
                "|------|------------|------|--------|\n"
            )
            for e in tier_blocked:
                reason = (e.get("error") or e.get("remediation_reasoning") or "")[:80]
                sections.append(
                    f"| {e.get('site_name', '—')} "
                    f"| {e.get('issue_type', '—')} "
                    f"| {e.get('action_tier', '—')} "
                    f"| {reason} |\n"
                )

    # ------------------------------------------------------------------ #
    # Remediation detail (top 10 highest-scored actions)
    # ------------------------------------------------------------------ #
    top_actions = sorted(
        [e for e in entries if e.get("action_result") not in ("skipped",)],
        key=lambda x: x.get("priority_score", 0),
        reverse=True,
    )[:10]

    if top_actions:
        sections.append("\n## Remediation Detail (Top Actions)\n")
        for i, e in enumerate(top_actions, start=1):
            result_icon = _result_icon(e.get("action_result", ""))
            sections.append(
                f"\n### {i}. {e.get('issue_type', 'unknown')} — {e.get('site_name', 'unknown')}\n\n"
                f"| Field | Value |\n"
                f"|-------|-------|\n"
                f"| **Score** | {e.get('priority_score', 0):.2f} |\n"
                f"| **Severity** | {e.get('severity', '—')} |\n"
                f"| **Blast Radius** | {e.get('blast_radius', '—')} |\n"
                f"| **Recurrence** | {e.get('recurrence', '—')} |\n"
                f"| **Action** | `{e.get('action_taken', '—')}` |\n"
                f"| **Target** | `{e.get('action_target', '—')}` |\n"
                f"| **Endpoint** | `{e.get('api_endpoint', 'n/a')}` |\n"
                f"| **Result** | {result_icon} {e.get('action_result', '—')} |\n\n"
                f"> {e.get('remediation_reasoning', '')}\n"
            )

    # ------------------------------------------------------------------ #
    # Footer
    # ------------------------------------------------------------------ #
    sections.append(
        f"\n---\n"
        f"*Generated by MAREN (Marvis Autonomous Remediation Engine for Networks) "
        f"at `{finished_at.strftime('%Y-%m-%dT%H:%M:%SZ')}`*\n"
    )

    return "".join(sections)


# --------------------------------------------------------------------------- #
# Private helpers
# --------------------------------------------------------------------------- #

def _count_results(entries: list[dict[str, Any]]) -> dict[str, int]:
    """Count entries by action_result value.

    Parameters
    ----------
    entries : list[dict[str, Any]]
        Audit log entries.

    Returns
    -------
    dict[str, int]
        Counts keyed by result string.
    """
    counts: dict[str, int] = defaultdict(int)
    for e in entries:
        counts[e.get("action_result", "unknown")] += 1
    return dict(counts)


def _count_tiers(entries: list[dict[str, Any]]) -> dict[int, int]:
    """Count entries by action_tier.

    Parameters
    ----------
    entries : list[dict[str, Any]]
        Audit log entries.

    Returns
    -------
    dict[int, int]
        Counts keyed by tier number (0 = skipped/below-threshold).
    """
    counts: dict[int, int] = defaultdict(int)
    for e in entries:
        tier = e.get("action_tier", 0)
        counts[int(tier)] += 1
    # Ensure all tiers present.
    for t in (0, 1, 2, 3):
        counts.setdefault(t, 0)
    return dict(counts)


def _group_by_site(
    entries: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    """Group audit entries by site_name.

    Parameters
    ----------
    entries : list[dict[str, Any]]
        Audit log entries.

    Returns
    -------
    dict[str, list[dict[str, Any]]]
        Entries grouped by site name.
    """
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for e in entries:
        groups[e.get("site_name", "unknown")].append(e)
    return dict(groups)


def _result_icon(result: str) -> str:
    """Map an action_result string to a Markdown emoji icon.

    Parameters
    ----------
    result : str
        One of ``"success"``, ``"dry_run"``, ``"skipped"``, ``"failed"``.

    Returns
    -------
    str
        Emoji string.
    """
    return {
        "success": "✅",
        "dry_run": "🔵",
        "skipped": "⏭️",
        "failed":  "❌",
    }.get(result, "❓")


def _format_duration(total_seconds: float) -> str:
    """Format a duration in seconds to a human-readable string.

    Parameters
    ----------
    total_seconds : float
        Duration in seconds.

    Returns
    -------
    str
        e.g. ``"2m 14s"`` or ``"48s"``.
    """
    total_seconds = max(0, total_seconds)
    minutes = int(total_seconds // 60)
    seconds = int(total_seconds % 60)
    if minutes:
        return f"{minutes}m {seconds}s"
    return f"{seconds}s"