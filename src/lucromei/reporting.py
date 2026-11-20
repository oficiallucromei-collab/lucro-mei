"""Deterministic reporting. All monetary units are BRL centavos."""

from collections import defaultdict
from datetime import UTC, date, datetime, time, timedelta

from sqlalchemy import select

from lucromei.growth import BRAZIL, access, utc
from lucromei.models import Acquisition, CampaignDay, GrowthEvent, LandingDay


def ratio(numerator, denominator):
    return numerator / denominator if denominator else None


def build_report(session, settings, start, end, now=None):
    now = now or datetime.now(UTC)
    beginning = datetime.combine(start, time.min, BRAZIL)
    ending = datetime.combine(end + timedelta(days=1), time.min, BRAZIL)
    profiles = session.scalars(select(Acquisition)).all()
    events = defaultdict(dict)
    for event in session.scalars(select(GrowthEvent).where(GrowthEvent.happened_at < now)):
        events[event.business_id][event.name] = event
    cohort = [p for p in profiles if p.trial_start and beginning <= utc(p.trial_start) < ending]
    imported = session.scalars(
        select(CampaignDay).where(CampaignDay.day.between(start.isoformat(), end.isoformat()))
    ).all()
    days = (end - start).days + 1
    complete = len({row.day for row in imported}) == days
    spend = sum(r.spend_cents for r in imported) if complete else None
    visits = sum(
        session.scalars(
            select(LandingDay.visits).where(
                LandingDay.day.between(start.isoformat(), end.isoformat())
            )
        )
    )
    paid = [p for p in profiles if access(p, now) == "paid"]
    mrr = sum(p.recurring_cents for p in paid)
    cancellations = revenue = new_mrr = lost_mrr = 0
    revenue_by_source = defaultdict(int)
    for profile in profiles:
        for name, event in events[profile.business_id].items():
            if beginning <= utc(event.happened_at) < ending:
                if name.startswith("canceled:"):
                    cancellations += 1
                    lost_mrr += event.amount_cents
                elif name.startswith("mrr_new:"):
                    new_mrr += event.amount_cents
                elif name.startswith("paid_invoice:"):
                    revenue += event.amount_cents
                    revenue_by_source[profile.attribution.get("utm_source", "unknown")] += (
                        event.amount_cents
                    )

    def funnel(group, group_spend):
        count = len(group)
        metrics = {
            event: sum(event in events[p.business_id] for p in group)
            for event in (
                "conversation_started",
                "onboarding_started",
                "onboarding_completed",
                "first_transaction",
                "third_transaction",
                "summary_requested",
                "referral_reward_invited",
                "activated",
                "paid_conversion",
            )
        }
        metrics["trial_signups"] = count
        for metric, step in [
            ("onboarding_completion_rate", "onboarding_completed"),
            ("activation_rate", "activated"),
            ("trial_to_paid_rate", "paid_conversion"),
        ]:
            metrics[metric] = ratio(metrics[step], count)
        for metric, step in [
            ("cost_per_conversation_cents", "conversation_started"),
            ("cost_per_trial_cents", "trial_signups"),
            ("cost_per_activated_cents", "activated"),
            ("cac_cents", "paid_conversion"),
        ]:
            metrics[metric] = ratio(group_spend, metrics[step]) if group_spend is not None else None
        metrics["spend_cents"] = group_spend
        return metrics

    metrics = funnel(cohort, spend)
    campaigns = {}
    identifiers = {row.campaign for row in imported} | {
        p.attribution[key]
        for p in cohort
        for key in ("campaign_id", "utm_campaign", "meta_source_id")
        if key in p.attribution
    }
    for identifier in sorted(identifiers):
        group = [
            p
            for p in cohort
            if (
                p.attribution.get("campaign_id")
                or p.attribution.get("utm_campaign")
                or p.attribution.get("meta_source_id")
            )
            == identifier
        ]
        rows = [r for r in imported if r.campaign == identifier]
        if not group and not rows:
            continue
        campaigns[identifier] = funnel(
            group, sum(r.spend_cents for r in rows) if complete else None
        ) | {
            "impressions_imported": sum(r.impressions for r in rows),
            "clicks_imported": sum(r.clicks for r in rows),
        }

    retention = {}
    for day in (7, 14, 30):
        mature = [p for p in cohort if utc(p.trial_start) + timedelta(days=day + 1) <= now]
        returned = sum(f"returned_day_{day}" in events[p.business_id] for p in mature)
        retention[f"day_{day}"] = {
            "eligible": len(mature),
            "returned": returned,
            "rate": ratio(returned, len(mature)),
        }
    subscriptions_at_start = set()
    canceled_in_period = set()
    for business_id, history in events.items():
        for name, event in history.items():
            if name.startswith("mrr_new:") and utc(event.happened_at) < beginning:
                subscriptions_at_start.add((business_id, name.split(":", 1)[1]))
            elif name.startswith("canceled:") and beginning <= utc(event.happened_at) < ending:
                canceled_in_period.add((business_id, name.split(":", 1)[1]))
        subscriptions_at_start.difference_update(
            (business_id, name.split(":", 1)[1])
            for name, event in history.items()
            if name.startswith("canceled:") and utc(event.happened_at) < beginning
        )
    paid_cohort_churn = ratio(
        len(subscriptions_at_start & canceled_in_period), len(subscriptions_at_start)
    )
    referral_cohort = [p for p in cohort if p.attribution.get("referral_id")]
    margin = (
        settings.growth_price_cents - settings.growth_variable_cost_cents
        if settings.growth_variable_cost_cents is not None
        else None
    )
    estimate = {
        "monthly_contribution_margin_cents": margin,
        "cac_payback_months": ratio(metrics["cac_cents"], margin)
        if margin and margin > 0 and metrics["cac_cents"] is not None
        else None,
        "ltv_cents": ratio(margin, paid_cohort_churn)
        if margin and margin > 0 and days == 30 and len(subscriptions_at_start) >= 20
        else None,
    }
    alert = None
    experiment_start = settings.growth_experiment_start
    experiment_end = (
        experiment_start + timedelta(days=settings.growth_campaign_days - 1)
        if experiment_start
        else None
    )
    budget_rows = (
        session.scalars(
            select(CampaignDay).where(
                CampaignDay.day.between(experiment_start.isoformat(), experiment_end.isoformat())
            )
        ).all()
        if experiment_start
        else imported
    )
    budget_spend = sum(row.spend_cents for row in budget_rows) if budget_rows else None
    if (
        budget_spend is not None
        and budget_spend * 100
        >= settings.growth_budget_cents * settings.growth_budget_alert_percent
    ):
        alert = (
            "budget_exhausted" if budget_spend >= settings.growth_budget_cents else "budget_warning"
        )
    return {
        "period": {"start": start.isoformat(), "end": end.isoformat()},
        "measured": metrics
        | {
            "landing_page_requests": visits,
            "spend_days_complete": complete,
            "imported_spend_cents": sum(r.spend_cents for r in imported) if imported else None,
            "paying_now": len(paid),
            "mrr_now_cents": mrr,
            "new_mrr_cents": new_mrr,
            "lost_mrr_from_cancellation_cents": lost_mrr,
            "revenue_cents": revenue,
            "revenue_by_source_cents": dict(revenue_by_source),
            "revenue_less_ads_cents": revenue - spend if spend is not None else None,
            "cancellations": cancellations,
            "trial_retention": retention,
            "subscriber_cancellation_rate": paid_cohort_churn,
            "subscriber_retention_rate": (
                1 - paid_cohort_churn if paid_cohort_churn is not None else None
            ),
        },
        "estimated": estimate,
        "campaigns": campaigns,
        "referrals": funnel(referral_cohort, None),
        "experiment": {
            "budget_cents": settings.growth_budget_cents,
            "imported_spend_cents": budget_spend,
            "start": experiment_start.isoformat() if experiment_start else start.isoformat(),
            "end": experiment_end.isoformat() if experiment_end else end.isoformat(),
            "days": settings.growth_campaign_days,
            "target_activated": settings.growth_target_activated,
            "target_paid": settings.growth_target_paid,
            "alert": alert,
        },
    }


