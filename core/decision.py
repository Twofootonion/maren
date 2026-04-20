# Python 3.11+
# core/decision.py — Remediation tier assignment and action selection.
#
# For each scored Marvis Action, this module decides:
#   1. Which remediation tier applies (1, 2, or 3)
#   2. Which specific action to take within that tier
#   3. Whether the action is permitted given current config flags
#   4. A human-readable rationale for the audit log
#
# Decision rules are data-driven via the ACTION_MATRIX dict so new issue
# types can be added without changing control flow.
#
# Output per action:
#   action_tier        — int (1, 2, 3)
#   action_type        — str (e.g. "clear_client_session")
#   action_target      — str (device_id, client_id, or site_id)
#   action_permitted   — bool (False if tier config flags block it)
#   skip_reason        — str | None (populated when not permitted)
#   remediation_reasoning — str (full human-readable explanation)

from __future__ import annotations

from typing import Any

from utils.logger import get_logger

logger = get_logger(__name__)


# --------------------------------------------------------------------------- #
# Action matrix
# --------------------------------------------------------------------------- #
# Maps Marvis Action category/issue_type → preferred remediation action per tier.
# Each entry specifies the lowest tier that should handle it plus a fallback.
#
# Structure:
#   category_key: {
#       "tier":        int,          # preferred tier
#       "action_type": str,          # action identifier used by executor.py
#       "target_field": str,         # field in the action dict holding the target ID
#       "fallback_target": str,      # field to use if target_field is absent
#   }
#
# Categories are matched case-insensitively against action["category"] and
# action["issue_type"].  "default" is the catch-all.

ACTION_MATRIX: dict[str, dict[str, Any]] = {
    # ------------------------------------------------------------------ #
    # Tier 1 — always allowed in live mode
    # ------------------------------------------------------------------ #
    "bad_wired_uplink": {
        "tier":           1,
        "action_type":    "marvis_rca_query",
        "target_field":   "device_id",
        "fallback_target": "site_id",
    },
    "dns_failure": {
        "tier":           1,
        "action_type":    "marvis_rca_query",
        "target_field":   "site_id",
        "fallback_target": "site_id",
    },
    "dhcp_failure": {
        "tier":           1,
        "action_type":    "marvis_rca_query",
        "target_field":   "site_id",
        "fallback_target": "site_id",
    },
    "missing_vlan": {
        "tier":           1,
        "action_type":    "marvis_rca_query",
        "target_field":   "site_id",
        "fallback_target": "site_id",
    },
    "auth_failure": {
        "tier":           1,
        "action_type":    "clear_client_session",
        "target_field":   "client_id",
        "fallback_target": "site_id",
    },
    "roaming_failure": {
        "tier":           1,
        "action_type":    "clear_client_session",
        "target_field":   "client_id",
        "fallback_target": "site_id",
    },
    "client_connectivity": {
        "tier":           1,
        "action_type":    "clear_client_session",
        "target_field":   "client_id",
        "fallback_target": "site_id",
    },
    # ------------------------------------------------------------------ #
    # Tier 2 — requires ENABLE_TIER2=true
    # ------------------------------------------------------------------ #
    "wifi_interference": {
        "tier":           2,
        "action_type":    "push_ap_config",
        "target_field":   "ap_id",
        "fallback_target": "device_id",
    },
    "wifi": {
        "tier":           2,
        "action_type":    "push_ap_config",
        "target_field":   "ap_id",
        "fallback_target": "device_id",
    },
    "channel_utilization": {
        "tier":           2,
        "action_type":    "push_ap_config",
        "target_field":   "ap_id",
        "fallback_target": "device_id",
    },
    "ap_offline": {
        "tier":           2,
        "action_type":    "bounce_port",
        "target_field":   "device_id",
        "fallback_target": "site_id",
    },
    "switch_port": {
        "tier":           2,
        "action_type":    "bounce_port",
        "target_field":   "port_id",
        "fallback_target": "device_id",
    },
    "wlan": {
        "tier":           2,
        "action_type":    "disable_reenable_wlan",
        "target_field":   "wlan_id",
        "fallback_target": "site_id",
    },
    # ------------------------------------------------------------------ #
    # Tier 3 — requires ENABLE_TIER3=true AND TIER3_CONFIRM=true
    # ------------------------------------------------------------------ #
    "device_restart": {
        "tier":           3,
        "action_type":    "restart_device",
        "target_field":   "device_id",
        "fallback_target": "site_id",
    },
    "firmware": {
        "tier":           3,
        "action_type":    "bulk_config_push",
        "target_field":   "site_id",
        "fallback_target": "site_id",
    },
    "site_down": {
        "tier":           3,
        "action_type":    "bulk_config_push",
        "target_field":   "site_id",
        "fallback_target": "site_id",
    },
    # ------------------------------------------------------------------ #
    # Catch-all
    # ------------------------------------------------------------------ #
    "default": {
        "tier":           1,
        "action_type":    "marvis_rca_query",
        "target_field":   "site_id",
        "fallback_target": "site_id",
    },
}

