# Python 3.11+
# core/scorer.py — Priority scoring engine.
#
# Implements the scoring model from the spec:
#
#   priority_score = severity_weight × blast_radius_factor × recurrence_factor
#
# severity_weight:
#   critical = 4, high = 3, medium = 2, low = 1
#
# blast_radius_factor:
#   > 50 affected = 3.0, 10–50 = 2.0, 1–9 = 1.0
#
# recurrence_factor:
#   > 3 occurrences in last 24h = 2.0, 2–3 = 1.5, first occurrence = 1.0
#
# Scores are attached to the action dict under the keys:
#   priority_score, severity_weight, blast_radius_factor, recurrence_factor,
#   blast_radius, recurrence_count, severity (normalised string)
#
# Actions below the configured min_score_threshold are flagged as skipped.

from __future__ import annotations

from typing import Any

from utils.logger import get_logger

logger = get_logger(__name__)


# --------------------------------------------------------------------------- #
# Weight / factor tables
# --------------------------------------------------------------------------- #

SEVERITY_WEIGHTS: dict[str, float] = {
    "critical": 4.0,
    "high":     3.0,
    "medium":   2.0,
    "low":      1.0,
}

# Default weight for any severity string not in the table.
DEFAULT_SEVERITY_WEIGHT: float = 1.0


def _blast_radius_factor(blast_radius: int) -> float:
    """Map a raw blast radius count to the spec-defined factor.

    Parameters
    ----------
    blast_radius : int
        Number of affected clients or devices (minimum 1).

    Returns
    -------
    float
        3.0 if > 50, 2.0 if 10–50, 1.0 if 1–9.
    """
    if blast_radius > 50:
        return 3.0
    if blast_radius >= 10:
        return 2.0
    return 1.0


def _recurrence_factor(recurrence_count: int) -> float:
    """Map a raw recurrence count to the spec-defined factor.

    Parameters
    ----------
    recurrence_count : int
        Number of times this issue type was seen in the last 24 hours.

    Returns
    -------
    float
        2.0 if > 3, 1.5 if 2–3, 1.0 if first occurrence.
    """
    if recurrence_count > 3:
        return 2.0
    if recurrence_count >= 2:
        return 1.5
    return 1.0


# --------------------------------------------------------------------------- #
# Severity normalisation
# --------------------------------------------------------------------------- #

def normalise_severity(raw: str) -> str:
    """Normalise a Mist severity string to one of: critical/high/medium/low.

    Mist action severity values are not always consistent across firmware
    versions.  This function maps known variants to canonical values and
    falls back to 'low' for unrecognised strings.

    Parameters
    ----------
    raw : str
        Raw severity string from the Marvis Action object.

    Returns
    -------
    str
        One of ``"critical"``, ``"high"``, ``"medium"``, ``"low"``.
    """
    _ALIASES: dict[str, str] = {
        # canonical
        "critical": "critical",
        "high":     "high",
        "medium":   "medium",
        "low":      "low",
        # common Mist variants
        "crit":     "critical",
        "warn":     "medium",
        "warning":  "medium",
        "info":     "low",
        "informational": "low",
        "minor":    "low",
        "major":    "high",
        "error":    "high",
    }
    normalised = _ALIASES.get(raw.lower().strip(), "low")
    if normalised == "low" and raw.lower().strip() not in _ALIASES:
        logger.debug(
            "Unrecognised severity value — defaulting to 'low'",
            extra={"raw_severity": raw},
        )
    return normalised


# --------------------------------------------------------------------------- #
# Core scoring function
# --------------------------------------------------------------------------- #

