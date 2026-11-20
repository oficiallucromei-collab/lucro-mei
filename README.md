# LucroMEI

LucroMEI is a WhatsApp-first financial assistant for Brazilian microentrepreneurs. The MVP
starts with independent barbers and helps them record income, business expenses, and personal
withdrawals in natural Portuguese.

## Product guardrails

- AI may interpret language, but deterministic code performs financial calculations.
- Ambiguous messages trigger clarification instead of silently creating a transaction.
- Monetary values are stored as integer centavos.
- WhatsApp message IDs are idempotency keys.
- Corrections will be auditable events, not destructive overwrites.
- The product does not provide tax, accounting, or investment advice.

## Current slice

This initial scaffold includes:

- FastAPI application and health endpoint;
- deterministic Portuguese transaction parser;
- income, expense, and personal-withdrawal types;
- PostgreSQL persistence with Alembic migrations;
- database-enforced idempotent inbound-message processing;
- immutable correction and reversal events;
- deterministic monthly summaries separating personal withdrawals;
- WhatsApp Cloud API webhook verification and signature validation;
- normalized inbound text messages with WhatsApp Cloud API or mocked outbound replies;
- idempotent WhatsApp commands for undo, corrections, and monthly summaries;
- unit and API tests;
- Docker and PostgreSQL development configuration.

Original transaction rows are never silently overwritten. Corrections and reversals are appended
to the audit event stream, and summaries replay those events to obtain the effective ledger state.

## Local development

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
pytest
alembic upgrade head
uvicorn lucromei.main:app --reload
```

Open `http://127.0.0.1:8000/docs` for the interactive API documentation.

Example:

```bash
curl -X POST http://127.0.0.1:8000/v1/messages \
  -H 'content-type: application/json' \
  -d '{
    "message_id": "wamid.demo-1",
    "whatsapp_number": "+5511999999999",
    "text": "Recebi 150 reais por um corte e barba"
  }'
```

## Production WhatsApp configuration

Set `LUCROMEI_ENVIRONMENT=production`. Direct ledger endpoints under `/v1` are
available only in the local environment. Production ledger access uses the signed
WhatsApp webhook.

Set the Meta webhook callback to the service's HTTPS URL with the path
`/webhooks/whatsapp`. The same path handles GET verification and signed POST events.
Subscribe to the `messages` webhook field.

| Variable | Purpose |
| --- | --- |
| `LUCROMEI_WHATSAPP_VERIFY_TOKEN` | Shared token for webhook verification |
| `LUCROMEI_WHATSAPP_APP_SECRET` | Meta app secret for validating webhook signatures |
| `LUCROMEI_WHATSAPP_ACCESS_TOKEN` | Production access token for sending replies |
| `LUCROMEI_WHATSAPP_PHONE_NUMBER_ID` | Meta phone-number ID for sending replies |
| `LUCROMEI_WHATSAPP_GRAPH_API_VERSION` | Graph API version, defaults to `v25.0` |

The app uses the Cloud API when both the access token and phone-number ID are set.
Local development can use the mock sender. A Cloud Run service requires a complete
production configuration at startup. Its runtime provides `PORT` and `K_SERVICE`.

Before deploying, review the Cloud Run revision diff against the intended image,
command, arguments, service account, ingress, and environment variable names. Use
the image command and arguments and deploy a reviewed image digest. Configure the
following values:

- Set `LUCROMEI_ENVIRONMENT=production` and the expected Meta phone-number ID.
  Set the Graph API version only when overriding the application's default.
- Map `LUCROMEI_DATABASE_URL`, `LUCROMEI_WHATSAPP_VERIFY_TOKEN`,
  `LUCROMEI_WHATSAPP_APP_SECRET`, and `LUCROMEI_WHATSAPP_ACCESS_TOKEN` to
  pinned Secret Manager versions. Give a dedicated runtime service account
  access to just those secrets. Use a `postgresql+psycopg` URL with
  `sslmode=verify-full`, a trusted CA source such as `sslrootcert=system`,
  and a limited database role for the service.
- Restrict who can deploy revisions and act as the runtime service account.
  Confirm that the webhook accepts the Meta verification challenge and signed
  messages for the configured phone-number ID, and that `/v1` returns 404.

[Cloud Run secret configuration](https://docs.cloud.google.com/run/docs/configuring/services/secrets)

The container disables Uvicorn access logging because Meta sends the webhook
verification token in the query string. Cloud Run request logs are separate.
Before registering the callback, review log routing and exclude verification
requests containing `hub.verify_token` from storage and export sinks. Rotate
any verification token previously recorded in logs or saved in a deployment draft.

[Cloud Run logging](https://docs.cloud.google.com/run/docs/logging)

## Roadmap

1. WhatsApp delivery observability.
2. Structured AI fallback for messages that deterministic rules cannot classify.
3. Minimal onboarding and subscription portal.


## Acquisition and founder operations

The acquisition funnel runs in the existing FastAPI service. See the
[growth runbook](GROWTH.md) for setup, event definitions, campaign imports, privacy
requests, and costs. Growth is off by default.
