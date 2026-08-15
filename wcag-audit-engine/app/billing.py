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

import logging
import os
import secrets
import uuid
from typing import Optional

import stripe

# .strip() because a secret piped in with `echo` carries a trailing newline,
# which is the single most common way a correct key arrives broken. Stripe
# rejects the whitespace-padded value, and without stripping here the padding
# would also defeat the prefix check below and silently pull every plan out of
# the manifest. Empty after stripping is None, not "", so a blank secret reads
# the same as an absent one.
stripe.api_key = (os.environ.get("STRIPE_SECRET_KEY") or "").strip() or None
_WEBHOOK_SECRET = (os.environ.get("STRIPE_WEBHOOK_SECRET") or "").strip() or None
_METERED_PRICE_ID = os.environ.get("STRIPE_METERED_PRICE_ID")
_METER_EVENT_NAME = os.environ.get("STRIPE_METER_EVENT_NAME", "wcag_audit_call")

# Stripe secret keys are sk_/rk_ prefixed -- live, test, or restricted.
# https://docs.stripe.com/keys
_STRIPE_KEY_PREFIXES = ("sk_", "rk_")


def stripe_key_looks_valid() -> bool:
    """Whether STRIPE_SECRET_KEY is shaped like a Stripe secret key at all.

    Truthiness is not enough to decide the Stripe rail is live. Any non-empty
    string made is_configured() return True, so a variable holding the wrong
    value -- an payout address, a price ID, a publishable pk_ key, a partly
    pasted secret -- would have had the manifest advertise all three plans
    and the stripe_api_key rail to every agent that asked, and sent buyers to
    a checkout that could not authenticate. Advertising a rail that cannot
    settle is the one thing this service must never do (it already omits x402
    rather than publish payTo:null), so a key that cannot possibly work has
    to count as no key.

    Shape-only, deliberately: it cannot tell a revoked or wrong-account key
    from a good one, and it does not call Stripe, because resolving this at
    import time would tie container startup to Stripe being reachable. It
    catches the class of failure that actually occurs -- the wrong value in
    the right variable -- not authorization.
    """
    key = stripe.api_key
    return bool(key) and key.startswith(_STRIPE_KEY_PREFIXES)


# RETIRED. The old single flat "Agency / Developer" subscription, replaced
# by the per-site plans below. Read only so a deployment still configured
# with it keeps working; nothing advertises it and no new checkout selects
# it. Delete once no live deployment sets it.
_FLAT_SUBSCRIPTION_PRICE_ID = os.environ.get("STRIPE_FLAT_SUBSCRIPTION_PRICE_ID")

# Fallback included-scans cap for a subscriber whose plan we don't know
# (legacy customers activated before the plan was recorded at checkout).
SAAS_MONTHLY_QUOTA = int(os.environ.get("SAAS_MONTHLY_QUOTA", "1500"))

# Per-plan monthly call caps.
#
# One global 1,500 cap for every subscriber was a plan-breaking bug. Agency
# is sold as "50 sites, audited daily": 50 bundle calls a day is 1,550 in a
# 31-day month, so the customer paying $249 got cut off before month end --
# and if they audited per-dimension rather than bundling (4 calls per site
# per day) they hit the wall around day 7 and started getting 402s on a plan
# they had already paid for. Pro and Agency also shared the same ceiling, so
# tripling the price bought no extra capacity at all.
#
# These are sized to the promise with real headroom, because the cap exists
# to stop runaway abuse, not to meter value: marginal cost is ~$0.00007 per
# audit, so even 10,000 audits is about $0.70 against $249 of revenue.
# Under-sizing this costs a customer; over-sizing it costs pennies.
PLAN_MONTHLY_QUOTA = {
    "pro": int(os.environ.get("QUOTA_PRO", "2000")),  # 5 sites x 4 checks x 31d = 620
    "agency": int(os.environ.get("QUOTA_AGENCY", "10000")),  # 50 x 4 x 31 = 6,200
}


def monthly_quota_for(plan: Optional[str]) -> int:
    return PLAN_MONTHLY_QUOTA.get(plan or "", SAAS_MONTHLY_QUOTA)


