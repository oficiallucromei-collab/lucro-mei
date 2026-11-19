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
Otherwise it uses the local mock sender. Store production credentials in a secret manager.
The Docker container listens on the runtime `PORT` and defaults to 8080.

## Roadmap

1. WhatsApp delivery observability.
2. Structured AI fallback for messages that deterministic rules cannot classify.
3. Minimal onboarding and subscription portal.