def score_action(
    action: dict[str, Any],
    min_score_threshold: float = 2.0,
) -> dict[str, Any]:
    """Compute priority score for a single Marvis Action and attach results.

    Reads ``severity`` from the action and ``blast_radius`` /
    ``recurrence_count`` from ``action["correlated_telemetry"]`` (populated
    by ``correlator.correlate()``).  If telemetry is absent, conservative
    defaults are used (blast_radius=1, recurrence_count=0).

    Attaches the following keys to the action dict in-place:

    - ``severity``            — normalised severity string
    - ``severity_weight``     — float weight from SEVERITY_WEIGHTS
    - ``blast_radius``        — int (from telemetry or action field)
    - ``blast_radius_factor`` — float
    - ``recurrence_count``    — int
    - ``recurrence_factor``   — float
    - ``priority_score``      — float (product of the three factors)
    - ``below_threshold``     — bool (True if score < min_score_threshold)

    Parameters
    ----------
    action : dict[str, Any]
        Marvis Action object, optionally enriched with correlated_telemetry.
    min_score_threshold : float
        Minimum score required for the action to proceed to decision/execution.
        Defaults to 2.0 (spec value).

    Returns
    -------
    dict[str, Any]
        The same action dict with scoring keys attached.
    """
    # --- Severity ---
    raw_severity = str(
        action.get("severity")
        or action.get("priority")
        or "low"
    )
    severity = normalise_severity(raw_severity)
    sev_weight = SEVERITY_WEIGHTS.get(severity, DEFAULT_SEVERITY_WEIGHT)

    # --- Blast radius ---
    telemetry: dict[str, Any] = action.get("correlated_telemetry") or {}
    blast_radius: int = int(
        telemetry.get("blast_radius")
        or action.get("blast_radius")
        or action.get("affected_count")
        or 1
    )
    blast_radius = max(1, blast_radius)  # floor at 1
    br_factor = _blast_radius_factor(blast_radius)

    # --- Recurrence ---
    recurrence_count: int = int(
        telemetry.get("recurrence_count")
        or action.get("recurrence_count")
        or 0
    )
    rec_factor = _recurrence_factor(recurrence_count)

    # --- Final score ---
    priority_score = round(sev_weight * br_factor * rec_factor, 2)
    below_threshold = priority_score < min_score_threshold

    # Attach all scoring fields to the action.
    action.update(
        {
            "severity":            severity,
            "severity_weight":     sev_weight,
            "blast_radius":        blast_radius,
            "blast_radius_factor": br_factor,
            "recurrence_count":    recurrence_count,
            "recurrence_factor":   rec_factor,
            "priority_score":      priority_score,
            "below_threshold":     below_threshold,
        }
    )

    logger.debug(
        "Action scored",
        extra={
            "action_id":       action.get("id", "unknown"),
            "severity":        severity,
            "blast_radius":    blast_radius,
            "recurrence":      recurrence_count,
            "priority_score":  priority_score,
            "below_threshold": below_threshold,
        },
    )

    return action


# --------------------------------------------------------------------------- #
# Batch scoring
# --------------------------------------------------------------------------- #

def score_all(
    actions: list[dict[str, Any]],
    min_score_threshold: float = 2.0,
) -> list[dict[str, Any]]:
    """Score all actions and return them sorted by priority_score descending.

    Parameters
    ----------
    actions : list[dict[str, Any]]
        Marvis Action objects, each with ``correlated_telemetry`` attached.
    min_score_threshold : float
        Passed through to :func:`score_action`.

    Returns
    -------
    list[dict[str, Any]]
        Actions sorted highest-score-first with scoring keys attached.
        Below-threshold actions are included (flagged with
        ``below_threshold=True``) so the audit log can record why they
        were skipped.
    """
    logger.info(
        "Scoring %d actions (threshold=%.1f)",
        len(actions),
        min_score_threshold,
    )

    for action in actions:
        score_action(action, min_score_threshold=min_score_threshold)

    scored = sorted(actions, key=lambda a: a.get("priority_score", 0), reverse=True)

    above = sum(1 for a in scored if not a.get("below_threshold"))
    below = len(scored) - above

    logger.info(
        "Scoring complete",
        extra={
            "total":          len(scored),
            "above_threshold": above,
            "below_threshold": below,
        },
    )

    return scored


# --------------------------------------------------------------------------- #
# Human-readable score explanation
# --------------------------------------------------------------------------- #

def explain_score(action: dict[str, Any]) -> str:
    """Return a human-readable explanation of an action's priority score.

    Used to populate the ``remediation_reasoning`` field in the audit log.

    Parameters
    ----------
    action : dict[str, Any]
        A scored action dict (must have scoring keys attached by
        :func:`score_action`).

    Returns
    -------
    str
        Multi-sentence explanation of the score components and what action
        tier was selected.
    """
    sev       = action.get("severity", "unknown")
    sev_w     = action.get("severity_weight", 0)
    br        = action.get("blast_radius", 0)
    br_f      = action.get("blast_radius_factor", 0)
    rec       = action.get("recurrence_count", 0)
    rec_f     = action.get("recurrence_factor", 0)
    score     = action.get("priority_score", 0)
    threshold = action.get("min_score_threshold", 2.0)
    issue     = action.get("category") or action.get("issue_type") or "unknown issue"
    site      = action.get("site_name", "unknown site")

    rec_desc = (
        "first occurrence" if rec == 0
        else f"seen {rec} time(s) in the last 24 hours"
    )

    explanation = (
        f"Issue type '{issue}' at site '{site}' scored {score:.2f}. "
        f"Severity is {sev} (weight {sev_w:.1f}), affecting an estimated "
        f"{br} endpoint(s) (blast-radius factor {br_f:.1f}), "
        f"{rec_desc} (recurrence factor {rec_f:.1f}). "
        f"Score = {sev_w:.1f} × {br_f:.1f} × {rec_f:.1f} = {score:.2f}."
    )

    if action.get("below_threshold"):
        explanation += (
            f" Score is below the configured threshold ({threshold}); "
            "action skipped."
        )

    return explanation