"""PayPal gateway for Duebell (issues #10, #11).

The app talks to one protocol. A mock adapter keeps the demo offline, and a
sandbox adapter calls PayPal's Invoicing v2 REST API. See ADR 0001.
"""

from __future__ import annotations

import json
import time
from datetime import datetime
from typing import Mapping, Protocol
from uuid import uuid4

import httpx

from app.config import Settings


class PayPalError(RuntimeError):
    pass


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

    def __init__(self, settings: Settings, client: httpx.Client | None = None) -> None:
        self.settings = settings
        self.client = client or httpx.Client(
            base_url=settings.paypal_base_url,
            timeout=settings.paypal_timeout_seconds,
        )
        self._token: str | None = None
        self._token_expiry = 0.0

    def _access_token(self) -> str:
        if not self.settings.paypal_client_id or not self.settings.paypal_client_secret:
            raise PayPalError("PayPal credentials are missing.")
        if self._token and time.time() < self._token_expiry - 60:
            return self._token
        response = self.client.post(
            "/v1/oauth2/token",
            auth=(self.settings.paypal_client_id, self.settings.paypal_client_secret),
            data={"grant_type": "client_credentials"},
        )
        if response.status_code >= 400:
            raise PayPalError(f"PayPal token request failed: {response.text[:200]}")
        data = response.json()
        self._token = data["access_token"]
        self._token_expiry = time.time() + float(data.get("expires_in", 3000))
        return self._token

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._access_token()}", "Content-Type": "application/json"}

    def _request(self, method: str, url: str, **kwargs) -> httpx.Response:
        response = self.client.request(method, url, headers=self._headers(), **kwargs)
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
            "payment_term": {"term_type": f"NET_{terms_days}"},
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
                "date": datetime.utcnow().date().isoformat(),
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


def build_gateway(settings: Settings) -> PayPalGateway:
    if settings.paypal_mode == "sandbox":
        return SandboxPayPal(settings)
    return MockPayPal()
