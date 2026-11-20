# LucroMEI acquisition and growth

## Repository assessment and rollout

The working checkout is `review/`, on `codex/production-deployment`. Its history
includes Cloud Run hardening beyond the supplied deployment bundle. The app
uses FastAPI, SQLAlchemy, PostgreSQL in production, Alembic, and Docker. The checkout also has [Cloud Run adoption notes](terraform/README.md)
for the test service and its isolated Neon database. The
WhatsApp Cloud API callback verifies Meta signatures and routes messages to a
deterministic Brazilian Portuguese parser and an auditable ledger. The
canonical WhatsApp number maps to one internal business UUID. Production
direct ledger APIs are disabled. The previous footprint had a health endpoint
and Cloud Run logging guidance. It did not contain a public site, end-user login,
mobile source, billing, analytics, acquisition flows, or lifecycle jobs.

The implementation follows this rollout order.

1. Apply the additive schema and verify signed messaging and attribution against a test database.
2. Test the landing page, onboarding, trial, ledger activation, and Stripe test-mode checkout.
3. Open founder reporting and import Ads Manager spend. Enable trial enforcement once checkout and privacy commands pass staging checks.
4. Schedule reports and cleanup. Enable approved opt-in templates separately.
5. Test a manually controlled Meta experiment. Enable referrals and AI analysis when there is enough evidence to evaluate them.

## Architecture and data

```text
Ad / referral / direct visit
  -> FastAPI landing -> single-use reference -> WhatsApp
  -> signed webhook -> Business -> Acquisition + existing ledger
  -> Stripe Checkout and Portal -> signed payment webhooks -> shared access state
  -> PostgreSQL growth events + daily Ads Manager CSV -> founder dashboard / weekly job
```

- `acquisitions` snapshots first-touch tags, segment, trial dates and activation
  settings. It stores consent state, referral code and Stripe identifiers and
  entitlement state. Existing WhatsApp businesses retain legacy access.
- `acquisition_clicks` stores a random, one-use reference redeemed within 24
  hours. It accepts identifier-like values for UTM source, medium, campaign and
  content, campaign and ad IDs, and referral ID. Use campaign aliases without
  personal information. Meta direct-to-WhatsApp entries use `referral.source_id`
  where supplied. That ID can refer to an ad or post, so use a manually mapped
  import identifier when comparing it with campaign reports.
- `growth_events` has a unique key per business and event or occurrence. It
  stores timestamps and, for billing only, centavos. Phone numbers, ledger
  amounts and transaction descriptions are outside this event store.
- `landing_days`, `campaign_days`, `growth_audit`, `growth_rate_buckets`, and
  `trial_holds` support daily counts, ad imports, founder auditing, a shared
  per-route request ceiling and bounded trial-abuse prevention.