def default_period(now):
    end = now.astimezone(BRAZIL).date() - timedelta(days=1)
    return end - timedelta(days=6), end


def recommendations(report, language):
    pt = language == "pt"
    messages = []
    m = report["measured"]
    if report["experiment"]["alert"]:
        messages.append(
            "Revise o orçamento no Gerenciador de Anúncios."
            if pt
            else "Review the budget in Ads Manager."
        )
    if m["trial_signups"] > m["onboarding_completed"]:
        messages.append(
            "Teste um exemplo de primeiro lançamento mais curto."
            if pt
            else "Test a shorter example first transaction."
        )
    if m["onboarding_completed"] > m["activated"]:
        messages.append(
            "Revise a passagem do primeiro lançamento para a ativação."
            if pt
            else "Review the step from first transaction to activation."
        )
    if m["activated"] > m["paid_conversion"]:
        messages.append(
            "Teste a clareza do preço e do comando ASSINAR."
            if pt
            else "Test how clearly pricing and the subscribe command are presented."
        )
    for name, campaign in report["campaigns"].items():
        if campaign["conversation_started"] >= 3 and not campaign["activated"]:
            messages.append(
                f"{name}: revise a mensagem inicial e o público."
                if pt
                else f"{name}: review introductory copy and audience."
            )
        cost = campaign["cost_per_activated_cents"]
        target = report["experiment"]["budget_cents"] / report["experiment"]["target_activated"]
        if cost is not None and cost > target:
            messages.append(
                f"{name}: custo por ativação acima da meta."
                if pt
                else f"{name}: cost per activation exceeds the target."
            )
    retention = m["trial_retention"]["day_7"]
    if retention["eligible"] >= 5 and retention["rate"] < 0.4:
        messages.append(
            "Teste um convite para consultar o resumo semanal."
            if pt
            else "Test an invitation to check the weekly summary."
        )
    return messages


def parse_day(value):
    parsed = date.fromisoformat(value)
    if parsed.isoformat() != value:
        raise ValueError("Use YYYY-MM-DD")
    return parsed
