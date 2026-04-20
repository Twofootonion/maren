# Python 3.11+
# tests/test_scorer.py — Unit tests for core/scorer.py.
#
# Covers the full scoring model from the spec:
#   priority_score = severity_weight × blast_radius_factor × recurrence_factor
#
# No network calls — scorer is pure computation.
# Compatible with both pytest and unittest (python -m unittest).

import sys
import os
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.scorer import (
    score_action,
    score_all,
    explain_score,
    normalise_severity,
    _blast_radius_factor,
    _recurrence_factor,
    SEVERITY_WEIGHTS,
    DEFAULT_SEVERITY_WEIGHT,
)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _action(
    severity: str = "high",
    blast_radius: int = 10,
    recurrence_count: int = 0,
    **kwargs,
) -> dict:
    """Build a minimal action dict with correlated_telemetry pre-filled.

    Parameters
    ----------
    severity : str
        Raw severity string.
    blast_radius : int
        Blast radius to embed in correlated_telemetry.
    recurrence_count : int
        Recurrence count to embed in correlated_telemetry.
    **kwargs
        Additional keys merged into the action dict.

    Returns
    -------
    dict
        Minimal action dict ready for score_action().
    """
    a = {
        "id":       "act-test",
        "site_id":  "site-aaa",
        "site_name": "HQ",
        "category": "wifi",
        "severity": severity,
        "correlated_telemetry": {
            "blast_radius":     blast_radius,
            "recurrence_count": recurrence_count,
        },
    }
    a.update(kwargs)
    return a


# --------------------------------------------------------------------------- #
# _blast_radius_factor
# --------------------------------------------------------------------------- #

class TestBlastRadiusFactor(unittest.TestCase):
    """Unit tests for the _blast_radius_factor helper."""

    def test_single_affected(self):
        """Blast radius of 1 yields factor 1.0."""
        self.assertEqual(_blast_radius_factor(1), 1.0)

    def test_nine_affected(self):
        """Blast radius of 9 (upper boundary of 1–9 band) yields 1.0."""
        self.assertEqual(_blast_radius_factor(9), 1.0)

    def test_ten_affected(self):
        """Blast radius of 10 (lower boundary of 10–50 band) yields 2.0."""
        self.assertEqual(_blast_radius_factor(10), 2.0)

    def test_fifty_affected(self):
        """Blast radius of 50 (upper boundary of 10–50 band) yields 2.0."""
        self.assertEqual(_blast_radius_factor(50), 2.0)

    def test_fifty_one_affected(self):
        """Blast radius of 51 (lower boundary of >50 band) yields 3.0."""
        self.assertEqual(_blast_radius_factor(51), 3.0)

    def test_large_blast_radius(self):
        """Very large blast radius yields 3.0."""
        self.assertEqual(_blast_radius_factor(1000), 3.0)

    def test_factor_is_float(self):
        """Returned value is always a float."""
        for br in (1, 10, 51):
            self.assertIsInstance(_blast_radius_factor(br), float)


# --------------------------------------------------------------------------- #
# _recurrence_factor
# --------------------------------------------------------------------------- #

class TestRecurrenceFactor(unittest.TestCase):
    """Unit tests for the _recurrence_factor helper."""

    def test_zero_occurrences(self):
        """First occurrence (0 prior events) yields 1.0."""
        self.assertEqual(_recurrence_factor(0), 1.0)

    def test_one_occurrence(self):
        """One prior occurrence yields 1.0."""
        self.assertEqual(_recurrence_factor(1), 1.0)

    def test_two_occurrences(self):
        """Two occurrences (lower boundary of 2–3 band) yields 1.5."""
        self.assertEqual(_recurrence_factor(2), 1.5)

    def test_three_occurrences(self):
        """Three occurrences (upper boundary of 2–3 band) yields 1.5."""
        self.assertEqual(_recurrence_factor(3), 1.5)

    def test_four_occurrences(self):
        """Four occurrences (lower boundary of >3 band) yields 2.0."""
        self.assertEqual(_recurrence_factor(4), 2.0)

    def test_many_occurrences(self):
        """Many occurrences yields 2.0."""
        self.assertEqual(_recurrence_factor(100), 2.0)

    def test_factor_is_float(self):
        """Returned value is always a float."""
        for r in (0, 2, 4):
            self.assertIsInstance(_recurrence_factor(r), float)