# Human-facing plans, priced per SITE MONITORED rather than per scan.
# Denominating a plan in scans invites the obvious arithmetic against the
# $0.03 machine rate; the old scan-denominated plan worked out dearer per
# scan than paying per call, so nobody rational would buy it. Sites are the
# unit a human actually cares about and aren't comparable to the machine
# rate, so the two audiences stop competing with each other.
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

# What each plan costs and covers, kept here beside the price IDs rather than
# retyped in the manifest and the landing page. The agent manifest went on
# advertising the retired subscription long after Stripe had stopped selling
# it, because the number lived in a second place nobody thought to update --
# a quoted price that no checkout will honour is worse than no price at all.
HUMAN_PLANS = [
    {
        "id": "report",
        "name": "Single report",
        "usd": 29.99,
        "interval": "once",
        "covers": "One site, all four checks, delivered as a shareable report page.",
    },
    {
        "id": "pro",
        "name": "Pro",
        "usd": 79.0,
        "interval": "month",
        "covers": "5 sites, audited daily across all four dimensions, with history.",
    },
    {
        "id": "agency",
        "name": "Agency",
        "usd": 249.0,
        "interval": "month",
        "covers": "50 sites, audited daily, with reports you can hand to clients.",
    },
]


def plan_available(plan: str) -> bool:
    return bool(stripe_key_looks_valid() and PLAN_PRICE_IDS.get(plan))


def oneoff_report_available() -> bool:
    return bool(stripe_key_looks_valid() and ONEOFF_REPORT_PRICE_ID)


def human_plans_live() -> list:
    """The plans this deployment can actually take money for.

    Same discipline as the payment-rail list: a plan whose Stripe Price ID
    isn't configured is omitted rather than advertised, so nothing in the
    manifest points at a checkout that would fail.
    """
    live = []
    for plan in HUMAN_PLANS:
        available = (
            oneoff_report_available()
            if plan["id"] == "report"
            else plan_available(plan["id"])
        )
        if available:
            live.append(plan)
    return live


_db = None


def _any_sellable_price() -> bool:
    """True if Stripe has at least one Price this service can charge.

    The current catalogue is the three per-site plans; the flat and metered
    IDs are the retired ones, kept here only so a deployment still running on
    them doesn't regress. Gating on the retired pair alone was a live bug: a
    node configured with today's plans and nothing else reported
    is_configured() == False, so /billing/checkout answered 501 while
    /.well-known/agent.json cheerfully advertised all three tiers.
    """
    return bool(
        _FLAT_SUBSCRIPTION_PRICE_ID
        or _METERED_PRICE_ID
        or ONEOFF_REPORT_PRICE_ID
        or any(PLAN_PRICE_IDS.values())
    )


def is_configured() -> bool:
    return bool(stripe_key_looks_valid() and _WEBHOOK_SECRET and _any_sellable_price())


def _firestore():
    global _db
    if _db is None:
        from google.cloud import firestore

        _db = firestore.Client()
    return _db


