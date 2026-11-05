"""PayPal gateway for Duebell (issues #10, #11, #21, #29).

The app talks to one protocol. The mock gateway keeps the demo offline, the
sandbox gateway calls PayPal's Invoicing v2 REST API, and the toolkit gateway
routes most calls through PayPal's Agent Toolkit. See ADRs 0001 and 0006.
"""

from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from datetime import datetime, timezone
from typing import Mapping, Protocol
from uuid import uuid4

import httpx

from app.config import Settings
from app.retry import RetryableFailure, call_with_retries


class PayPalError(RuntimeError):
    pass


class PayPalTimeout(PayPalError):
    """PayPal did not answer in time, and the call may still complete.

    The toolkit's HTTP call keeps running in its worker thread after the app
    stops waiting, so the caller must not assume the action failed.
    """


def term_type(days: int) -> str:
    """Map local net terms to the values PayPal Invoicing accepts."""
    if days <= 0:
        return "DUE_ON_RECEIPT"
    if days <= 10:
        return "NET_10"
    if days <= 15:
        return "NET_15"
    if days <= 30:
        return "NET_30"
    if days <= 45:
        return "NET_45"
    if days <= 60:
        return "NET_60"
    return "NET_90"


class PayPalGateway(Protocol):
    def create_invoice(
        self,
        *,
        invoice_number: str,
        amount_cents: int,
        currency: str,
        description: str,
        terms_days: int,
        client_name: str,
        client_email: str,
        issued_at: datetime,
        due_at: datetime,
    ) -> str: ...

    def send_invoice(self, paypal_invoice_id: str) -> None: ...

    def send_reminder(self, paypal_invoice_id: str, subject: str, note: str) -> None: ...

    def record_payment(self, paypal_invoice_id: str, amount_cents: int, currency: str) -> None: ...

    def get_invoice(self, paypal_invoice_id: str) -> str: ...

    def verify_webhook(self, headers: Mapping[str, str], body: str) -> bool: ...


class MockPayPal:
    """In-memory PayPal. It records the calls and moves invoice status."""

    def __init__(self) -> None:
        self.invoices: dict[str, dict] = {}
        self.sent_reminders: list[dict] = []
        self.verify_result = True

    def create_invoice(self, **facts) -> str:
        paypal_id = f"MOCK-{uuid4().hex[:12].upper()}"
        self.invoices[paypal_id] = {**facts, "status": "DRAFT"}
        return paypal_id

    def send_invoice(self, paypal_invoice_id: str) -> None:
        invoice = self._get(paypal_invoice_id)
        invoice["status"] = "SENT"

    def send_reminder(self, paypal_invoice_id: str, subject: str, note: str) -> None:
        self._get(paypal_invoice_id)
        self.sent_reminders.append(
            {"paypal_invoice_id": paypal_invoice_id, "subject": subject, "note": note}
        )

    def record_payment(self, paypal_invoice_id: str, amount_cents: int, currency: str) -> None:
        invoice = self._get(paypal_invoice_id)
        invoice["status"] = "PAID"
        invoice["paid_cents"] = amount_cents

    def get_invoice(self, paypal_invoice_id: str) -> str:
        return self._get(paypal_invoice_id)["status"]

    def verify_webhook(self, headers: Mapping[str, str], body: str) -> bool:
        return self.verify_result

    def _get(self, paypal_invoice_id: str) -> dict:
        try:
            return self.invoices[paypal_invoice_id]
        except KeyError as exc:
            raise PayPalError(f"Unknown PayPal invoice {paypal_invoice_id}.") from exc


