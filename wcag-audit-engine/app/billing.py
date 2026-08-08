"""Stripe usage-based billing for the audit endpoint.

Customers subscribe once via Checkout (no upfront line-item charge), and
every real audit call reports one Meter Event. Stripe aggregates usage and
invoices on its own billing cycle -- this module never tracks a balance
itself, so there's no custom balance-tracking code that can drift from what
Stripe actually bills.

Uses the current Stripe Billing Meters API (stripe.billing.MeterEvent), not
the legacy SubscriptionItem.create_usage_record, which Stripe retired for
new integrations.

Requires, at deploy time (see README.md):
- STRIPE_SECRET_KEY        (Secret Manager)
- STRIPE_WEBHOOK_SECRET    (Secret Manager; from the Stripe Dashboard webhook config)
- STRIPE_METERED_PRICE_ID  (Stripe Dashboard: a recurring Price with
                             usage_type=metered, attached to a Billing Meter)
- STRIPE_METER_EVENT_NAME  (the `event_name` configured on that Meter)

Firestore (via Cloud Run's default service account / ADC) stores only the
api_key -> Stripe customer_id mapping -- nothing about balances or pricing,
since Stripe is the source of truth for both.
"""

import os
import secrets
import uuid
from typing import Optional

import stripe

stripe.api_key = os.environ.get("STRIPE_SECRET_KEY")
_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET")
_METERED_PRICE_ID = os.environ.get("STRIPE_METERED_PRICE_ID")
_METER_EVENT_NAME = os.environ.get("STRIPE_METER_EVENT_NAME", "wcag_audit_call")

_db = None


def is_configured() -> bool:
    return bool(stripe.api_key and _WEBHOOK_SECRET and _METERED_PRICE_ID)


def _firestore():
    global _db
    if _db is None:
        from google.cloud import firestore

        _db = firestore.Client()
    return _db


def create_checkout_session(email: str, success_url: str, cancel_url: str) -> str:
    """Start a metered subscription -- no upfront charge; billed per audit
    call at the end of the billing period via Meter Events."""
    session = stripe.checkout.Session.create(
        mode="subscription",
        customer_email=email,
        line_items=[{"price": _METERED_PRICE_ID}],
        success_url=f"{success_url}?session_id={{CHECKOUT_SESSION_ID}}",
        cancel_url=cancel_url,
    )
    return session.url


def verify_webhook(payload: bytes, sig_header: Optional[str]) -> dict:
    return stripe.Webhook.construct_event(payload, sig_header, _WEBHOOK_SECRET)


def activate_customer(checkout_session: dict) -> str:
    """Mint an API key for a completed checkout and persist the mapping.

    Idempotent: re-delivering the same webhook event (Stripe retries on
    non-2xx responses) returns the existing key instead of minting a new one.
    """
    customer_id = checkout_session["customer"]
    db = _firestore()

    customer_ref = db.collection("customers").document(customer_id)
    existing = customer_ref.get()
    if existing.exists:
        return existing.to_dict()["api_key"]

    api_key = secrets.token_urlsafe(32)
    db.collection("api_keys").document(api_key).set({"customer_id": customer_id, "active": True})
    customer_ref.set({"api_key": api_key})
    return api_key


def api_key_for_session(session_id: str) -> Optional[str]:
    """Best-effort lookup for the checkout success page.

    Returns None while the webhook hasn't landed yet -- callers should treat
    that as "pending" and poll briefly, not as an error.
    """
    session = stripe.checkout.Session.retrieve(session_id)
    if session.status != "complete" or not session.customer:
        return None
    doc = _firestore().collection("customers").document(session.customer).get()
    if not doc.exists:
        return None
    return doc.to_dict().get("api_key")


def lookup_key(api_key: str) -> Optional[dict]:
    doc = _firestore().collection("api_keys").document(api_key).get()
    if not doc.exists or not doc.to_dict().get("active"):
        return None
    return doc.to_dict()


def record_usage(customer_id: str) -> None:
    """Bill exactly one audit call.

    Only call this after a real audit ran -- never for requests that errored
    out before producing a result. Uses a fresh idempotency identifier per
    call so a retried request can't double-bill.
    """
    stripe.billing.MeterEvent.create(
        event_name=_METER_EVENT_NAME,
        payload={"value": "1", "stripe_customer_id": customer_id},
        identifier=str(uuid.uuid4()),
    )
