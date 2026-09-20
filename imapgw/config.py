"""Settings, environment loading, and secret-redacting logging."""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Mapping
from dataclasses import dataclass, fields, replace
from pathlib import Path

LOGGER_NAME = "imapgw"


@dataclass(frozen=True)
class Settings:
    imap_host: str = "127.0.0.1"
    imap_port: int = 1143
    api_url: str = "http://127.0.0.1:3210/v0"
    allowed_inbox_id: str | None = None
    db_path: Path = Path("imapgw.sqlite3")
    idle_timeout: float = 1800.0
    http_timeout: float = 10.0
    command_timeout: float = 30.0
    # Absolute deadline for assembling one command (literals included), independent of idle.
    assembly_timeout: float = 60.0
    # Longest we wait for a client to accept our output before dropping the connection.
    write_timeout: float = 30.0
    # After a mutating command has already answered OK, how long to wait for the follow-up
    # re-listing so its EXISTS can be announced promptly. Never affects the command's result.
    post_command_wait: float = 2.0
    max_connections: int = 100
    max_attempts: int = 3
    retry_budget: float = 10.0
    backoff_base: float = 0.5
    refresh_interval: float = 5.0
    cache_bytes: int = 64 * 1024 * 1024
    max_line: int = 64 * 1024
    max_literal: int = 1024 * 1024
    max_command_bytes: int = 1024 * 1024 + 64 * 1024
    max_literals: int = 16
    log_level: str = "INFO"

    def with_overrides(self, **kwargs: object) -> Settings:
        return replace(self, **kwargs)


_ENV_MAP: dict[str, tuple[str, type]] = {
    "IMAP_HOST": ("imap_host", str),
    "IMAP_PORT": ("imap_port", int),
    "AGENTMAIL_API_URL": ("api_url", str),
    "AGENTMAIL_INBOX_ID": ("allowed_inbox_id", str),
    "IMAPGW_DB_PATH": ("db_path", Path),
    "IMAPGW_IDLE_TIMEOUT": ("idle_timeout", float),
    "IMAPGW_HTTP_TIMEOUT": ("http_timeout", float),
    "IMAPGW_COMMAND_TIMEOUT": ("command_timeout", float),
    "IMAPGW_ASSEMBLY_TIMEOUT": ("assembly_timeout", float),
    "IMAPGW_WRITE_TIMEOUT": ("write_timeout", float),
    "IMAPGW_POST_COMMAND_WAIT": ("post_command_wait", float),
    "IMAPGW_MAX_CONNECTIONS": ("max_connections", int),
    "IMAPGW_HTTP_RETRIES": ("max_attempts", int),
    "IMAPGW_REFRESH_INTERVAL": ("refresh_interval", float),
    "IMAPGW_CACHE_BYTES": ("cache_bytes", int),
    "IMAPGW_LOG_LEVEL": ("log_level", str),
    "IMAPGW_MAX_LINE": ("max_line", int),
    "IMAPGW_MAX_LITERAL": ("max_literal", int),
    "IMAPGW_MAX_COMMAND_BYTES": ("max_command_bytes", int),
    "IMAPGW_MAX_LITERALS": ("max_literals", int),
}


def parse_dotenv(text: str) -> dict[str, str]:
    """Minimal KEY=VALUE parser: ignores blanks and comments, strips matching quotes."""
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            out[key] = value
    return out


def load_settings(
    environ: Mapping[str, str] | None = None, dotenv: Path | None = Path(".env")
) -> Settings:
    env: dict[str, str] = {}
    if dotenv is not None and dotenv.is_file():
        env.update(parse_dotenv(dotenv.read_text(encoding="utf-8")))
    env.update(os.environ if environ is None else environ)

    values: dict[str, object] = {}
    for var, (attr, conv) in _ENV_MAP.items():
        if var in env and env[var] != "":
            try:
                values[attr] = conv(env[var])
            except (TypeError, ValueError) as exc:
                raise ValueError(f"invalid value for {var}: {exc}") from exc
    settings = Settings(**values)  # type: ignore[arg-type]

    if "AGENTMAIL_API_KEY" in env:
        logging.getLogger(LOGGER_NAME).debug(
            "AGENTMAIL_API_KEY present in environment; ignored, credentials come from LOGIN"
        )
    settings = settings.with_overrides(api_url=settings.api_url.rstrip("/"))
    return settings


class RedactingFilter(logging.Filter):
    """Replaces registered secrets with *** in a log record's message, exception text, and
    stack text. Registrations are reference-counted so a secret shared by several sessions
    stays redacted until the last one releases it."""

    def __init__(self) -> None:
        super().__init__()
        self._secrets: dict[str, int] = {}
        self._lock = threading.Lock()

    def register(self, secret: str) -> None:
        if secret:
            with self._lock:
                self._secrets[secret] = self._secrets.get(secret, 0) + 1

    def unregister(self, secret: str) -> None:
        with self._lock:
            count = self._secrets.get(secret, 0) - 1
            if count <= 0:
                self._secrets.pop(secret, None)
            else:
                self._secrets[secret] = count

    def _scrub(self, text: str, secrets: list[str]) -> str:
        for secret in secrets:
            text = text.replace(secret, "***")
        return text

    def filter(self, record: logging.LogRecord) -> bool:
        with self._lock:
            secrets = sorted(self._secrets, key=len, reverse=True)
        if not secrets:
            return True
        try:
            message = record.getMessage()
        except Exception:  # pragma: no cover - defensive
            return True
        record.msg = self._scrub(message, secrets)
        record.args = ()
        if record.exc_info:
            formatter = logging.Formatter()
            record.exc_text = self._scrub(formatter.formatException(record.exc_info), secrets)
            record.exc_info = None
        elif record.exc_text:
            record.exc_text = self._scrub(record.exc_text, secrets)
        if record.stack_info:
            record.stack_info = self._scrub(record.stack_info, secrets)
        return True


REDACTOR = RedactingFilter()


def configure_logging(level: str = "INFO") -> None:
    root = logging.getLogger()
    root.setLevel(level.upper())
    if not any(isinstance(f, RedactingFilter) for f in root.filters):
        root.addFilter(REDACTOR)
    if not root.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
        root.addHandler(handler)
    for handler in root.handlers:
        if not any(isinstance(f, RedactingFilter) for f in handler.filters):
            handler.addFilter(REDACTOR)
    # Records emitted on child loggers propagate to root handlers, whose filters redact them.
    for name in (LOGGER_NAME,):
        logger = logging.getLogger(name)
        if not any(isinstance(f, RedactingFilter) for f in logger.filters):
            logger.addFilter(REDACTOR)


def settings_field_names() -> list[str]:
    return [f.name for f in fields(Settings)]
