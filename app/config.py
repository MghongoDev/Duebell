"""Runtime settings for Duebell (issues #9, #21, #24, #26, #38, #47, #85; ADRs 0005, 0006, 0008)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache

from dotenv import load_dotenv

load_dotenv()


class ConfigError(ValueError):
    """A setting holds a value Duebell cannot read. Startup stops (issue #85)."""


TRUE_WORDS = frozenset({"1", "true", "yes", "on"})
FALSE_WORDS = frozenset({"0", "false", "no", "off"})


def _bool(name: str, default: bool) -> bool:
    """Read an on/off setting. Every one goes through here (issue #85).

    Unset or blank keeps the default. Case and surrounding spaces are ignored.
    Any other word stops startup rather than picking a side.
    """
    value = _env(name).strip().lower()
    if not value:
        return default
    if value in TRUE_WORDS:
        return True
    if value in FALSE_WORDS:
        return False
    raise ConfigError(
        f"{name}={os.getenv(name, '').strip()!r} is not an on/off value. "
        "Use one of: on, off, yes, no, true, false, 1, 0."
    )


@dataclass(frozen=True)
class Settings:
    paypal_mode: str = "mock"
    # Mock mode cannot check a webhook's signature, so it refuses webhooks
    # unless this is on. Tests and the local demo turn it on (issue #38).
    mock_webhooks: bool = False
    llm_mode: str = "mock"
    paypal_client_id: str = ""
    paypal_client_secret: str = ""
    paypal_webhook_id: str = ""
    paypal_invoicer_email: str = ""
    paypal_base_url: str = "https://api-m.sandbox.paypal.com"
    anthropic_api_key: str = ""
    anthropic_base_url: str = ""
    anthropic_auth_token: str = ""
    anthropic_model: str = "claude-haiku-5-5"
    amount_cap_cents: int = 1_000_000
    allowed_currencies: tuple[str, ...] = ("USD",)
    terms_min_days: int = 0
    terms_max_days: int = 120
    database_url: str = "sqlite:///./duebell.db"
    reminder_window_days: int = 3
    paypal_timeout_seconds: float = 20.0
    llm_timeout_seconds: float = 30.0
    email_mode: str = "mock"
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    smtp_starttls: bool = True
    smtp_timeout_seconds: float = 20.0
    secret_key: str = ""
    cookie_secure: bool = False


def _env(name: str, default: str = "") -> str:
    """Read a setting. A blank value, as `.env.example` leaves many, means unset."""
    value = os.getenv(name, "")
    return value if value.strip() else default


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    currencies = tuple(
        part.strip().upper()
        for part in _env("ALLOWED_CURRENCIES", "USD").split(",")
        if part.strip()
    )
    return Settings(
        paypal_mode=_env("PAYPAL_MODE", "mock").strip().lower(),
        mock_webhooks=_bool("MOCK_WEBHOOKS", False),
        llm_mode=_env("LLM_MODE", "mock").strip().lower(),
        paypal_client_id=_env("PAYPAL_CLIENT_ID", "").strip(),
        paypal_client_secret=_env("PAYPAL_CLIENT_SECRET", "").strip(),
        paypal_webhook_id=_env("PAYPAL_WEBHOOK_ID", "").strip(),
        paypal_invoicer_email=_env("PAYPAL_INVOICER_EMAIL", "").strip(),
        paypal_base_url=_env("PAYPAL_BASE_URL", "https://api-m.sandbox.paypal.com").rstrip("/"),
        anthropic_api_key=_env("ANTHROPIC_API_KEY", "").strip(),
        anthropic_base_url=_env("ANTHROPIC_BASE_URL", "").rstrip("/"),
        anthropic_auth_token=_env("ANTHROPIC_AUTH_TOKEN", "").strip(),
        anthropic_model=_env("ANTHROPIC_MODEL", "claude-haiku-5-5").strip(),
        amount_cap_cents=int(_env("AMOUNT_CAP_CENTS", "1000000")),
        allowed_currencies=currencies or ("USD",),
        terms_min_days=int(_env("TERMS_MIN_DAYS", "0")),
        terms_max_days=int(_env("TERMS_MAX_DAYS", "120")),
        database_url=_env("DATABASE_URL", "sqlite:///./duebell.db"),
        reminder_window_days=int(_env("REMINDER_WINDOW_DAYS", "3")),
        paypal_timeout_seconds=float(_env("PAYPAL_TIMEOUT_SECONDS", "20")),
        llm_timeout_seconds=float(_env("LLM_TIMEOUT_SECONDS", "30")),
        email_mode=_env("EMAIL_MODE", "mock").strip().lower(),
        smtp_host=_env("SMTP_HOST", "").strip(),
        smtp_port=int(_env("SMTP_PORT", "587")),
        smtp_username=_env("SMTP_USERNAME", "").strip(),
        smtp_password=_env("SMTP_PASSWORD", ""),
        smtp_from=_env("SMTP_FROM", "").strip(),
        smtp_starttls=_bool("SMTP_STARTTLS", True),
        smtp_timeout_seconds=float(_env("SMTP_TIMEOUT_SECONDS", "20")),
        secret_key=_env("SECRET_KEY", "").strip(),
        cookie_secure=_bool("COOKIE_SECURE", False),
    )