class SandboxPayPal:
    """PayPal Invoicing v2 over REST. It caches the access token."""

    def __init__(
        self,
        settings: Settings,
        client: httpx.Client | None = None,
        retry_delay: float = 0.5,
    ) -> None:
        self.settings = settings
        self.client = client or httpx.Client(
            base_url=settings.paypal_base_url,
            timeout=settings.paypal_timeout_seconds,
        )
        self.retry_delay = retry_delay
        self._token: str | None = None
        self._token_expiry = 0.0

    def _access_token(self) -> str:
        if not self.settings.paypal_client_id or not self.settings.paypal_client_secret:
            raise PayPalError("PayPal credentials are missing.")
        if self._token and time.time() < self._token_expiry - 60:
            return self._token
        def fetch_token() -> httpx.Response:
            response = self.client.post(
                "/v1/oauth2/token",
                auth=(self.settings.paypal_client_id, self.settings.paypal_client_secret),
                data={"grant_type": "client_credentials"},
            )
            if response.status_code == 429 or response.status_code >= 500:
                raise RetryableFailure(f"PayPal token request returned {response.status_code}")
            return response

        try:
            response = call_with_retries(fetch_token, base_delay=self.retry_delay)
        except (httpx.HTTPError, RetryableFailure) as exc:
            raise PayPalError(f"PayPal token request failed: {exc}") from exc
        if response.status_code >= 400:
            raise PayPalError(f"PayPal token request failed: {response.text[:200]}")
        data = response.json()
        self._token = data["access_token"]
        self._token_expiry = time.time() + float(data.get("expires_in", 3000))
        return self._token

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._access_token()}", "Content-Type": "application/json"}

    def _request(self, method: str, url: str, **kwargs) -> httpx.Response:
        # One ID per action, chosen before the retry loop, so PayPal can treat
        # a retry as the same request (issue #29). PayPal documents this for
        # creating invoices; send, remind and payments do not document it.
        request_id = str(uuid4()) if method in {"POST", "PUT"} else None

        def send() -> httpx.Response:
            headers = self._headers()
            if request_id:
                headers["PayPal-Request-Id"] = request_id
            response = self.client.request(method, url, headers=headers, **kwargs)
            if response.status_code == 429 or response.status_code >= 500:
                raise RetryableFailure(f"PayPal returned {response.status_code}")
            return response

        try:
            response = call_with_retries(send, base_delay=self.retry_delay)
        except (httpx.HTTPError, RetryableFailure) as exc:
            raise PayPalError(f"PayPal could not be reached after retries: {exc}") from exc
        if response.status_code >= 400:
            raise PayPalError(f"PayPal rejected the call: {response.text[:300]}")
        return response

    def create_invoice(
        self,
        *,
        invoice_number: str,
        amount_cents: int,
        currency: str,
        description: str,
        terms_days: int,
        client_name: str,
        client_email: str,
        issued_at: datetime,
        due_at: datetime,
    ) -> str:
        detail = {
            "invoice_number": invoice_number,
            "invoice_date": issued_at.date().isoformat(),
            "currency_code": currency,
            "note": description,
            "payment_term": {"term_type": term_type(terms_days)},
        }
        invoicer: dict = {"business_name": "Duebell"}
        if self.settings.paypal_invoicer_email:
            invoicer["email_address"] = self.settings.paypal_invoicer_email
        body = {
            "detail": detail,
            "invoicer": invoicer,
            "primary_recipients": [
                {"billing_info": {"email_address": client_email}, "name": {"given_name": client_name}}
            ],
            "items": [
                {
                    "name": description,
                    "quantity": "1",
                    "unit_amount": {"currency_code": currency, "value": f"{amount_cents / 100:.2f}"},
                }
            ],
        }
        response = self._request("POST", "/v2/invoicing/invoices", json=body)
        location = response.headers.get("Location") or response.headers.get("location")
        if location:
            return location.rstrip("/").split("/")[-1]
        data = response.json()
        href = data.get("href") or ""
        if href:
            return href.rstrip("/").split("/")[-1]
        raise PayPalError("PayPal did not return an invoice id.")

    def send_invoice(self, paypal_invoice_id: str) -> None:
        self._request(
            "POST",
            f"/v2/invoicing/invoices/{paypal_invoice_id}/send",
            json={"send_to_recipient": True, "send_to_invoicer": True},
        )

    def send_reminder(self, paypal_invoice_id: str, subject: str, note: str) -> None:
        self._request(
            "POST",
            f"/v2/invoicing/invoices/{paypal_invoice_id}/remind",
            json={"subject": subject, "note": note, "send_to_recipient": True},
        )

    def record_payment(self, paypal_invoice_id: str, amount_cents: int, currency: str) -> None:
        self._request(
            "POST",
            f"/v2/invoicing/invoices/{paypal_invoice_id}/payments",
            json={
                "method": "BANK_TRANSFER",
                "amount": {"currency_code": currency, "value": f"{amount_cents / 100:.2f}"},
                "date": datetime.now(timezone.utc).date().isoformat(),
            },
        )

    def get_invoice(self, paypal_invoice_id: str) -> str:
        response = self._request("GET", f"/v2/invoicing/invoices/{paypal_invoice_id}")
        return response.json().get("status", "UNKNOWN")

    def verify_webhook(self, headers: Mapping[str, str], body: str) -> bool:
        if not self.settings.paypal_webhook_id:
            return False
        lowered = {key.lower(): value for key, value in headers.items()}
        try:
            event = json.loads(body)
        except json.JSONDecodeError:
            return False
        payload = {
            "auth_algo": lowered.get("paypal-auth-algo", ""),
            "cert_url": lowered.get("paypal-cert-url", ""),
            "transmission_id": lowered.get("paypal-transmission-id", ""),
            "transmission_sig": lowered.get("paypal-transmission-sig", ""),
            "transmission_time": lowered.get("paypal-transmission-time", ""),
            "webhook_id": self.settings.paypal_webhook_id,
            "webhook_event": event,
        }
        try:
            response = self._request("POST", "/v1/notifications/verify-webhook-signature", json=payload)
        except PayPalError:
            return False
        return response.json().get("verification_status") == "SUCCESS"