# --------------------------------------------------------------------------- #
# normalise_severity
# --------------------------------------------------------------------------- #

class TestNormaliseSeverity(unittest.TestCase):
    """Unit tests for severity string normalisation."""

    def test_canonical_values_pass_through(self):
        """Canonical severity strings are returned unchanged."""
        for sev in ("critical", "high", "medium", "low"):
            self.assertEqual(normalise_severity(sev), sev)

    def test_uppercase_canonical(self):
        """Severity strings are case-insensitive."""
        self.assertEqual(normalise_severity("CRITICAL"), "critical")
        self.assertEqual(normalise_severity("HIGH"),     "high")
        self.assertEqual(normalise_severity("MEDIUM"),   "medium")
        self.assertEqual(normalise_severity("LOW"),      "low")

    def test_mixed_case(self):
        """Mixed-case values are normalised."""
        self.assertEqual(normalise_severity("Critical"), "critical")
        self.assertEqual(normalise_severity("High"),     "high")

    def test_alias_crit(self):
        """'crit' maps to 'critical'."""
        self.assertEqual(normalise_severity("crit"), "critical")

    def test_alias_warn(self):
        """'warn' maps to 'medium'."""
        self.assertEqual(normalise_severity("warn"), "medium")

    def test_alias_warning(self):
        """'warning' maps to 'medium'."""
        self.assertEqual(normalise_severity("warning"), "medium")

    def test_alias_info(self):
        """'info' maps to 'low'."""
        self.assertEqual(normalise_severity("info"), "low")

    def test_alias_informational(self):
        """'informational' maps to 'low'."""
        self.assertEqual(normalise_severity("informational"), "low")

    def test_alias_minor(self):
        """'minor' maps to 'low'."""
        self.assertEqual(normalise_severity("minor"), "low")

    def test_alias_major(self):
        """'major' maps to 'high'."""
        self.assertEqual(normalise_severity("major"), "high")

    def test_alias_error(self):
        """'error' maps to 'high'."""
        self.assertEqual(normalise_severity("error"), "high")

    def test_unknown_string_defaults_to_low(self):
        """Unrecognised severity strings fall back to 'low'."""
        self.assertEqual(normalise_severity("garbage"),       "low")
        self.assertEqual(normalise_severity("UNKNOWN_VALUE"), "low")
        self.assertEqual(normalise_severity(""),              "low")

    def test_whitespace_stripped(self):
        """Leading/trailing whitespace is stripped before matching."""
        self.assertEqual(normalise_severity("  high  "), "high")
        self.assertEqual(normalise_severity(" critical"), "critical")


# --------------------------------------------------------------------------- #
# score_action — spec matrix values
# --------------------------------------------------------------------------- #

class TestScoreActionSpecMatrix(unittest.TestCase):
    """Verify exact values from the spec scoring matrix."""

    def test_critical_large_blast_high_recurrence(self):
        """critical(4) × >50(3.0) × >3(2.0) = 24.0"""
        a = _action("critical", blast_radius=75, recurrence_count=5)
        score_action(a)
        self.assertEqual(a["priority_score"], 24.0)

    def test_critical_large_blast_medium_recurrence(self):
        """critical(4) × >50(3.0) × 2-3(1.5) = 18.0"""
        a = _action("critical", blast_radius=75, recurrence_count=2)
        score_action(a)
        self.assertEqual(a["priority_score"], 18.0)

    def test_critical_large_blast_first_occurrence(self):
        """critical(4) × >50(3.0) × first(1.0) = 12.0"""
        a = _action("critical", blast_radius=75, recurrence_count=0)
        score_action(a)
        self.assertEqual(a["priority_score"], 12.0)

    def test_high_medium_blast_medium_recurrence(self):
        """high(3) × 10-50(2.0) × 2-3(1.5) = 9.0"""
        a = _action("high", blast_radius=25, recurrence_count=3)
        score_action(a)
        self.assertEqual(a["priority_score"], 9.0)

    def test_high_small_blast_first_occurrence(self):
        """high(3) × 1-9(1.0) × first(1.0) = 3.0"""
        a = _action("high", blast_radius=5, recurrence_count=0)
        score_action(a)
        self.assertEqual(a["priority_score"], 3.0)

    def test_medium_small_blast_first_occurrence(self):
        """medium(2) × 1-9(1.0) × first(1.0) = 2.0"""
        a = _action("medium", blast_radius=5, recurrence_count=0)
        score_action(a)
        self.assertEqual(a["priority_score"], 2.0)

    def test_low_small_blast_first_occurrence(self):
        """low(1) × 1-9(1.0) × first(1.0) = 1.0"""
        a = _action("low", blast_radius=3, recurrence_count=0)
        score_action(a)
        self.assertEqual(a["priority_score"], 1.0)

    def test_low_large_blast_high_recurrence(self):
        """low(1) × >50(3.0) × >3(2.0) = 6.0"""
        a = _action("low", blast_radius=60, recurrence_count=10)
        score_action(a)
        self.assertEqual(a["priority_score"], 6.0)


