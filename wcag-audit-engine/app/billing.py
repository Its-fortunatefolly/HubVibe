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

# The human "Agency / Developer" plan: a flat recurring Price (NOT
# usage_type=metered) -- e.g. $49/month, created separately in the Stripe
# Dashboard. If this isn't set, checkout falls back to the pure metered
# price above so nothing regresses -- but the landing page's "$49/month,
# 1,500 scans included" framing is only actually true once this is set.
_FLAT_SUBSCRIPTION_PRICE_ID = os.environ.get("STRIPE_FLAT_SUBSCRIPTION_PRICE_ID")

# Included scans per calendar month on the subscription plan before a
# caller falls back to paying per-call via x402/MPP instead of the API key
# alone. The meter above still reports every call regardless -- this is a
# separate, additive limit on top of metered billing, not a replacement
# for it (Stripe is still the source of truth for what a subscriber owes).
SAAS_MONTHLY_QUOTA = int(os.environ.get("SAAS_MONTHLY_QUOTA", "1500"))

# Human-facing plans, priced per SITE MONITORED rather than per scan.
# Denominating in scans invited the obvious arithmetic -- $49 for 1,500 scans
# is $0.033 each, more than the $0.03 machine rate, so the plan was strictly
# worse than just paying per call and nobody rational would buy it. Sites are
# the unit a human actually cares about, and it isn't comparable to the
# machine rate, so the two audiences stop competing with each other.
#
# Each is a Stripe Price you create in the Dashboard; a tier with no price ID
# configured is simply not offered rather than half-working.
PLAN_PRICE_IDS = {
    "pro": os.environ.get("STRIPE_PRICE_PRO"),
    "agency": os.environ.get("STRIPE_PRICE_AGENCY"),
}

# One-time purchase (mode="payment", not a subscription): a single full
# bundle report on one URL, for the visitor who will never subscribe. Pure
# margin and it captures traffic that would otherwise bounce.
ONEOFF_REPORT_PRICE_ID = os.environ.get("STRIPE_PRICE_ONEOFF_REPORT")


def plan_available(plan: str) -> bool:
    return bool(stripe.api_key and PLAN_PRICE_IDS.get(plan))


def oneoff_report_available() -> bool:
    return bool(stripe.api_key and ONEOFF_REPORT_PRICE_ID)


_db = None


def is_configured() -> bool:
    return bool(stripe.api_key and _WEBHOOK_SECRET and (_FLAT_SUBSCRIPTION_PRICE_ID or _METERED_PRICE_ID))


def _firestore():
    global _db
    if _db is None:
        from google.cloud import firestore

        _db = firestore.Client()
    return _db