class ToolkitPayPal(SandboxPayPal):
    """PayPal through the official Agent Toolkit (issue #21, ADR 0006).

    The app calls the toolkit directly. The model never sees these tools
    (ADR 0004), and only an approved reminder reaches send_reminder (ADR 0003).

    Create stays on the REST path inherited from SandboxPayPal, because the
    toolkit's create and update tools cannot set payment terms or a due date.
    Webhook verification also stays inherited: the toolkit has no tool for it.
    The toolkit sets no HTTP timeout, so each call runs under the app's own.
    """

    def __init__(self, settings: Settings, toolkit=None, **kwargs) -> None:
        super().__init__(settings, **kwargs)
        self.toolkit = toolkit or _load_toolkit(settings)

    def _run(self, method: str, params: dict) -> dict:
        future = _TOOLKIT_POOL.submit(self.toolkit.run, method, params)
        try:
            result = future.result(timeout=self.settings.paypal_timeout_seconds)
        except FutureTimeout as exc:
            raise PayPalTimeout(
                f"PayPal did not answer within {self.settings.paypal_timeout_seconds:g} seconds. Try again."
            ) from exc
        except Exception as exc:  # the toolkit raises requests, pydantic and plain errors
            raise PayPalError(f"PayPal rejected the call ({method}): {_short(exc)}") from exc
        if isinstance(result, str):
            try:
                result = json.loads(result) if result.strip() else {}
            except json.JSONDecodeError:
                result = {}
        return result if isinstance(result, dict) else {}

    def send_invoice(self, paypal_invoice_id: str) -> None:
        self._run("send_invoice", {"invoice_id": paypal_invoice_id, "send_to_recipient": True})

    def send_reminder(self, paypal_invoice_id: str, subject: str, note: str) -> None:
        self._run("send_invoice_reminder", {"invoice_id": paypal_invoice_id, "subject": subject, "note": note})

    def record_payment(self, paypal_invoice_id: str, amount_cents: int, currency: str) -> None:
        self._run(
            "record_payment_for_invoice",
            {
                "invoice_id": paypal_invoice_id,
                "method": "BANK_TRANSFER",
                "payment_date": datetime.now(timezone.utc).date().isoformat(),
                "amount": {"currency_code": currency, "value": f"{amount_cents / 100:.2f}"},
            },
        )

    def get_invoice(self, paypal_invoice_id: str) -> str:
        return self._run("get_invoice", {"invoice_id": paypal_invoice_id}).get("status", "UNKNOWN")


_TOOLKIT_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="paypal-toolkit")


def _short(exc: Exception) -> str:
    text = str(exc).splitlines()[0] if str(exc) else exc.__class__.__name__
    return text[:200]


def _load_toolkit(settings: Settings):
    try:
        from paypal_agent_toolkit.shared.api import PayPalAPI
        from paypal_agent_toolkit.shared.configuration import Context
    except ImportError as exc:
        raise PayPalError(
            "PAYPAL_MODE=toolkit needs the PayPal Agent Toolkit. "
            "Run: pip install -r requirements-toolkit.txt --no-deps"
        ) from exc
    if not settings.paypal_client_id or not settings.paypal_client_secret:
        raise PayPalError("PayPal credentials are missing.")
    sandbox = "sandbox" in settings.paypal_base_url
    context = Context(sandbox=sandbox, source="DUEBELL")
    return PayPalAPI(
        client_id=settings.paypal_client_id,
        secret=settings.paypal_client_secret,
        context=context,
    )


def build_gateway(settings: Settings) -> PayPalGateway:
    if settings.paypal_mode == "sandbox":
        return SandboxPayPal(settings)
    if settings.paypal_mode == "toolkit":
        return ToolkitPayPal(settings)
    return MockPayPal()
