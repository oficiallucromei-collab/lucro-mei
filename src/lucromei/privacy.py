"""Founder fulfillment of identity-verified WhatsApp privacy requests."""

from datetime import timedelta

from sqlalchemy import delete, select

from lucromei.billing import configured, stripe
from lucromei.growth import insert_for, trial_identity
from lucromei.models import (
    Acquisition,
    Business,
    CommandReceipt,
    GrowthAudit,
    GrowthEvent,
    InboundMessage,
    Transaction,
    TransactionEvent,
    TrialHold,
)


class PrivacyConflict(ValueError):
    pass


def dataset(session, model, criterion, *, exclude=()):
    return [
        {
            column.name: getattr(row, column.name)
            for column in model.__table__.columns
            if column.name not in exclude
        }
        for row in session.scalars(select(model).where(criterion))
    ]


def fulfill(session, settings, business_id, action, now):
    business = session.scalar(select(Business).where(Business.id == business_id).with_for_update())
    if not business or not session.scalar(
        select(GrowthEvent.name)
        .where(
            GrowthEvent.business_id == business_id,
            GrowthEvent.name.startswith(f"requested_{action}:", autoescape=True),
        )
        .limit(1)
    ):
        raise LookupError("Verified request not found")

    transaction_ids = select(Transaction.id).where(Transaction.business_id == business_id)
    profile = session.get(Acquisition, business_id)
    if action == "export":
        session.add(GrowthAudit(action="privacy_export_prepared"))
        return {
            "account": dataset(session, Business, Business.id == business_id),
            "acquisition": dataset(
                session,
                Acquisition,
                Acquisition.business_id == business_id,
                exclude=("stripe_customer", "checkout_id", "subscription_id"),
            ),
            "transactions": dataset(session, Transaction, Transaction.business_id == business_id),
            "transaction_history": dataset(
                session, TransactionEvent, TransactionEvent.transaction_id.in_(transaction_ids)
            ),
            "growth_events": dataset(session, GrowthEvent, GrowthEvent.business_id == business_id),
            "ledger_messages": dataset(
                session, InboundMessage, InboundMessage.business_id == business_id
            ),
        }

    key = settings.growth_trial_hold_key
    if len(key) < 32:
        raise PrivacyConflict("Configure the trial hold key before deletion")
    if profile.subscription_status and profile.subscription_status not in {
        "canceled",
        "incomplete_expired",
    }:
        raise PrivacyConflict("Resolve the subscription in Stripe first")
    if profile.checkout_id:
        if not configured(settings):
            raise PrivacyConflict("Verify the checkout with the billing provider first")
        checkout = stripe(settings, f"checkout/sessions/{profile.checkout_id}")
        if checkout["status"] == "open" or (
            checkout["status"] == "complete" and checkout["subscription"] != profile.subscription_id
        ):
            raise PrivacyConflict("Resolve the checkout in Stripe first")
    hold = {
        "identity_digest": trial_identity(business.whatsapp_number, key),
        "expires_at": now + timedelta(days=settings.growth_trial_hold_days),
    }
    session.execute(
        insert_for(session, TrialHold)
        .values(**hold)
        .on_conflict_do_update(
            index_elements=["identity_digest"],
            set_={"expires_at": hold["expires_at"]},
        )
    )
    session.execute(
        delete(TransactionEvent).where(TransactionEvent.transaction_id.in_(transaction_ids))
    )
    for table in (Transaction, InboundMessage, CommandReceipt, GrowthEvent, Acquisition):
        session.execute(delete(table).where(table.business_id == business_id))
    session.delete(business)
    session.add(GrowthAudit(action="privacy_account_deleted"))
    return {"deleted": True}