# --------------------------------------------------------------------------- #
# score_action — field attachment
# --------------------------------------------------------------------------- #

class TestScoreActionFieldAttachment(unittest.TestCase):
    """Verify all scoring fields are attached to the action dict."""

    def setUp(self):
        self.action = _action("high", blast_radius=25, recurrence_count=3)
        score_action(self.action)

    def test_severity_normalised(self):
        """severity field contains normalised string."""
        self.assertEqual(self.action["severity"], "high")

    def test_severity_weight_attached(self):
        """severity_weight matches SEVERITY_WEIGHTS table."""
        self.assertEqual(self.action["severity_weight"], SEVERITY_WEIGHTS["high"])

    def test_blast_radius_attached(self):
        """blast_radius is taken from correlated_telemetry."""
        self.assertEqual(self.action["blast_radius"], 25)

    def test_blast_radius_factor_attached(self):
        """blast_radius_factor is correct for blast_radius=25."""
        self.assertEqual(self.action["blast_radius_factor"], 2.0)

    def test_recurrence_count_attached(self):
        """recurrence_count is taken from correlated_telemetry."""
        self.assertEqual(self.action["recurrence_count"], 3)

    def test_recurrence_factor_attached(self):
        """recurrence_factor is correct for recurrence_count=3."""
        self.assertEqual(self.action["recurrence_factor"], 1.5)

    def test_priority_score_attached(self):
        """priority_score is computed and rounded to 2dp."""
        self.assertEqual(self.action["priority_score"], 9.0)

    def test_below_threshold_attached(self):
        """below_threshold bool is attached."""
        self.assertIn("below_threshold", self.action)
        self.assertIsInstance(self.action["below_threshold"], bool)


# --------------------------------------------------------------------------- #
# score_action — threshold behaviour
# --------------------------------------------------------------------------- #

class TestScoreActionThreshold(unittest.TestCase):
    """Verify below_threshold flag is set correctly."""

    def test_score_above_threshold_not_flagged(self):
        """score > threshold → below_threshold=False."""
        a = _action("high", blast_radius=25, recurrence_count=3)  # score=9.0
        score_action(a, min_score_threshold=2.0)
        self.assertFalse(a["below_threshold"])

    def test_score_equal_to_threshold_not_flagged(self):
        """score == threshold → below_threshold=False (at threshold is allowed)."""
        a = _action("medium", blast_radius=5, recurrence_count=0)  # score=2.0
        score_action(a, min_score_threshold=2.0)
        self.assertFalse(a["below_threshold"])

    def test_score_below_threshold_flagged(self):
        """score < threshold → below_threshold=True."""
        a = _action("low", blast_radius=3, recurrence_count=0)  # score=1.0
        score_action(a, min_score_threshold=2.0)
        self.assertTrue(a["below_threshold"])

    def test_custom_threshold(self):
        """Custom threshold value is respected."""
        a = _action("high", blast_radius=25, recurrence_count=3)  # score=9.0
        score_action(a, min_score_threshold=10.0)
        self.assertTrue(a["below_threshold"])

    def test_zero_threshold_never_flagged(self):
        """Threshold of 0.0 means nothing is ever flagged below."""
        a = _action("low", blast_radius=1, recurrence_count=0)  # score=1.0
        score_action(a, min_score_threshold=0.0)
        self.assertFalse(a["below_threshold"])


