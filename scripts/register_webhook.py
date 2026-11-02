"""Register the PayPal webhook and print its id (issue #18).

PayPal needs a public HTTPS URL. For local sandbox work, expose the app with a
tunnel and pass the tunnel URL:

    .venv/bin/python scripts/register_webhook.py --url https://example.ngrok.app/webhooks/paypal
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx

from app.config import get_settings

EVENT_TYPES = [
    "INVOICING.INVOICE.SENT",
    "INVOICING.INVOICE.PAID",
    "INVOICING.INVOICE.UPDATED",
    "INVOICING.INVOICE.CANCELLED",
    "INVOICING.INVOICE.REFUNDED",
]


def main() -> None:
    parser = argparse.ArgumentParser(description="Register a PayPal webhook.")
    parser.add_argument("--url", required=True, help="Public HTTPS URL ending in /webhooks/paypal")
    args = parser.parse_args()

    settings = get_settings()
    if settings.paypal_mode != "sandbox":
        print("Set PAYPAL_MODE=sandbox before registering a webhook.")
        raise SystemExit(1)
    if not settings.paypal_client_id or not settings.paypal_client_secret:
        print("PayPal credentials are missing.")
        raise SystemExit(1)

    with httpx.Client(base_url=settings.paypal_base_url, timeout=settings.paypal_timeout_seconds) as client:
        token_response = client.post(
            "/v1/oauth2/token",
            auth=(settings.paypal_client_id, settings.paypal_client_secret),
            data={"grant_type": "client_credentials"},
        )
        if token_response.status_code >= 400:
            print(f"Token request failed with {token_response.status_code}.")
            raise SystemExit(1)
        token = token_response.json()["access_token"]
        response = client.post(
            "/v1/notifications/webhooks",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"url": args.url, "event_types": [{"name": name} for name in EVENT_TYPES]},
        )

    if response.status_code >= 400:
        print(f"Webhook registration failed with {response.status_code}: {response.text[:300]}")
        raise SystemExit(1)

    webhook_id = response.json().get("id", "")
    print(f"Webhook registered: {webhook_id}")
    print(f"Add PAYPAL_WEBHOOK_ID={webhook_id} to .env")


if __name__ == "__main__":
    main()
