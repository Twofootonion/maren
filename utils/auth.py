# Python 3.11+
# utils/auth.py — Credential loading and authenticated HTTP session factory.
#
# Tokens are read exclusively from environment variables (via .env or shell).
# They are NEVER logged, printed, or embedded in any data structure that
# leaves this module. The only external surface is the requests.Session object
# whose Authorization header is set once at construction time.

import os
from functools import lru_cache

import requests
from dotenv import load_dotenv

from utils.logger import get_logger

logger = get_logger(__name__)

# Load .env file if present. Silent if the file does not exist — production
# deployments are expected to inject variables directly into the environment.
load_dotenv(override=False)

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

_REQUIRED_ENV_VARS = ("MIST_API_TOKEN", "MIST_ORG_ID")
_DEFAULT_BASE_URL = "https://api.mist.com/api/v1"


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #


class AuthError(Exception):
    """Raised when required credentials are missing or structurally invalid."""


@lru_cache(maxsize=1)
def get_credentials() -> dict[str, str]:
    """Load and validate Mist API credentials from environment variables.

    Reads MIST_API_TOKEN, MIST_ORG_ID, and optionally MIST_BASE_URL.
    The token value is validated to be non-empty but is never written to
    any log output.

    Parameters
    ----------
    None — reads from the process environment.

    Returns
    -------
    dict[str, str]
        Keys: ``org_id``, ``base_url``.
        The API token is intentionally NOT included in the returned dict;
        callers obtain an authenticated session via :func:`build_session`.

    Raises
    ------
    AuthError
        If MIST_API_TOKEN or MIST_ORG_ID are absent or empty.
    """
    missing = [var for var in _REQUIRED_ENV_VARS if not os.getenv(var, "").strip()]
    if missing:
        raise AuthError(
            f"Missing required environment variable(s): {', '.join(missing)}. "
            "Set them in your .env file or shell environment."
        )

    base_url = os.getenv("MIST_BASE_URL", _DEFAULT_BASE_URL).rstrip("/")

    # Log that credentials were found, but never log the token value itself.
    logger.info(
        "Credentials loaded",
        extra={"org_id": os.environ["MIST_ORG_ID"], "base_url": base_url},
    )

    return {
        "org_id": os.environ["MIST_ORG_ID"],
        "base_url": base_url,
    }


def build_session() -> requests.Session:
    """Create a requests.Session pre-configured with Mist auth headers.

    The Authorization header is set to ``Token <MIST_API_TOKEN>`` as required
    by the Mist API. The Content-Type and Accept headers are also set to
    application/json for all requests.

    Parameters
    ----------
    None — reads token from environment via :func:`get_credentials`.

    Returns
    -------
    requests.Session
        A session object ready for use with the Mist API.  Callers should
        treat this as a long-lived object and reuse it across requests.

    Raises
    ------
    AuthError
        Propagated from :func:`get_credentials` if env vars are missing.
    """
    # Call get_credentials() to trigger validation even though we read the
    # token directly from env here. This ensures the cache warms up and the
    # org_id/base_url are validated in the same step.
    get_credentials()

    token = os.environ["MIST_API_TOKEN"].strip()

    session = requests.Session()
    session.headers.update(
        {
            # Mist requires exactly this header format.
            "Authorization": f"Token {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
    )

    # Clear the local reference immediately — it now lives only inside the
    # session object's internal header dict, which we do not log.
    del token

    logger.debug("Authenticated HTTP session created")
    return session


def get_org_id() -> str:
    """Convenience accessor for the org ID.

    Parameters
    ----------
    None.

    Returns
    -------
    str
        The MIST_ORG_ID value from the environment.

    Raises
    ------
    AuthError
        If the environment variable is missing.
    """
    return get_credentials()["org_id"]


def get_base_url() -> str:
    """Convenience accessor for the API base URL.

    Parameters
    ----------
    None.

    Returns
    -------
    str
        The base URL, defaulting to ``https://api.mist.com/api/v1``.

    Raises
    ------
    AuthError
        If required credentials are missing (triggers full validation).
    """
    return get_credentials()["base_url"]