# --------------------------------------------------------------------------- #
# score_action — missing / partial data
# --------------------------------------------------------------------------- #

class TestScoreActionFallbacks(unittest.TestCase):
    """Verify conservative fallbacks when telemetry or severity is absent."""

    def test_missing_telemetry_uses_defaults(self):
        """No correlated_telemetry → blast_radius=1, recurrence=0."""
        a = {"id": "act-x", "severity": "high"}
        score_action(a)
        self.assertEqual(a["blast_radius"],     1)
        self.assertEqual(a["recurrence_count"], 0)
        # high × 1.0 × 1.0 = 3.0
        self.assertEqual(a["priority_score"], 3.0)

    def test_none_telemetry_uses_defaults(self):
        """correlated_telemetry=None → same conservative defaults."""
        a = {"id": "act-x", "severity": "critical", "correlated_telemetry": None}
        score_action(a)
        self.assertEqual(a["blast_radius"], 1)
        self.assertEqual(a["priority_score"], 4.0)  # critical × 1.0 × 1.0

    def test_missing_severity_defaults_to_low(self):
        """No severity field → defaults to 'low'."""
        a = {"id": "act-x", "correlated_telemetry": {"blast_radius": 5, "recurrence_count": 0}}
        score_action(a)
        self.assertEqual(a["severity"], "low")
        self.assertEqual(a["priority_score"], 1.0)

    def test_blast_radius_floored_at_one(self):
        """blast_radius of 0 in telemetry is floored to 1."""
        a = _action("high", blast_radius=0, recurrence_count=0)
        score_action(a)
        self.assertEqual(a["blast_radius"], 1)

    def test_action_level_affected_count_used_when_no_telemetry(self):
        """affected_count on the action itself is used if no telemetry."""
        a = {"id": "act-x", "severity": "critical", "affected_count": 60}
        score_action(a)
        # critical(4) × >50(3.0) × first(1.0) = 12.0
        self.assertEqual(a["blast_radius"], 60)
        self.assertEqual(a["priority_score"], 12.0)

    def test_priority_field_used_as_severity_fallback(self):
        """'priority' key is used as severity fallback when 'severity' absent."""
        a = {"id": "act-x", "priority": "critical",
             "correlated_telemetry": {"blast_radius": 5, "recurrence_count": 0}}
        score_action(a)
        self.assertEqual(a["severity"], "critical")

    def test_unknown_severity_alias_gets_weight_one(self):
        """Completely unknown severity string → weight=1.0 (low default)."""
        a = _action("notaseverity", blast_radius=5, recurrence_count=0)
        score_action(a)
        self.assertEqual(a["severity_weight"], 1.0)


# --------------------------------------------------------------------------- #
# score_all
# --------------------------------------------------------------------------- #

