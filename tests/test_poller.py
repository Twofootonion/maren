# Python 3.11+
# tests/test_poller.py — Unit tests for core/poller.py.
#
# All Mist API calls are mocked — no live network access.
# Compatible with both pytest and unittest (python -m unittest).

import sys
import os
import unittest
from unittest.mock import MagicMock, patch, call

import requests

# Ensure the project root is on the path regardless of how the tests are run.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.poller import (
    fetch_sites,
    fetch_marvis_actions,
    enrich_actions_with_site_metadata,
    poll,
    PollerError,
)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

BASE = "https://api.mist.com/api/v1"
ORG  = "org-test-abc123"


def _mock_resp(status: int, body) -> MagicMock:
    """Build a minimal mock requests.Response.

    Parameters
    ----------
    status : int
        HTTP status code.
    body : any
        Value returned by resp.json().

    Returns
    -------
    MagicMock
        Mock response object.
    """
    r = MagicMock(spec=requests.Response)
    r.status_code = status
    r.ok = status < 400
    r.json.return_value = body
    r.url = "https://api.mist.com/test"
    r.headers = {}
    return r


SITES_PAYLOAD = [
    {"id": "site-001", "name": "HQ",     "timezone": "America/Chicago"},
    {"id": "site-002", "name": "Branch", "timezone": "America/New_York"},
]

ACTIONS_PAYLOAD = [
    {"id": "act-001", "site_id": "site-001", "category": "auth_failure",  "severity": "high"},
    {"id": "act-002", "site_id": "site-002", "category": "wifi",           "severity": "critical"},
    {"id": "act-003", "site_id": "site-999", "category": "dhcp_failure",   "severity": "medium"},
]


# --------------------------------------------------------------------------- #
# fetch_sites tests
# --------------------------------------------------------------------------- #

class TestFetchSites(unittest.TestCase):
    """Tests for fetch_sites()."""

    def _session(self):
        return MagicMock(spec=requests.Session)

    def test_returns_list_on_200(self):
        """fetch_sites returns the full site list on HTTP 200."""
        with patch("core.poller._get", return_value=_mock_resp(200, SITES_PAYLOAD)):
            result = fetch_sites(self._session(), ORG, BASE)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["name"], "HQ")
        self.assertEqual(result[1]["name"], "Branch")

    def test_raises_on_401(self):
        """fetch_sites raises PollerError with '401' in message on unauthorised."""
        with patch("core.poller._get", return_value=_mock_resp(401, {})):
            with self.assertRaises(PollerError) as ctx:
                fetch_sites(self._session(), ORG, BASE)
        self.assertIn("401", str(ctx.exception))
        self.assertIn("nauthorized", str(ctx.exception))

    def test_raises_on_403(self):
        """fetch_sites raises PollerError on forbidden."""
        with patch("core.poller._get", return_value=_mock_resp(403, {})):
            with self.assertRaises(PollerError) as ctx:
                fetch_sites(self._session(), ORG, BASE)
        self.assertIn("403", str(ctx.exception))

    def test_raises_on_404(self):
        """fetch_sites raises PollerError when org not found."""
        with patch("core.poller._get", return_value=_mock_resp(404, {})):
            with self.assertRaises(PollerError) as ctx:
                fetch_sites(self._session(), ORG, BASE)
        self.assertIn("404", str(ctx.exception))

    def test_raises_on_500(self):
        """fetch_sites raises PollerError on server error."""
        with patch("core.poller._get", return_value=_mock_resp(500, {})):
            with self.assertRaises(PollerError) as ctx:
                fetch_sites(self._session(), ORG, BASE)
        self.assertIn("500", str(ctx.exception))

    def test_raises_on_503(self):
        """fetch_sites raises PollerError on service unavailable."""
        with patch("core.poller._get", return_value=_mock_resp(503, {})):
            with self.assertRaises(PollerError) as ctx:
                fetch_sites(self._session(), ORG, BASE)
        self.assertIn("503", str(ctx.exception))

    def test_raises_on_invalid_json(self):
        """fetch_sites raises PollerError when response is not valid JSON."""
        r = _mock_resp(200, None)
        r.json.side_effect = ValueError("No JSON")
        with patch("core.poller._get", return_value=r):
            with self.assertRaises(PollerError) as ctx:
                fetch_sites(self._session(), ORG, BASE)
        self.assertIn("JSON", str(ctx.exception))

    def test_raises_on_non_list_response(self):
        """fetch_sites raises PollerError when API returns a non-list body."""
        with patch("core.poller._get", return_value=_mock_resp(200, {"error": "bad"})):
            with self.assertRaises(PollerError) as ctx:
                fetch_sites(self._session(), ORG, BASE)
        self.assertIn("list", str(ctx.exception).lower())

    def test_raises_on_network_error(self):
        """fetch_sites raises PollerError on requests.RequestException."""
        with patch("core.poller._get", side_effect=requests.ConnectionError("refused")):
            with self.assertRaises(PollerError) as ctx:
                fetch_sites(self._session(), ORG, BASE)
        self.assertIn("Network error", str(ctx.exception))

    def test_returns_empty_list_when_api_returns_empty(self):
        """fetch_sites returns an empty list when the org has no sites."""
        with patch("core.poller._get", return_value=_mock_resp(200, [])):
            result = fetch_sites(self._session(), ORG, BASE)
        self.assertEqual(result, [])

    def test_url_constructed_correctly(self):
        """fetch_sites calls _get with the correct org-scoped URL."""
        with patch("core.poller._get", return_value=_mock_resp(200, [])) as mock_get:
            fetch_sites(self._session(), ORG, BASE)
        called_url = mock_get.call_args[0][1]
        self.assertIn(ORG, called_url)
        self.assertIn("/sites", called_url)