def create_checkout_session(
    email: str, success_url: str, cancel_url: str, plan: Optional[str] = None
) -> str:
    """Start a subscription for one of the named per-site plans.

    `plan` is what the landing page always sends and is the only supported
    way to buy. The no-plan fallback to the retired flat/metered price is
    kept solely for deployments still configured that way; where those IDs
    are unset it raises rather than reaching Stripe with price=None, which
    surfaced as an opaque 500 instead of telling the caller what to pick.
    """
    if plan:
        price_id = PLAN_PRICE_IDS.get(plan)
        if not price_id:
            raise ValueError(f"Plan {plan!r} is not configured on this deployment")
    else:
        price_id = _FLAT_SUBSCRIPTION_PRICE_ID or _METERED_PRICE_ID
        if not price_id:
            offered = sorted(p for p, pid in PLAN_PRICE_IDS.items() if pid)
            raise ValueError(
                "No plan specified and this deployment has no default price. "
                f"Pass one of: {', '.join(offered) or '(none configured)'}"
            )
    session = stripe.checkout.Session.create(
        mode="subscription",
        customer_email=email,
        line_items=[{"price": price_id}],
        # Which plan was bought, so activate_customer can record it against
        # the key and the monthly cap can match what the customer paid for.
        # Reading it back off the Price ID would mean an extra expanded
        # lookup on every webhook for something we already know here.
        metadata={"plan": plan} if plan else {},
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
    plan = (checkout_session.get("metadata") or {}).get("plan")
    db = _firestore()

    customer_ref = db.collection("customers").document(customer_id)
    existing = customer_ref.get()
    if existing.exists:
        return existing.to_dict()["api_key"]

    api_key = secrets.token_urlsafe(32)
    # `plan` rides on the key document so the auth path gets it from the
    # lookup it already does, rather than a second Firestore read per call.
    db.collection("api_keys").document(api_key).set(
        {"customer_id": customer_id, "active": True, "plan": plan}
    )
    customer_ref.set({"api_key": api_key, "plan": plan})
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


_key_store_warned = False


def _warn_key_store_unavailable(exc: Exception) -> None:
    """Say so, once, when the key store cannot be reached at all.

    Logged once rather than per request because this fires on every keyed
    call, and a paid endpoint under load would otherwise bury the logs.
    """
    global _key_store_warned
    if _key_store_warned:
        return
    _key_store_warned = True
    logging.getLogger(__name__).error(
        "API key lookup failed against Firestore (%s: %s). Every X-API-Key "
        "call will fall through to a 402 payment challenge until this is "
        "fixed -- subscribers cannot authenticate. If this is "
        "'The database (default) does not exist', no Firestore database has "
        "been created for this project; create one, because checkout also "
        "writes keys there.",
        type(exc).__name__,
        exc,
    )


def lookup_key(api_key: str) -> Optional[dict]:
    """The record behind an API key, or None if there isn't a usable one.

    Returns None rather than raising when the key store itself is
    unreachable. That distinction matters more than it looks: an
    unhandled exception here becomes an HTTP 500, and a 500 tells a machine
    caller nothing it can act on -- it cannot pay, cannot retry usefully, and
    cannot tell a broken backend from a rejected key. Returning None lets the
    caller fall through to x402/MPP, which do not touch Firestore at all, and
    answer with the 402 challenge the caller can actually act on.

    This is still fail-closed: an unreachable key store grants nobody access,
    it only changes an unactionable 500 into an actionable 402. The cost is
    that a genuine subscriber is asked to pay per call while the outage
    lasts, which is why it is logged at ERROR rather than swallowed.
    """
    try:
        doc = _firestore().collection("api_keys").document(api_key).get()
    except Exception as exc:
        _warn_key_store_unavailable(exc)
        return None
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

    A higher-priced route (the $0.10 /audit/bundle) reports `units` separate
    events rather than requiring a second Stripe meter and price just for
    it.

    What these events are actually worth is NOT $0.03 each. The metered
    Price on the account is $0.01 per unit, so a single audit meters $0.01
    and a bundle $0.03 -- nowhere near the $0.03/$0.10 those routes charge.
    That does not currently mis-bill anyone, because the human plans are
    `licensed` flat prices with no metered item on the subscription, so
    these events are recorded and never charged. It would start mis-billing
    the moment that metered Price is attached to a subscription, so if
    metered billing is ever revived, fix the Price first: reconcile it with
    the real per-call rate rather than trusting this call count.
    """
    for _ in range(max(1, units)):
        stripe.billing.MeterEvent.create(
            event_name=_METER_EVENT_NAME,
            payload={"value": "1", "stripe_customer_id": customer_id},
            identifier=str(uuid.uuid4()),
        )


def check_and_increment_quota(customer_id: str, plan: Optional[str] = None) -> bool:
    """Returns True and increments the counter if this call is within the
    subscription's included monthly quota; returns False if the customer
    has already used their included scans for this calendar month.

    The cap comes from the plan the customer actually bought
    (monthly_quota_for). A subscriber activated before plans were recorded
    has no plan on their key and falls back to SAAS_MONTHLY_QUOTA.

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
            if count >= monthly_quota_for(plan):
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
