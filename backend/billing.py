"""Stripe Checkout for the $5/mo Pro plan, plus the webhook that activates it.

The webhook is the only thing that grants Pro. A user returning to the success
URL proves nothing — they could just visit it — so the redirect only shows a
"finishing up" message while Stripe's signed event does the actual upgrade.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import stripe

import db
from config import (
    PUBLIC_BASE_URL,
    STRIPE_PRICE_ID,
    STRIPE_SECRET_KEY,
    STRIPE_WEBHOOK_SECRET,
    billing_enabled,
)


class BillingError(RuntimeError):
    pass


def _client() -> stripe.StripeClient:
    if not billing_enabled():
        raise BillingError(
            "Billing is not configured — set STRIPE_SECRET_KEY and STRIPE_PRICE_ID in .env."
        )
    return stripe.StripeClient(STRIPE_SECRET_KEY)


def _ts(epoch: object) -> str | None:
    if not isinstance(epoch, (int, float)):
        return None
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


def create_checkout_session(user: sqlite3.Row) -> str:
    """Return a Stripe-hosted Checkout URL for this user's subscription."""
    client = _client()
    customer_id = user["stripe_customer_id"]

    if not customer_id:
        customer = client.customers.create(
            params={"email": user["email"], "metadata": {"user_id": str(user["id"])}}
        )
        customer_id = customer.id
        db.set_stripe_customer(user["id"], customer_id)

    session = client.checkout.sessions.create(
        params={
            "mode": "subscription",
            "customer": customer_id,
            "line_items": [{"price": STRIPE_PRICE_ID, "quantity": 1}],
            "success_url": f"{PUBLIC_BASE_URL}/app?checkout=success",
            "cancel_url": f"{PUBLIC_BASE_URL}/app?checkout=cancelled",
            "client_reference_id": str(user["id"]),
            "metadata": {"user_id": str(user["id"])},
        }
    )
    if not session.url:
        raise BillingError("Stripe did not return a Checkout URL.")
    return session.url


def create_portal_session(user: sqlite3.Row) -> str:
    """Stripe's hosted billing portal, so users can cancel without emailing you."""
    if not user["stripe_customer_id"]:
        raise BillingError("No subscription on file for this account.")
    session = _client().billing_portal.sessions.create(
        params={"customer": user["stripe_customer_id"], "return_url": f"{PUBLIC_BASE_URL}/app"}
    )
    return session.url


def verify_event(payload: bytes, signature: str | None) -> stripe.Event:
    if not STRIPE_WEBHOOK_SECRET:
        raise BillingError("STRIPE_WEBHOOK_SECRET is not set — refusing to trust this webhook.")
    if not signature:
        raise BillingError("Missing Stripe-Signature header.")
    try:
        return stripe.Webhook.construct_event(payload, signature, STRIPE_WEBHOOK_SECRET)
    except (ValueError, stripe.SignatureVerificationError) as exc:
        raise BillingError(f"Invalid webhook signature: {exc}") from exc


# Events that mean "this customer should (or should no longer) have Pro".
ACTIVATING = {"checkout.session.completed", "customer.subscription.created"}
UPDATING = {"customer.subscription.updated"}
DEACTIVATING = {"customer.subscription.deleted"}
LIVE_STATUSES = {"active", "trialing"}


def apply_event(event: stripe.Event) -> str:
    """Translate a verified Stripe event into a plan change. Returns a log line."""
    # event.data.object is a StripeObject, not a dict — convert before reading.
    raw = event.data.object
    obj = raw.to_dict() if hasattr(raw, "to_dict") else dict(raw)
    kind = event.type
    customer_id = obj.get("customer")

    if not isinstance(customer_id, str):
        return f"{kind}: no customer id, ignored"

    if kind in ACTIVATING:
        if kind == "checkout.session.completed" and obj.get("mode") != "subscription":
            return f"{kind}: not a subscription checkout, ignored"
        ok = db.set_plan_by_customer(customer_id, "pro", _ts(obj.get("current_period_end")))
        return f"{kind}: {'upgraded' if ok else 'no matching user for'} {customer_id}"

    if kind in UPDATING:
        status = obj.get("status")
        plan = "pro" if status in LIVE_STATUSES and not obj.get("cancel_at_period_end") else "free"
        # A cancel-at-period-end subscription keeps Pro until the period actually ends.
        if obj.get("cancel_at_period_end") and status in LIVE_STATUSES:
            plan = "pro"
        db.set_plan_by_customer(customer_id, plan, _ts(obj.get("current_period_end")))
        return f"{kind}: {customer_id} -> {plan} (status={status})"

    if kind in DEACTIVATING:
        db.set_plan_by_customer(customer_id, "free", None)
        return f"{kind}: {customer_id} -> free"

    return f"{kind}: ignored"
