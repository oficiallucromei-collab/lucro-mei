"""One-shot jobs for the existing deployment. Run via Cloud Run Jobs or cron."""

import argparse
import json
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select, update

from lucromei.analysis_assistant import suggested_experiment
from lucromei.config import get_settings
from lucromei.db import SessionLocal
from lucromei.growth import BRAZIL, access, record, utc
from lucromei.models import (
    Acquisition,
    AcquisitionClick,
    Business,
    CommandReceipt,
    GrowthEvent,
    GrowthRateBucket,
    InboundMessage,
    TrialHold,
)
from lucromei.reporting import build_report, default_period, recommendations
from lucromei.whatsapp import CloudApiOutboundSender, WhatsAppSendError


def prune_message_text(session, now):
    session.execute(
        update(InboundMessage)
        .where(
            InboundMessage.received_at <= now - timedelta(days=30),
            InboundMessage.raw_text != "",
        )
        .values(raw_text="")
    )


def eligible_message(profile, events, now):
    if not profile.opt_in_at or not 9 <= now.astimezone(BRAZIL).hour < 19:
        return None
    if any(
        name.startswith("lifecycle:") and utc(when) > now - timedelta(days=1)
        for name, when in events.items()
    ):
        return None
    state = access(profile, now)
    if state == "paid" and "paid_conversion" in events:
        if now - utc(events["paid_conversion"]) < timedelta(days=3):
            if "lifecycle:subscription_confirmation" not in events:
                return "subscription_confirmation", "subscription_confirmation"
        weeks = (now - utc(events["paid_conversion"])).days // 7
        if weeks and f"lifecycle:weekly_summary:{weeks}" not in events:
            return "weekly_summary", f"weekly_summary:{weeks}"
    if state != "trial" or not profile.trial_start:
        return None
    start = utc(profile.trial_start)
    end = utc(profile.trial_end)
    if end - now <= timedelta(days=2):
        if "lifecycle:trial_expiring" not in events:
            return "trial_expiring", "trial_expiring"
    elif now >= start + (end - start) / 2:
        if "summary_requested" not in events and "lifecycle:trial_midpoint" not in events:
            return "trial_midpoint", "trial_midpoint"
    elif now >= start + timedelta(hours=12):
        if "first_transaction" not in events and "lifecycle:activation_help" not in events:
            return "activation_help", "activation_help"
    return None


def run_lifecycle(session, settings, now, sender):
    if not settings.growth_lifecycle_enabled:
        return 0
    sent = 0
    failed = 0
    candidates = session.scalars(
        select(Acquisition.business_id).where(Acquisition.opt_in_at.is_not(None))
    ).all()
    for business_id in candidates:
        business = session.scalar(
            select(Business).where(Business.id == business_id).with_for_update()
        )
        profile = session.get(Acquisition, business_id, populate_existing=True)
        events = dict(
            session.execute(
                select(GrowthEvent.name, GrowthEvent.happened_at).where(
                    GrowthEvent.business_id == business_id
                )
            ).all()
        )
        choice = eligible_message(profile, events, now) if business and profile else None
        template = settings.growth_lifecycle_templates.get(choice[0]) if choice else None
        if not template:
            session.rollback()
            continue
        key = choice[1]
        # Serialize opt-outs and concurrent jobs with the send. An ambiguous network
        # failure is recorded and surfaced without a retry for this occurrence.
        if record(session, business_id, f"lifecycle:{key}", now):
            try:
                sender.send_template(business.whatsapp_number, template)
                sent += 1
            except WhatsAppSendError:
                record(session, business_id, f"lifecycle_failed:{key}", now)
                failed += 1
        session.commit()
    if failed:
        raise RuntimeError(f"Lifecycle send failures: {failed}")
    return sent


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("job", choices=["weekly-report", "budget-check", "lifecycle", "prune"])
    args = parser.parse_args()
    settings = get_settings()
    if not settings.growth_enabled:
        raise SystemExit("Growth is disabled")
    now = datetime.now(UTC)
    with SessionLocal() as session:
        if args.job == "prune":
            prune_message_text(session, now)
            session.execute(
                delete(AcquisitionClick).where(
                    AcquisitionClick.created_at < now - timedelta(hours=24)
                )
            )
            session.execute(
                delete(GrowthRateBucket).where(
                    GrowthRateBucket.minute < int(now.timestamp()) // 60 - 60
                )
            )
            session.execute(delete(TrialHold).where(TrialHold.expires_at <= now))
            session.execute(
                update(CommandReceipt)
                .where(
                    CommandReceipt.command_type == "growth",
                    CommandReceipt.created_at <= now - timedelta(days=2),
                    CommandReceipt.response_text != "",
                )
                .values(response_text="")
            )
            session.execute(
                delete(GrowthEvent).where(
                    GrowthEvent.name.startswith("reply:"),
                    GrowthEvent.happened_at <= now - timedelta(days=30),
                )
            )
            session.commit()
        elif args.job == "budget-check":
            start = settings.growth_experiment_start
            if not start:
                raise SystemExit("Configure LUCROMEI_GROWTH_EXPERIMENT_START")
            end = start + timedelta(days=settings.growth_campaign_days - 1)
            report = build_report(session, settings, start, end, now)
            print(json.dumps({"budget": report["experiment"]}))
        elif args.job == "weekly-report":
            report = build_report(session, settings, *default_period(now), now)
            metrics = report["measured"]
            keys = (
                "landing_page_requests",
                "trial_signups",
                "activated",
                "paid_conversion",
                "spend_cents",
                "mrr_now_cents",
            )
            labels = (
                (
                    "Visitas",
                    "Avaliações",
                    "Ativados",
                    "Pagantes",
                    "Gasto (centavos)",
                    "MRR atual (centavos)",
                )
                if settings.growth_report_language == "pt"
                else (
                    "Visits",
                    "Trials",
                    "Activated",
                    "Conversions",
                    "Spend (cents)",
                    "MRR (cents)",
                )
            )
            print(
                json.dumps(
                    {
                        "period": report["period"],
                        "metrics": dict(zip(labels, (metrics[key] for key in keys), strict=True)),
                        "recommendations": recommendations(report, settings.growth_report_language),
                        "ai_suggested_experiment": suggested_experiment(settings, report),
                        "budget": report["experiment"],
                    },
                    ensure_ascii=False,
                )
            )
        elif settings.growth_lifecycle_enabled:
            if not settings.whatsapp_access_token or not settings.whatsapp_phone_number_id:
                raise SystemExit("Configure the WhatsApp sender")
            sender = CloudApiOutboundSender(
                settings.whatsapp_access_token,
                settings.whatsapp_phone_number_id,
                settings.whatsapp_graph_api_version,
            )
            print(json.dumps({"messages_sent": run_lifecycle(session, settings, now, sender)}))


if __name__ == "__main__":
    main()