# Human-readable descriptions for each action type (used in reasoning text).
ACTION_DESCRIPTIONS: dict[str, str] = {
    "clear_client_session":    "Disconnect and re-authenticate the affected client",
    "marvis_rca_query":        "Trigger a Marvis RCA query and capture the AI analysis",
    "bounce_port":             "Disable then re-enable the affected switch port",
    "push_ap_config":          "Push a channel/power config update to the affected AP",
    "disable_reenable_wlan":   "Disable then re-enable the affected WLAN",
    "restart_device":          "Restart the affected device",
    "bulk_config_push":        "Push a bulk config update across the affected site",
}


# --------------------------------------------------------------------------- #
# Config dataclass (passed in from main.py / config.yaml)
# --------------------------------------------------------------------------- #

class DecisionConfig:
    """Holds the tier-enable flags read from config.yaml.

    Parameters
    ----------
    dry_run : bool
        If True, no live actions are taken regardless of tier.
    enable_tier2 : bool
        If False, Tier 2 actions are blocked.
    enable_tier3 : bool
        If False, Tier 3 actions are blocked.
    tier3_confirm : bool
        If False, Tier 3 actions are blocked even when enable_tier3=True.
    min_score_threshold : float
        Actions with priority_score below this are skipped.
    """

    def __init__(
        self,
        dry_run: bool = True,
        enable_tier2: bool = False,
        enable_tier3: bool = False,
        tier3_confirm: bool = False,
        min_score_threshold: float = 2.0,
    ) -> None:
        self.dry_run = dry_run
        self.enable_tier2 = enable_tier2
        self.enable_tier3 = enable_tier3
        self.tier3_confirm = tier3_confirm
        self.min_score_threshold = min_score_threshold

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"DecisionConfig(dry_run={self.dry_run}, "
            f"enable_tier2={self.enable_tier2}, "
            f"enable_tier3={self.enable_tier3}, "
            f"tier3_confirm={self.tier3_confirm}, "
            f"min_score_threshold={self.min_score_threshold})"
        )


# --------------------------------------------------------------------------- #
# Core decision function
# --------------------------------------------------------------------------- #

def decide(
    action: dict[str, Any],
    config: DecisionConfig,
) -> dict[str, Any]:
    """Assign a remediation tier and action type to a scored Marvis Action.

    Reads the action's category/issue_type, looks up the ACTION_MATRIX,
    verifies tier permissions against config flags, and attaches decision
    fields to the action dict in-place.

    Parameters
    ----------
    action : dict[str, Any]
        A scored Marvis Action (must have ``priority_score`` and
        ``below_threshold`` from scorer.py).
    config : DecisionConfig
        Current runtime configuration flags.

    Returns
    -------
    dict[str, Any]
        The action dict with the following keys added:

        - ``action_tier``          — int
        - ``action_type``          — str
        - ``action_target``        — str
        - ``action_permitted``     — bool
        - ``skip_reason``          — str | None
        - ``remediation_reasoning``— str
    """
    action_id = action.get("id", "unknown")

    # --- Skip below-threshold actions immediately ---
    if action.get("below_threshold"):
        _attach_skip(
            action,
            tier=0,
            action_type="none",
            target="none",
            reason=(
                f"Priority score {action.get('priority_score', 0):.2f} is below "
                f"the configured threshold of {config.min_score_threshold:.2f}."
            ),
        )
        logger.debug(
            "Action skipped — below threshold",
            extra={"action_id": action_id,
                   "priority_score": action.get("priority_score")},
        )
        return action

    # --- Look up category in action matrix ---
    category = (
        action.get("category")
        or action.get("issue_type")
        or "default"
    ).lower()

    matrix_entry = ACTION_MATRIX.get(category)

    # Try partial match if exact match not found (e.g. "wifi_channel" → "wifi")
    if matrix_entry is None:
        for key in ACTION_MATRIX:
            if key != "default" and key in category:
                matrix_entry = ACTION_MATRIX[key]
                logger.debug(
                    "Action category matched via partial key",
                    extra={"category": category, "matched_key": key},
                )
                break

    if matrix_entry is None:
        matrix_entry = ACTION_MATRIX["default"]
        logger.debug(
            "Action category not in matrix — using default",
            extra={"category": category},
        )

    tier: int = matrix_entry["tier"]
    action_type: str = matrix_entry["action_type"]
    target_field: str = matrix_entry["target_field"]
    fallback_field: str = matrix_entry["fallback_target"]

    # --- Resolve target ID ---
    target = (
        action.get(target_field)
        or action.get(fallback_field)
        or action.get("site_id", "unknown")
    )

    # Also check inside details dict.
    if target in (None, "unknown", ""):
        details = action.get("details") or {}
        if isinstance(details, dict):
            target = (
                details.get(target_field)
                or details.get(fallback_field)
                or action.get("site_id", "unknown")
            )

    # --- Check tier permissions ---
    permitted, skip_reason = _check_permissions(tier, config)

    # --- Build reasoning string ---
    reasoning = _build_reasoning(action, tier, action_type, target, config, skip_reason)

    # --- Attach decision fields ---
    action.update(
        {
            "action_tier":           tier,
            "action_type":           action_type,
            "action_target":         str(target),
            "action_permitted":      permitted,
            "skip_reason":           skip_reason,
            "remediation_reasoning": reasoning,
        }
    )

    logger.info(
        "Decision made",
        extra={
            "action_id":       action_id,
            "category":        category,
            "tier":            tier,
            "action_type":     action_type,
            "target":          target,
            "permitted":       permitted,
            "skip_reason":     skip_reason,
            "priority_score":  action.get("priority_score"),
        },
    )

    return action