- The business UUID and `growth.access` are the identity and entitlement
  integration points for a future mobile channel. The mobile application and
  its sign-in system are absent from this checkout. Once available, verify
  ownership of the WhatsApp identity before linking a mobile login to the
  UUID. Never accept a client-supplied phone number as proof of ownership.
  The mobile adapter should reuse the trial and ledger instrumentation;
  there is no mobile signup or purchase path in this build.
  [Apple App Review Guidelines](https://developer.apple.com/app-store/review/guidelines/#payments),
  [Google Play payments policy](https://support.google.com/googleplay/android-developer/answer/9858738).

### Event definitions

| Event | Meaning |
| --- | --- |
| `conversation_started`, `onboarding_started`, `trial_started` | First eligible user-initiated conversation for a new business. Business creation and trial times are stored in UTC. |
| `onboarding_completed`, `first_transaction` | First successfully committed transaction. Segment is optional. |
| `summary_prepared`, `summary_requested` | Automatic first-value summary generated, and first explicit summary command. No read receipt is inferred. |
| `third_transaction`, `activated` | Third effective transaction and configured activation threshold within the configured trial window. Reversed transactions are excluded at evaluation time. Historical milestones remain recorded. |
| `returned_day_7/14/30` | Inbound activity in the 24-hour interval starting N days after trial start. Reporting includes only cohorts old enough to complete the interval. |
| `paid_conversion` | First observed active subscription with a paid invoice and a paid-through date. |
| `paid_invoice:*`, `payment_failed:*`, `canceled:*`, `mrr_new:*` | Per-invoice collection or payment failure, per-subscription cancellation and new recurring amount. |
| `opted_in`, `opted_out`, `lifecycle:*`, `lifecycle_failed:*` | First preference events and reminder occurrences. The current opt-in timestamp controls eligibility. |
| `referral_reward_inviter`, `referral_reward_invited` | Trial extensions awarded on valid activation. One lifetime award as inviter; single first-touch referrer per invitee. |

## Deploy to the existing service

1. Run `pytest` and build the image. On staging PostgreSQL, use a migration role
   with schema permissions and execute `alembic upgrade head` from the new image
   before enabling growth. Keep the runtime database role limited to the
   required tables and operations. Use the existing Cloud Run deployment and
   Secret Manager setup in the main README. To roll back application behavior,
   disable the growth and lifecycle flags or roll back to the earlier revision.
   Leave migrated data in place during that rollback.
2. Set `LUCROMEI_GROWTH_ENABLED=true` on staging. Set the public HTTPS origin and
   real digit-only WhatsApp display number, reviewed privacy and terms URLs,
   and separate random secrets of at least 32 characters for the founder login
   and trial-abuse digest. Keep `GROWTH_ENFORCE_TRIAL=false` initially. All names
   in this runbook use the `LUCROMEI_` prefix on environment variables. Copy
   `.env.example` for local values and keep production secrets in Secret Manager.
3. Create a Stripe test-mode BRL monthly Price at the configured amount and
   enable customer-portal payment updates and cancellation. Set the test API key,
   price ID and webhook secret. Register `/webhooks/stripe` with webhook API
   version `2024-06-20` and events `checkout.session.completed`,
   `customer.subscription.updated`, `customer.subscription.deleted`,
   `invoice.paid` and `invoice.payment_failed`. The server checks signatures,
   matches the Stripe customer and product, and reads current subscription state
   from Stripe. Checkout and portal sessions are created on user request. Test
   with card payments, including renewal, scheduled cancellation, exhausted
   payment retries and webhook retries. Keep test and live databases, keys,
   prices, destinations and reports isolated. Enable live mode only after
   reviewing the commercial terms and running the staging smoke test.
   [Stripe subscription webhooks](https://docs.stripe.com/billing/subscriptions/webhooks),
   [Hosted Checkout](https://docs.stripe.com/billing/subscriptions/build-subscriptions).
4. Open the landing page with test UTMs and submit the WhatsApp prefill. Register
   three transactions, request a summary, type `ASSINAR`, finish test checkout,
   then send `MEU PLANO`. Validate `GERENCIAR ASSINATURA`, `PARAR`,
   `EXPORTAR MEUS DADOS`, and `EXCLUIR MINHA CONTA`. Confirm the dashboard
   auto-detects the signup, activation and payment in the selected period.
5. With the same image and database, run these one-shot Cloud Run Jobs under a
   dedicated service account. Trigger via Cloud Scheduler in America/Sao_Paulo.
   Limit overlapping executions. The lifecycle job remains disabled until
   templates and consent are ready.

| Job argument after `python -m lucromei.growth_jobs` | Suggested cadence | Output |
| --- | --- | --- |
| `weekly-report` | Monday 10:00 | Aggregate JSON in job logs, with budget state and next experiments |
| `prune` | Daily 04:00 | Deletes expired click references, rate buckets and trial holds, redacts growth reply receipts after two days, and clears raw inbound ledger message text on the first run at or after 30 days |
| `lifecycle` | Every four hours between 09:00 and 19:00 | Count accepted by WhatsApp, nonzero exit on send failure |

The `prune` job's database role needs `SELECT` on `inbound_messages.received_at`
and `inbound_messages.raw_text` and `UPDATE` on `inbound_messages.raw_text` for
message retention. The job preserves message IDs, ledger entries, and the link
between them so webhook retries remain idempotent.

Configure log alerts for job failures, Stripe and WhatsApp webhook 5xx responses,
and `budget_warning` or `budget_exhausted` in report output. During the experiment,
check the dashboard after each import and use the Ads Manager hard budget as
the source of spending control. The app-level landing/redirect ceiling is
global per replica group via PostgreSQL. If public traffic is exposed to
distributed abuse, add per-client edge rate limits at the HTTPS ingress.
Restrict access to logs and secrets. Set a backup and ledger retention policy
before production. Keep the existing exclusion for Meta verification queries.
Avoid UTMs with customer identifiers.

### Proactive WhatsApp messaging

An explicit `ACEITO LEMBRETES` after consent disclosure enables trial reminders
and weekly notices. `PARAR` revokes them immediately. The job uses approved
`pt_BR` templates with static bodies, even within the service window, and
plain text webhook replies are restricted to the 24-hour window in production.
The job sends at most one lifecycle message in 24 hours per business and
operates during São Paulo daytime. Configure only approved names in
`GROWTH_LIFECYCLE_TEMPLATES` for `activation_help`, `trial_midpoint`,
`trial_expiring`, `subscription_confirmation`, and `weekly_summary`. Use the
weekly template to invite the user to request a summary rather than including
financial details in the template. Provider errors are logged via failed
occurrences and job exits. A provider acceptance followed by a crash before the
database commit can still produce a duplicate on retry. Inspect provider and
job logs before replaying failures. [WhatsApp Business Messaging Policy](https://business.whatsapp.com/policy).

## Founder operating guide

- Open `/founder` over HTTPS and sign in as `founder` with the founder token.
  Edit the date range for the experiment or download the equivalent
  `/founder/metrics` JSON. The dashboard distinguishes measured and estimated
  figures. The API uses BRL centavos and fractional rates.
- In Ads Manager, configure a lifetime budget of R$150 over 10 days manually.
  Use a matching `campaign_id` in the landing URL and set
  `GROWTH_EXPERIMENT_START`. Export daily campaign rows in BRL and São Paulo
  dates. Use the normalization below for manual import, with explicit zero
  rows on days where no ads ran. Import complete day totals for all relevant
  campaigns and resubmit rows to correct values. CSV import is the read-only
  reporting fallback. No Meta API credentials or campaign write capabilities
  are needed. [Meta Insights API](https://developers.facebook.com/docs/marketing-api/insights/).
- Look at spend, CAC, where users stop, and the count of mature cohorts. Use the
  defaults of five activated users and one to two paying customers as experiment
  targets. Landing visits are HTTP requests and include reloads/bots; WhatsApp
  counts are first eligible trial conversations. Trial conversion is cohort
  based and can increase after the report window. Overall CAC is blended
  across acquisition sources; campaign CAC uses first-touch matching.
  Imported spend and counts are limited to provided rows; missing days suppress
  cost ratios. Revenue is observed gross paid-invoice cash before fees,
  refunds or disputes. Paid retention uses observed subscriptions that started before the period
  and had no prior recorded cancellation. It measures cancellation-based
  retention and does not reconstruct entitlement losses during payment retries. Treat the
  LTV estimate as exploratory. It requires configured costs, at least 20 subscriptions at period start, and a 30-day period.
- Once a week, review the job output, compare campaign spend and activation,
  then change one variable you can evaluate. Reports can be in PT or EN. The
  optional OpenAI ranker runs only in the weekly job with an API key and a model
  supporting Structured Outputs. It sees a short allowlist of aggregated
  metrics after at least five signups and can select one predefined experiment.
  The report falls back to deterministic hypotheses when unavailable.
  [Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs).
- Review `/founder/privacy-requests` regularly. Verify the requester securely
  using the original, signed WhatsApp conversation and determine any applicable
  retention obligation before confirming fulfillment. Use POST to
  `/founder/privacy-requests/{business_id}/export` or `/deletion` with JSON
  `{"identity_verified":true}`. Transmit exports to the verified requester
  through a secure delivery channel. Cancel active Stripe subscriptions and
  resolve open checkout sessions before deleting; handle processor copies and
  backup retention independently. Deletion leaves a keyed, expiring digest for
  trial abuse prevention. Document and review its retention and keep the key
  stable for its retention horizon. Deletion removes account-level analytics,
  so historical counts may fall. [ANPD guidance on data subject rights](https://www.gov.br/anpd/pt-br/assuntos/titular-de-dados/direito-dos-titulares).
- After the core funnel is stable, enable `GROWTH_REFERRALS_ENABLED`. A user in
  trial can type `INDICAR`. The default reward is seven additional trial days
  for each party when the invitee activates and both trials are active. Only
  the first qualifying invite earns the inviter a reward. WhatsApp identity
  and the one-time referral credit limit basic abuse; separate phone numbers
  remain a possible source of abuse. Review suspicious clusters before widening.

CSV example. Replace dates and identifiers with real export values and use
integer centavos for spend. The `-u founder` option prompts for the password.

```csv
day,campaign,spend_cents,impressions,clicks
2026-11-20,238000000,1500,1000,24
```

```sh
curl --fail-with-body -u founder -H 'Content-Type: text/csv' \
  --data-binary @campaigns.csv https://YOUR_HOST/founder/campaigns
```

## Incremental monthly cost estimate

- Application and database reuse the existing Cloud Run service and PostgreSQL
  database. There are no additional fixed service subscriptions. For a
  small test on a service already within the Cloud Run free allowance, budget
  approximately R$0–R$20/month for incremental compute, jobs and logging, with
  actual spend dependent on region, billing tier, workload and log volume.
  This excludes the existing database, its storage and backups.
  [Cloud Run pricing](https://cloud.google.com/run/pricing).
- For a successful domestic card charge at R$24.90, the published Stripe
  standard 3.99% + R$0.39 and Billing 0.7% total approximately R$1.56 per
  paid subscriber per month. One to two paid subscriptions imply approximately
  R$1.56–R$3.12 in processing and Billing fees at that price.
  [Stripe Brazil pricing](https://stripe.com/br/pricing),
  [Stripe Billing pricing](https://stripe.com/br/billing/pricing).
- Proactive template spend is R$0 while disabled. For scale planning, use
  delivered templates by category multiplied by the current Brazil rates on
  your account. For example, 20 deliveries at a hypothetical R$0.35 each cost
  R$7, before BSP markups. This example is a budget assumption.
  [WhatsApp platform pricing](https://business.whatsapp.com/products/platform-pricing).
- The Ads Manager experiment is a separately approved one-time R$150 budget.
  Optional weekly AI usage depends on the selected model and is zero with the
  keys unset. Actual taxes, processor adjustments and contribution costs depend
  on the business and are outside these estimates.