# --------------------------------------------------------------------------- #
# fetch_marvis_actions tests
# --------------------------------------------------------------------------- #

class TestFetchMarvisActions(unittest.TestCase):
    """Tests for fetch_marvis_actions()."""

    def _session(self):
        return MagicMock(spec=requests.Session)

    def test_returns_list_on_200_bare(self):
        """fetch_marvis_actions handles a bare JSON list response."""
        with patch("core.poller._get", return_value=_mock_resp(200, ACTIONS_PAYLOAD)):
            result = fetch_marvis_actions(self._session(), ORG, BASE)
        self.assertEqual(len(result), 3)
        self.assertEqual(result[0]["id"], "act-001")

    def test_unwraps_results_key(self):
        """fetch_marvis_actions unwraps a {'results': [...]} wrapper."""
        wrapped = {"results": ACTIONS_PAYLOAD, "total": 3}
        with patch("core.poller._get", return_value=_mock_resp(200, wrapped)):
            result = fetch_marvis_actions(self._session(), ORG, BASE)
        self.assertEqual(len(result), 3)

    def test_unwraps_actions_key(self):
        """fetch_marvis_actions unwraps a {'actions': [...]} wrapper."""
        wrapped = {"actions": ACTIONS_PAYLOAD}
        with patch("core.poller._get", return_value=_mock_resp(200, wrapped)):
            result = fetch_marvis_actions(self._session(), ORG, BASE)
        self.assertEqual(len(result), 3)

    def test_unwraps_data_key(self):
        """fetch_marvis_actions unwraps a {'data': [...]} wrapper."""
        wrapped = {"data": ACTIONS_PAYLOAD}
        with patch("core.poller._get", return_value=_mock_resp(200, wrapped)):
            result = fetch_marvis_actions(self._session(), ORG, BASE)
        self.assertEqual(len(result), 3)

    def test_single_dict_treated_as_one_action(self):
        """fetch_marvis_actions treats an unwrapped dict as a single action."""
        single = {"id": "act-solo", "site_id": "site-001", "category": "wifi"}
        with patch("core.poller._get", return_value=_mock_resp(200, single)):
            result = fetch_marvis_actions(self._session(), ORG, BASE)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["id"], "act-solo")

    def test_raises_on_401(self):
        """fetch_marvis_actions raises PollerError on 401."""
        with patch("core.poller._get", return_value=_mock_resp(401, {})):
            with self.assertRaises(PollerError) as ctx:
                fetch_marvis_actions(self._session(), ORG, BASE)
        self.assertIn("401", str(ctx.exception))

    def test_raises_on_500(self):
        """fetch_marvis_actions raises PollerError on server errors."""
        with patch("core.poller._get", return_value=_mock_resp(502, {})):
            with self.assertRaises(PollerError) as ctx:
                fetch_marvis_actions(self._session(), ORG, BASE)
        self.assertIn("502", str(ctx.exception))

    def test_raises_on_network_error(self):
        """fetch_marvis_actions raises PollerError on network failure."""
        with patch("core.poller._get", side_effect=requests.Timeout("timeout")):
            with self.assertRaises(PollerError) as ctx:
                fetch_marvis_actions(self._session(), ORG, BASE)
        self.assertIn("Network error", str(ctx.exception))

    def test_url_constructed_correctly(self):
        """fetch_marvis_actions builds the correct org-scoped URL."""
        with patch("core.poller._get", return_value=_mock_resp(200, [])) as mock_get:
            fetch_marvis_actions(self._session(), ORG, BASE)
        called_url = mock_get.call_args[0][1]
        self.assertIn(ORG, called_url)
        self.assertIn("marvis", called_url)
        self.assertIn("actions", called_url)

    def test_returns_empty_list_on_empty_response(self):
        """fetch_marvis_actions returns empty list when no actions exist."""
        with patch("core.poller._get", return_value=_mock_resp(200, [])):
            result = fetch_marvis_actions(self._session(), ORG, BASE)
        self.assertEqual(result, [])