def decide_all(
    actions: list[dict[str, Any]],
    config: DecisionConfig,
) -> list[dict[str, Any]]:
    """Apply :func:`decide` to every action in the list.

    Parameters
    ----------
    actions : list[dict[str, Any]]
        Scored Marvis Actions (output of scorer.score_all).
    config : DecisionConfig
        Runtime configuration flags.

    Returns
    -------
    list[dict[str, Any]]
        Actions with decision fields attached to each.
    """
    logger.info(
        "Running decision engine on %d actions", len(actions),
        extra={"dry_run": config.dry_run},
    )

    for action in actions:
        decide(action, config)

    permitted_count = sum(1 for a in actions if a.get("action_permitted"))
    skipped_count   = len(actions) - permitted_count

    logger.info(
        "Decision engine complete",
        extra={
            "total":     len(actions),
            "permitted": permitted_count,
            "skipped":   skipped_count,
        },
    )

    return actions


# --------------------------------------------------------------------------- #
# Private helpers
# --------------------------------------------------------------------------- #

def _check_permissions(
    tier: int,
    config: DecisionConfig,
) -> tuple[bool, str | None]:
    """Check whether a tier action is permitted under current config flags.

    Parameters
    ----------
    tier : int
        Remediation tier (1, 2, or 3).
    config : DecisionConfig
        Current configuration.

    Returns
    -------
    tuple[bool, str | None]
        ``(permitted, skip_reason)`` — skip_reason is None when permitted.
    """
    if tier == 1:
        # Tier 1 is always allowed in both dry-run and live mode.
        return True, None

    if tier == 2:
        if not config.enable_tier2:
            return False, (
                "Tier 2 action blocked: ENABLE_TIER2 is false in config. "
                "Set enable_tier2: true in config.yaml to enable."
            )
        return True, None

    if tier == 3:
        if not config.enable_tier3:
            return False, (
                "Tier 3 action blocked: ENABLE_TIER3 is false in config. "
                "Set enable_tier3: true in config.yaml to enable."
            )
        if not config.tier3_confirm:
            return False, (
                "Tier 3 action blocked: TIER3_CONFIRM is false. "
                "Set tier3_confirm: true in config.yaml to confirm intent."
            )
        return True, None

    # Unknown tier — block by default.
    return False, f"Unknown tier {tier} — action blocked by default."


def _attach_skip(
    action: dict[str, Any],
    tier: int,
    action_type: str,
    target: str,
    reason: str,
) -> None:
    """Attach skip fields to an action dict in-place.

    Parameters
    ----------
    action : dict[str, Any]
        Action to modify.
    tier : int
        Tier that would have been used.
    action_type : str
        Action type label.
    target : str
        Target identifier.
    reason : str
        Human-readable skip reason.
    """
    action.update(
        {
            "action_tier":           tier,
            "action_type":           action_type,
            "action_target":         target,
            "action_permitted":      False,
            "skip_reason":           reason,
            "remediation_reasoning": reason,
        }
    )


def _build_reasoning(
    action: dict[str, Any],
    tier: int,
    action_type: str,
    target: str,
    config: DecisionConfig,
    skip_reason: str | None,
) -> str:
    """Compose the human-readable remediation_reasoning string.

    Parameters
    ----------
    action : dict[str, Any]
        Scored action dict.
    tier : int
        Assigned tier.
    action_type : str
        Selected action type.
    target : str
        Target ID.
    config : DecisionConfig
        Runtime flags.
    skip_reason : str | None
        Populated when action is not permitted.

    Returns
    -------
    str
        Multi-sentence reasoning explanation for the audit log.
    """
    score    = action.get("priority_score", 0)
    severity = action.get("severity", "unknown")
    br       = action.get("blast_radius", 0)
    rec      = action.get("recurrence_count", 0)
    category = action.get("category") or action.get("issue_type") or "unknown"
    site     = action.get("site_name", "unknown")
    desc     = ACTION_DESCRIPTIONS.get(action_type, action_type)
    mode     = "DRY RUN" if config.dry_run else "LIVE"

    reasoning = (
        f"[{mode}] Issue '{category}' at '{site}' assigned to Tier {tier}. "
        f"Priority score: {score:.2f} (severity={severity}, "
        f"blast_radius={br}, recurrence={rec}). "
        f"Selected action: {action_type} — {desc}. "
        f"Target: {target}."
    )

    if skip_reason:
        reasoning += f" NOT EXECUTED: {skip_reason}"
    elif config.dry_run:
        reasoning += (
            " Action would be executed in LIVE mode "
            "(DRY_RUN=true — no changes made)."
        )
    else:
        reasoning += " Action will be executed."

    return reasoning