def create_checkout_session(
    email: str, success_url: str, cancel_url: str, plan: Optional[str] = None
) -> str:
    """Start the subscription: the flat "Agency / Developer" price
    (STRIPE_FLAT_SUBSCRIPTION_PRICE_ID) if configured, else the older pure
    metered price -- so this keeps working even before the flat price is
    set up. Either way, every real audit call still reports a Meter Event
    (see billing.record_usage); the flat price just adds a base monthly
    charge and an included-scans quota (see check_and_increment_quota) on
    top of that.
    """
    price_id = _FLAT_SUBSCRIPTION_PRICE_ID or _METERED_PRICE_ID
    if plan:
        tier_price = PLAN_PRICE_IDS.get(plan)
        if not tier_price:
            raise ValueError(f"Plan {plan!r} is not configured on this deployment")
        price_id = tier_price
    session = stripe.checkout.Session.create(
        mode="subscription",
        customer_email=email,
        line_items=[{"price": price_id}],
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


def save_lead(url: str, email: Optional[str], violation_count: int) -> None:
    """Record a free-scan lead for manual follow-up.

    Best-effort: callers should catch failures rather than let a storage
    hiccup break the free scan response for the visitor. This only stores
    what the visitor themself submitted through the scan form -- it's not
    used to look up or contact anyone who didn't submit their own site/email.
    """
    import time

    db = _firestore()
    db.collection("leads").add(
        {
            "url": url,
            "email": email,
            "violation_count": violation_count,
            "created_at": time.time(),
        }
    )


def record_usage(customer_id: str, units: int = 1) -> None:
    """Bill a completed audit call.

    Only call this after a real audit ran -- never for requests that errored
    out before producing a result. Uses a fresh idempotency identifier per
    call so a retried request can't double-bill.

    The underlying meter is a flat per-event ($0.03) count, so a
    higher-priced route (e.g. the $0.10 /audit/bundle) reports `units`
    separate events to approximate its price against that same meter,
    rather than requiring a second Stripe meter/price to be provisioned
    just for this. This is an approximation (3 units ~= $0.09, not exactly
    $0.10) -- a dedicated bundle price would make it exact, and can be
    added later without changing this function's signature.
    """
    for _ in range(max(1, units)):
        stripe.billing.MeterEvent.create(
            event_name=_METER_EVENT_NAME,
            payload={"value": "1", "stripe_customer_id": customer_id},
            identifier=str(uuid.uuid4()),
        )


def check_and_increment_quota(customer_id: str) -> bool:
    """Returns True and increments the counter if this call is within the
    subscription's included monthly quota; returns False if the customer
    has already used their included scans for this calendar month.

    Callers should treat False as "the API key alone is no longer
    sufficient" and require x402/MPP payment for this specific call,
    matching the SaaS plan's advertised overage behavior -- Stripe still
    bills every call via the meter either way, this only gates whether the
    bare API key is enough on its own.

    This is a business limit, not a security boundary, so unlike
    authentication elsewhere in this codebase it fails OPEN on error
    (treats the call as within quota) rather than closed: a transient
    Firestore hiccup should not cut off a paying, already-authenticated
    subscriber, and the worst case of failing open here is a small amount
    of temporary under-enforcement, not unauthorized access.
    """
    import datetime

    from google.cloud import firestore

    try:
        period = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m")
        db = _firestore()
        ref = db.collection("quota_usage").document(f"{customer_id}:{period}")

        @firestore.transactional
        def _increment(transaction):
            snapshot = ref.get(transaction=transaction)
            count = snapshot.get("count") if snapshot.exists else 0
            if count >= SAAS_MONTHLY_QUOTA:
                return False
            transaction.set(
                ref,
                {"count": count + 1, "period": period, "customer_id": customer_id},
                merge=True,
            )
            return True

        return _increment(db.transaction())
    except Exception:
        return True


def create_report_checkout(email: str, url: str, success_url: str, cancel_url: str) -> str:
    """One-time Checkout for a single full-bundle report on one URL.

    mode="payment", not "subscription": this buyer is explicitly not
    subscribing. The audited URL rides along in session metadata so the
    report can be produced after payment without asking for it twice.
    """
    if not ONEOFF_REPORT_PRICE_ID:
        raise ValueError("One-off reports are not configured on this deployment")
    session = stripe.checkout.Session.create(
        mode="payment",
        customer_email=email,
        line_items=[{"price": ONEOFF_REPORT_PRICE_ID, "quantity": 1}],
        metadata={"audit_url": url, "kind": "oneoff_report"},
        success_url=f"{success_url}?session_id={{CHECKOUT_SESSION_ID}}",
        cancel_url=cancel_url,
    )
    return session.url


def paid_report_request(session_id: str) -> Optional[dict]:
    """Return {"url": ...} if this session is a genuinely PAID one-off report.

    Verified against Stripe on every call rather than trusting a webhook
    having landed, so a report can never be produced for an unpaid session --
    the report URL is the only thing standing between a stranger and a free
    audit. Returns None for anything unpaid, unknown, or not a report order.
    """
    try:
        session = stripe.checkout.Session.retrieve(session_id)
    except Exception:
        return None
    if session.get("payment_status") != "paid":
        return None
    metadata = session.get("metadata") or {}
    if metadata.get("kind") != "oneoff_report":
        return None
    url = metadata.get("audit_url")
    return {"url": url} if url else None


def load_report(session_id: str) -> Optional[dict]:
    """Previously generated report, if any -- so a refresh doesn't re-run
    (and re-pay for) an audit the buyer already purchased."""
    try:
        doc = _firestore().collection("reports").document(session_id).get()
    except Exception:
        return None
    return doc.to_dict() if doc.exists else None


def save_report(session_id: str, url: str, result: dict) -> None:
    import time

    try:
        _firestore().collection("reports").document(session_id).set(
            {"url": url, "result": result, "created_at": time.time()}
        )
    except Exception:
        # Storage is a convenience for re-viewing. The buyer already has
        # their report rendered in the response; losing the cache must not
        # fail the purchase they just completed.
        pass
