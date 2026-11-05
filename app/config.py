"""Runtime settings for Duebell (issue #9)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache

from dotenv import load_dotenv

load_dotenv()


def _bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    paypal_mode: str = "mock"
    llm_mode: str = "mock"
    paypal_client_id: str = ""
    paypal_client_secret: str = ""
    paypal_webhook_id: str = ""
    paypal_invoicer_email: str = ""
    paypal_base_url: str = "https://api-m.sandbox.paypal.com"
    anthropic_api_key: str = ""
    anthropic_base_url: str = ""
    anthropic_auth_token: str = ""
    anthropic_model: str = "claude-3-5-haiku-20241022"
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


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    currencies = tuple(
        part.strip().upper()
        for part in os.getenv("ALLOWED_CURRENCIES", "USD").split(",")
        if part.strip()
    )
    return Settings(
        paypal_mode=os.getenv("PAYPAL_MODE", "mock").strip().lower(),
        llm_mode=os.getenv("LLM_MODE", "mock").strip().lower(),
        paypal_client_id=os.getenv("PAYPAL_CLIENT_ID", "").strip(),
        paypal_client_secret=os.getenv("PAYPAL_CLIENT_SECRET", "").strip(),
        paypal_webhook_id=os.getenv("PAYPAL_WEBHOOK_ID", "").strip(),
        paypal_invoicer_email=os.getenv("PAYPAL_INVOICER_EMAIL", "").strip(),
        paypal_base_url=os.getenv("PAYPAL_BASE_URL", "https://api-m.sandbox.paypal.com").rstrip("/"),
        anthropic_api_key=os.getenv("ANTHROPIC_API_KEY", "").strip(),
        anthropic_base_url=os.getenv("ANTHROPIC_BASE_URL", "").rstrip("/"),
        anthropic_auth_token=os.getenv("ANTHROPIC_AUTH_TOKEN", "").strip(),
        anthropic_model=os.getenv("ANTHROPIC_MODEL", "claude-3-5-haiku-20241022").strip(),
        amount_cap_cents=int(os.getenv("AMOUNT_CAP_CENTS", "1000000")),
        allowed_currencies=currencies or ("USD",),
        terms_min_days=int(os.getenv("TERMS_MIN_DAYS", "0")),
        terms_max_days=int(os.getenv("TERMS_MAX_DAYS", "120")),
        database_url=os.getenv("DATABASE_URL", "sqlite:///./duebell.db"),
        reminder_window_days=int(os.getenv("REMINDER_WINDOW_DAYS", "3")),
        paypal_timeout_seconds=float(os.getenv("PAYPAL_TIMEOUT_SECONDS", "20")),
        llm_timeout_seconds=float(os.getenv("LLM_TIMEOUT_SECONDS", "30")),
        email_mode=os.getenv("EMAIL_MODE", "mock").strip().lower(),
        smtp_host=os.getenv("SMTP_HOST", "").strip(),
        smtp_port=int(os.getenv("SMTP_PORT", "587")),
        smtp_username=os.getenv("SMTP_USERNAME", "").strip(),
        smtp_password=os.getenv("SMTP_PASSWORD", ""),
        smtp_from=os.getenv("SMTP_FROM", "").strip(),
        smtp_starttls=os.getenv("SMTP_STARTTLS", "true").strip().lower() not in {"0", "false", "no"},
        smtp_timeout_seconds=float(os.getenv("SMTP_TIMEOUT_SECONDS", "20")),
    )
