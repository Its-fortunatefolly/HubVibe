"""Stripe usage-based billing for the privacy/cookie compliance scanner.

Same pattern as wcag-audit-engine/app/billing.py (see that file for the
full rationale): Stripe owns the balance and invoicing via Meter Events,
this module only stores a thin api_key -> customer_id mapping. Firestore
collections are prefixed `privacy_` so this service's customers/leads never
collide with wcag-audit-engine's, even if both share a GCP project.

Requires, at deploy time:
- STRIPE_SECRET_KEY, STRIPE_WEBHOOK_SECRET      (Secret Manager)
- PRIVACY_STRIPE_METERED_PRICE_ID               (Stripe Dashboard)
- PRIVACY_STRIPE_METER_EVENT_NAME               (Stripe Dashboard)
"""

import os
import secrets
import time
import uuid
from typing import Optional

import stripe

stripe.api_key = os.environ.get("STRIPE_SECRET_KEY")
_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET")
_METERED_PRICE_ID = os.environ.get("PRIVACY_STRIPE_METERED_PRICE_ID")
_METER_EVENT_NAME = os.environ.get("PRIVACY_STRIPE_METER_EVENT_NAME", "privacy_scan_call")

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
    customer_id = checkout_session["customer"]
    db = _firestore()

    customer_ref = db.collection("privacy_customers").document(customer_id)
    existing = customer_ref.get()
    if existing.exists:
        return existing.to_dict()["api_key"]

    api_key = secrets.token_urlsafe(32)
    db.collection("privacy_api_keys").document(api_key).set(
        {"customer_id": customer_id, "active": True}
    )
    customer_ref.set({"api_key": api_key})
    return api_key


def api_key_for_session(session_id: str) -> Optional[str]:
    session = stripe.checkout.Session.retrieve(session_id)
    if session.status != "complete" or not session.customer:
        return None
    doc = _firestore().collection("privacy_customers").document(session.customer).get()
    if not doc.exists:
        return None
    return doc.to_dict().get("api_key")


def lookup_key(api_key: str) -> Optional[dict]:
    doc = _firestore().collection("privacy_api_keys").document(api_key).get()
    if not doc.exists or not doc.to_dict().get("active"):
        return None
    return doc.to_dict()


def save_lead(url: str, email: Optional[str], finding_count: int) -> None:
    db = _firestore()
    db.collection("privacy_leads").add(
        {"url": url, "email": email, "finding_count": finding_count, "created_at": time.time()}
    )


def record_usage(customer_id: str) -> None:
    stripe.billing.MeterEvent.create(
        event_name=_METER_EVENT_NAME,
        payload={"value": "1", "stripe_customer_id": customer_id},
        identifier=str(uuid.uuid4()),
    )
