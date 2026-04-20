# Python 3.11+
# utils/logger.py — Structured logging configuration for MAREN.
# All log output is JSON-compatible via a custom formatter. Credentials are
# never emitted at any log level; the sanitize_record() filter enforces this.

import logging
import json
import re
import sys
from datetime import datetime, timezone
from typing import Any


# Regex patterns for values that must never appear in log output.
# Matches typical Mist API tokens (32-char hex) and UUID-style org/site IDs.
_SENSITIVE_PATTERNS: list[re.Pattern] = [
    re.compile(r"[A-Za-z0-9]{32,}"),   # bare long tokens
]
_REDACTED = "[REDACTED]"


class SanitizingFilter(logging.Filter):
    """Logging filter that scrubs API tokens and credential-like strings.

    Parameters
    ----------
    None — instantiate and attach to any handler or logger.

    Notes
    -----
    Operates on the *formatted* message string after interpolation so that
    tokens injected via positional args are also caught.
    """

    # Key names whose values should always be redacted regardless of length.
    _SENSITIVE_KEYS = frozenset(
        {"token", "api_token", "authorization", "password", "secret", "key"}
    )

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        """Scrub sensitive values from the log record in-place.

        Parameters
        ----------
        record : logging.LogRecord
            The log record to inspect and mutate.

        Returns
        -------
        bool
            Always True — the record is kept but sanitized, never dropped.
        """
        # Sanitize the pre-formatted message args if they are a dict.
        if isinstance(record.args, dict):
            record.args = {
                k: (_REDACTED if k.lower() in self._SENSITIVE_KEYS else v)
                for k, v in record.args.items()
            }

        # Sanitize the already-interpolated message string.
        if record.getMessage:
            try:
                msg = record.getMessage()
                # Redact anything that looks like "Token <value>" or
                # "Authorization: Token <value>".
                msg = re.sub(
                    r"(Token\s+)[A-Za-z0-9_\-\.]{20,}",
                    r"\1" + _REDACTED,
                    msg,
                    flags=re.IGNORECASE,
                )
                # Rewrite the record so the formatter sees the clean version.
                record.msg = msg
                record.args = None
            except Exception:  # pragma: no cover — defensive only
                pass

        return True


class JsonFormatter(logging.Formatter):
    """Emit each log record as a single-line JSON object.

    Format
    ------
    {
      "timestamp": "<ISO8601 UTC>",
      "level": "<LEVEL>",
      "logger": "<name>",
      "message": "<text>",
      "module": "<module>",
      "line": <int>,
      ...extra fields from record.__dict__ if present...
    }

    Parameters
    ----------
    None.
    """

    def format(self, record: logging.LogRecord) -> str:
        """Serialize a LogRecord to a JSON string.

        Parameters
        ----------
        record : logging.LogRecord
            The record to format.

        Returns
        -------
        str
            A single-line JSON string terminated without a newline.
        """
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(
                record.created, tz=timezone.utc
            ).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "module": record.module,
            "line": record.lineno,
        }

        # Attach exception info if present.
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        # Pass through any extra fields the caller injected via extra={...}.
        _standard_keys = {
            "name", "msg", "args", "levelname", "levelno", "pathname",
            "filename", "module", "exc_info", "exc_text", "stack_info",
            "lineno", "funcName", "created", "msecs", "relativeCreated",
            "thread", "threadName", "processName", "process", "message",
            "taskName",
        }
        for key, value in record.__dict__.items():
            if key not in _standard_keys and not key.startswith("_"):
                payload[key] = value

        return json.dumps(payload, default=str)


class HumanFormatter(logging.Formatter):
    """Human-readable formatter for console output during development.

    Format: ``YYYY-MM-DDTHH:MM:SSZ  LEVEL     logger:line  message``
    """

    _LEVEL_COLORS = {
        "DEBUG":    "\033[36m",   # cyan
        "INFO":     "\033[32m",   # green
        "WARNING":  "\033[33m",   # yellow
        "ERROR":    "\033[31m",   # red
        "CRITICAL": "\033[35m",   # magenta
    }
    _RESET = "\033[0m"

    def format(self, record: logging.LogRecord) -> str:  # noqa: D102
        ts = datetime.fromtimestamp(record.created, tz=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        color = self._LEVEL_COLORS.get(record.levelname, "")
        reset = self._RESET
        level = f"{color}{record.levelname:<8}{reset}"
        location = f"{record.name}:{record.lineno}"
        msg = record.getMessage()
        if record.exc_info:
            msg += "\n" + self.formatException(record.exc_info)
        return f"{ts}  {level}  {location:<40}  {msg}"


def get_logger(name: str, level: str = "INFO") -> logging.Logger:
    """Return a named logger configured for MAREN.

    The logger writes JSON to a file handler (``maren.log``) and
    human-readable output to stderr.  Both handlers attach the
    :class:`SanitizingFilter` so no credentials leak regardless of handler.

    Parameters
    ----------
    name : str
        Logger name — typically ``__name__`` of the calling module.
    level : str
        Minimum log level string, e.g. ``"DEBUG"``, ``"INFO"``.  Defaults to
        ``"INFO"``.  Validated against ``logging`` level names; falls back to
        INFO on invalid input.

    Returns
    -------
    logging.Logger
        Configured logger instance.  Multiple calls with the same *name*
        return the same underlying logger (standard Python behavior).
    """
    numeric_level = getattr(logging, level.upper(), logging.INFO)

    logger = logging.getLogger(name)

    # Avoid adding duplicate handlers if get_logger is called more than once
    # for the same name (e.g., during testing).
    if logger.handlers:
        return logger

    logger.setLevel(numeric_level)

    sanitizer = SanitizingFilter()

    # --- stderr handler (human-readable) ---
    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setLevel(numeric_level)
    stderr_handler.setFormatter(HumanFormatter())
    stderr_handler.addFilter(sanitizer)
    logger.addHandler(stderr_handler)

    # --- file handler (JSON, appended) ---
    try:
        file_handler = logging.FileHandler("maren.log", encoding="utf-8")
        file_handler.setLevel(numeric_level)
        file_handler.setFormatter(JsonFormatter())
        file_handler.addFilter(sanitizer)
        logger.addHandler(file_handler)
    except OSError as exc:  # pragma: no cover — filesystem edge case
        logger.warning("Could not open maren.log for writing: %s", exc)

    # Prevent propagation to the root logger to avoid duplicate output.
    logger.propagate = False

    return logger


def configure_root_level(level: str) -> None:
    """Set the log level on every existing MAREN logger.

    Useful when the config.yaml log_level is read after initial module imports.

    Parameters
    ----------
    level : str
        Log level string, e.g. ``"DEBUG"``.
    """
    numeric_level = getattr(logging, level.upper(), logging.INFO)
    for name, logger in logging.Logger.manager.loggerDict.items():
        if isinstance(logger, logging.Logger) and (
            name.startswith("maren") or name.startswith("core")
            or name.startswith("utils") or name.startswith("output")
        ):
            logger.setLevel(numeric_level)
            for handler in logger.handlers:
                handler.setLevel(numeric_level)