class TestScoreAll(unittest.TestCase):
    """Tests for score_all() batch scoring and sorting."""

    def _make_actions(self):
        return [
            _action("low",      blast_radius=3,  recurrence_count=0),   # 1.0
            _action("critical", blast_radius=75, recurrence_count=5),   # 24.0
            _action("medium",   blast_radius=15, recurrence_count=2),   # 6.0
            _action("high",     blast_radius=5,  recurrence_count=0),   # 3.0
        ]

    def test_returns_all_actions(self):
        """score_all returns all input actions."""
        actions = self._make_actions()
        result  = score_all(actions)
        self.assertEqual(len(result), 4)

    def test_sorted_descending_by_score(self):
        """Actions are sorted highest-score-first."""
        actions = self._make_actions()
        result  = score_all(actions)
        scores  = [a["priority_score"] for a in result]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_highest_score_is_first(self):
        """The critical/high-blast/high-recurrence action is first."""
        actions = self._make_actions()
        result  = score_all(actions)
        self.assertEqual(result[0]["priority_score"], 24.0)

    def test_lowest_score_is_last(self):
        """The low/small-blast/first-occurrence action is last."""
        actions = self._make_actions()
        result  = score_all(actions)
        self.assertEqual(result[-1]["priority_score"], 1.0)

    def test_below_threshold_flagged(self):
        """Actions below threshold are flagged but still returned."""
        actions = self._make_actions()
        result  = score_all(actions, min_score_threshold=2.0)
        below   = [a for a in result if a["below_threshold"]]
        self.assertEqual(len(below), 1)
        self.assertEqual(below[0]["priority_score"], 1.0)

    def test_all_fields_attached_to_every_action(self):
        """Every action in the result has all expected scoring keys."""
        actions = self._make_actions()
        result  = score_all(actions)
        for a in result:
            for key in ("severity", "severity_weight", "blast_radius",
                        "blast_radius_factor", "recurrence_count",
                        "recurrence_factor", "priority_score", "below_threshold"):
                self.assertIn(key, a, f"Key '{key}' missing from action")

    def test_empty_list_returns_empty(self):
        """score_all([]) returns [] without error."""
        self.assertEqual(score_all([]), [])

    def test_single_action_returned(self):
        """score_all with one action returns that action scored."""
        a      = _action("high", blast_radius=25, recurrence_count=3)
        result = score_all([a])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["priority_score"], 9.0)

    def test_threshold_passed_to_each_action(self):
        """Custom min_score_threshold is applied to every action."""
        actions = self._make_actions()
        result  = score_all(actions, min_score_threshold=5.0)
        below   = [a for a in result if a["below_threshold"]]
        # Scores 1.0 and 3.0 are below 5.0
        self.assertEqual(len(below), 2)


# --------------------------------------------------------------------------- #
# explain_score
# --------------------------------------------------------------------------- #

class TestExplainScore(unittest.TestCase):
    """Tests for the explain_score() human-readable output."""

    def setUp(self):
        self.action = _action("high", blast_radius=25, recurrence_count=3)
        score_action(self.action)
        self.action["site_name"] = "Branch Office"

    def test_contains_score_value(self):
        """Explanation includes the numeric priority score."""
        explanation = explain_score(self.action)
        self.assertIn("9.00", explanation)

    def test_contains_severity(self):
        """Explanation includes the severity label."""
        explanation = explain_score(self.action)
        self.assertIn("high", explanation)

    def test_contains_blast_radius(self):
        """Explanation includes the blast radius count."""
        explanation = explain_score(self.action)
        self.assertIn("25", explanation)

    def test_contains_recurrence(self):
        """Explanation includes the recurrence count."""
        explanation = explain_score(self.action)
        self.assertIn("3", explanation)

    def test_contains_site_name(self):
        """Explanation includes the site name."""
        explanation = explain_score(self.action)
        self.assertIn("Branch Office", explanation)

    def test_contains_formula(self):
        """Explanation includes the multiplication formula."""
        explanation = explain_score(self.action)
        # high(3.0) × medium_blast(2.0) × medium_rec(1.5) = 9.0
        self.assertIn("3.0 × 2.0 × 1.5", explanation)

    def test_below_threshold_mention(self):
        """Below-threshold actions mention threshold in explanation."""
        a = _action("low", blast_radius=3, recurrence_count=0)
        score_action(a, min_score_threshold=2.0)
        explanation = explain_score(a)
        self.assertIn("threshold", explanation.lower())

    def test_above_threshold_no_skip_mention(self):
        """Above-threshold actions do not mention 'skipped' or 'threshold'."""
        explanation = explain_score(self.action)
        self.assertNotIn("skipped", explanation.lower())

    def test_first_occurrence_text(self):
        """Zero recurrences produces 'first occurrence' text."""
        a = _action("medium", blast_radius=5, recurrence_count=0)
        score_action(a)
        explanation = explain_score(a)
        self.assertIn("first occurrence", explanation)

    def test_returns_string(self):
        """explain_score always returns a non-empty string."""
        explanation = explain_score(self.action)
        self.assertIsInstance(explanation, str)
        self.assertGreater(len(explanation), 10)


# --------------------------------------------------------------------------- #
# Runner
# --------------------------------------------------------------------------- #

if __name__ == "__main__":
    unittest.main(verbosity=2)