# --------------------------------------------------------------------------- #
# enrich_actions_with_site_metadata tests
# --------------------------------------------------------------------------- #

class TestEnrichActionsWithSiteMetadata(unittest.TestCase):
    """Tests for enrich_actions_with_site_metadata()."""

    def test_attaches_site_name_and_timezone(self):
        """Site name and timezone are attached to matching actions."""
        actions = [a.copy() for a in ACTIONS_PAYLOAD[:2]]
        result  = enrich_actions_with_site_metadata(actions, SITES_PAYLOAD)
        self.assertEqual(result[0]["site_name"],     "HQ")
        self.assertEqual(result[0]["site_timezone"], "America/Chicago")
        self.assertEqual(result[1]["site_name"],     "Branch")
        self.assertEqual(result[1]["site_timezone"], "America/New_York")

    def test_unknown_site_id_gets_defaults(self):
        """Actions with unrecognised site_id get site_name='unknown', timezone='UTC'."""
        action = {"id": "act-x", "site_id": "site-999", "category": "wifi"}
        result = enrich_actions_with_site_metadata([action], SITES_PAYLOAD)
        self.assertEqual(result[0]["site_name"],     "unknown")
        self.assertEqual(result[0]["site_timezone"], "UTC")

    def test_empty_site_id_gets_defaults(self):
        """Actions with empty site_id string get fallback defaults."""
        action = {"id": "act-x", "site_id": "", "category": "wifi"}
        result = enrich_actions_with_site_metadata([action], SITES_PAYLOAD)
        self.assertEqual(result[0]["site_name"], "unknown")

    def test_missing_site_id_key_gets_defaults(self):
        """Actions with no site_id key at all get fallback defaults."""
        action = {"id": "act-x", "category": "wifi"}
        result = enrich_actions_with_site_metadata([action], SITES_PAYLOAD)
        self.assertEqual(result[0]["site_name"], "unknown")

    def test_empty_sites_list_marks_all_unknown(self):
        """All actions get 'unknown' site_name when sites list is empty."""
        actions = [a.copy() for a in ACTIONS_PAYLOAD]
        result  = enrich_actions_with_site_metadata(actions, [])
        for action in result:
            self.assertEqual(action["site_name"], "unknown")

    def test_mutates_actions_in_place(self):
        """enrich_actions_with_site_metadata mutates the input list in-place."""
        actions = [ACTIONS_PAYLOAD[0].copy()]
        returned = enrich_actions_with_site_metadata(actions, SITES_PAYLOAD)
        # Returned list is the same object — mutation happened in place.
        self.assertIs(returned, actions)
        self.assertIn("site_name", actions[0])

    def test_site_without_name_field_uses_unknown(self):
        """Sites missing the 'name' key default to 'unknown'."""
        sites = [{"id": "site-001", "timezone": "UTC"}]  # no 'name'
        action = {"id": "act-1", "site_id": "site-001"}
        result = enrich_actions_with_site_metadata([action], sites)
        self.assertEqual(result[0]["site_name"], "unknown")

    def test_site_without_timezone_field_uses_utc(self):
        """Sites missing the 'timezone' key default to 'UTC'."""
        sites = [{"id": "site-001", "name": "HQ"}]  # no 'timezone'
        action = {"id": "act-1", "site_id": "site-001"}
        result = enrich_actions_with_site_metadata([action], sites)
        self.assertEqual(result[0]["site_timezone"], "UTC")


# --------------------------------------------------------------------------- #
# poll() integration tests
# --------------------------------------------------------------------------- #

class TestPoll(unittest.TestCase):
    """Integration tests for the poll() entry point."""

    def test_poll_returns_enriched_actions(self):
        """poll() fetches sites and actions then returns enriched list."""
        with patch("core.poller.build_session", return_value=MagicMock()), \
             patch("core.poller.get_org_id",    return_value=ORG), \
             patch("core.poller.get_base_url",  return_value=BASE), \
             patch("core.poller._get", side_effect=[
                 _mock_resp(200, SITES_PAYLOAD),
                 _mock_resp(200, ACTIONS_PAYLOAD),
             ]):
            result = poll()

        self.assertEqual(len(result), 3)
        self.assertEqual(result[0]["site_name"], "HQ")
        self.assertEqual(result[1]["site_name"], "Branch")
        self.assertEqual(result[2]["site_name"], "unknown")  # site-999

    def test_poll_accepts_explicit_session_and_ids(self):
        """poll() uses provided session, org_id, and base_url without env lookup."""
        mock_session = MagicMock(spec=requests.Session)
        with patch("core.poller._get", side_effect=[
            _mock_resp(200, SITES_PAYLOAD),
            _mock_resp(200, ACTIONS_PAYLOAD),
        ]):
            result = poll(session=mock_session, org_id=ORG, base_url=BASE)
        self.assertEqual(len(result), 3)

    def test_poll_propagates_poller_error_on_site_fetch_failure(self):
        """poll() propagates PollerError if site fetch fails."""
        with patch("core.poller.build_session", return_value=MagicMock()), \
             patch("core.poller.get_org_id",    return_value=ORG), \
             patch("core.poller.get_base_url",  return_value=BASE), \
             patch("core.poller._get", return_value=_mock_resp(503, {})):
            with self.assertRaises(PollerError):
                poll()

    def test_poll_returns_empty_list_when_no_actions(self):
        """poll() returns an empty list when Marvis reports no actions."""
        with patch("core.poller.build_session", return_value=MagicMock()), \
             patch("core.poller.get_org_id",    return_value=ORG), \
             patch("core.poller.get_base_url",  return_value=BASE), \
             patch("core.poller._get", side_effect=[
                 _mock_resp(200, SITES_PAYLOAD),
                 _mock_resp(200, []),
             ]):
            result = poll()
        self.assertEqual(result, [])

    def test_poll_makes_exactly_two_get_calls(self):
        """poll() makes exactly one call for sites and one for actions."""
        with patch("core.poller.build_session", return_value=MagicMock()), \
             patch("core.poller.get_org_id",    return_value=ORG), \
             patch("core.poller.get_base_url",  return_value=BASE), \
             patch("core.poller._get", side_effect=[
                 _mock_resp(200, SITES_PAYLOAD),
                 _mock_resp(200, ACTIONS_PAYLOAD),
             ]) as mock_get:
            poll()
        self.assertEqual(mock_get.call_count, 2)


# --------------------------------------------------------------------------- #
# Runner
# --------------------------------------------------------------------------- #

if __name__ == "__main__":
    unittest.main(verbosity=2)