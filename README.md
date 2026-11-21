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
- shared Brazilian amount parser (`1.500,00`, `1500`, `12,50`, `12.50`, `R$`) and
  quantities with an explicit total (`Fiz 3 cortes por 90 reais`);
- deterministic monthly summaries in the business's local time zone (default
  `America/Sao_Paulo`), separating the business result (income − expenses) from what is left
  after personal withdrawals;
- WhatsApp Cloud API webhook verification and signature validation;
- replies sent through the WhatsApp Cloud API (`httpx`, timeouts, retryable vs permanent Meta
  errors), with a mock sender for tests and local development, and delivery receipts (sent,
  delivered, read, failed) recorded from the webhook;
- background processing: the webhook only validates, records the message once (its Meta ID is
  the idempotency key) and answers 200; a PostgreSQL job queue (`SELECT ... FOR UPDATE SKIP
  LOCKED`) then processes it and sends the reply, with bounded retries and exponential backoff;
- idempotent WhatsApp commands for undo, corrections, and monthly summaries;
- clarification instead of rejection: an ambiguous message ("Paguei 300 para o fornecedor") is
  kept as a pending entry and the user is asked whether it is a business expense or a personal
  withdrawal; the transaction is recorded only after the answer, and questions expire after
  `LUCROMEI_PENDING_ENTRY_TTL_HOURS` (24 by default);
- optional AI fallback (`claude-haiku-5-5`, structured JSON output) for messages the rules
  cannot read: the answer is re-validated with the same rules (amount must be typed by the user
  and pass `parse_brl_amount`, type within the enum), the description comes from the user's own
  text, and every suggestion must be confirmed with SIM before it is recorded; usage, tokens and
  cost (integer micro-dollars) are stored in `ai_usage`, never the message text;
- structured JSON logs with a correlation ID carried from the webhook request to its background
  jobs; logs never include message text, amounts, names or phone numbers (allowlisted fields,
  exception messages dropped); optional Sentry via `LUCROMEI_SENTRY_DSN`, without PII, local
  variables or request bodies;
- privacy (LGPD) over WhatsApp: "meus dados" returns everything stored about the user, and
  "apagar meus dados" (confirmed with APAGAR) blocks the account for 7 days, during which the
  user can cancel, then erases every row tied to them, keeping only an anonymous HMAC-based
  proof; an hourly maintenance job (deduplicated across workers) runs due deletions, erases raw
  message text after 90 days and removes old finished jobs;
- onboarding on first contact: name, type of business, acceptance of the terms (version and
  date stored in `terms_acceptances`) and the start of a 14-day free trial; nothing is written
  to the ledger before the terms are accepted;
- unit and API tests;
- Docker and PostgreSQL development configuration.

Original transaction rows are never silently overwritten. Corrections and reversals are appended
to the audit event stream, and summaries replay those events to obtain the effective ledger state.

## Local development

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
pytest                     # in-memory SQLite
LUCROMEI_TEST_DATABASE_URL=postgresql+psycopg://... pytest   # real PostgreSQL, as in CI
alembic upgrade head
uvicorn lucromei.main:app --reload
```

### WhatsApp Cloud API

Set `LUCROMEI_WHATSAPP_ACCESS_TOKEN` (a System User token with `whatsapp_business_messaging`)
and `LUCROMEI_WHATSAPP_PHONE_NUMBER_ID`. Without them replies are only recorded by the mock
sender; with `LUCROMEI_ENVIRONMENT=production` the app refuses to start. In the Meta app,
subscribe the webhook to the `messages` field, which carries both user messages and delivery
receipts. Free-form replies only work within 24 hours of the user's last message; outside that
window Meta answers with error 131047 and the send is not retried.

### Background worker

Received WhatsApp messages are processed by a worker that reads the `jobs` table. By default
(`LUCROMEI_WORKER_MODE=embedded`) it runs inside the web process. To run it separately, set
`LUCROMEI_WORKER_MODE=off` on the web service and start:

```bash
python -m lucromei.worker          # keeps running
python -m lucromei.worker --once   # drains due jobs and exits
```

Jobs that fail are retried with exponential backoff (5s, 10s, 20s, … capped at 15 min) up to
`LUCROMEI_JOB_MAX_ATTEMPTS`, then marked `dead` and kept for inspection. Replies are sent at
least once: if the process dies right after WhatsApp accepts a message and before the database
commit, that reply can be sent again.

Open `http://127.0.0.1:8000/docs` for the interactive API documentation.

Example:

The `/v1` endpoints have no end-user authentication yet, so they are an internal tool: they
are disabled unless `LUCROMEI_INTERNAL_API_KEY` is set, and every request must send that key in
the `X-Internal-Api-Key` header.

```bash
curl -X POST http://127.0.0.1:8000/v1/messages \
  -H 'content-type: application/json' \
  -H "X-Internal-Api-Key: $LUCROMEI_INTERNAL_API_KEY" \
  -d '{
    "message_id": "wamid.demo-1",
    "whatsapp_number": "+5511999999999",
    "text": "Recebi 150 reais por um corte e barba"
  }'
```

## Roadmap

1. Subscription portal and billing (the trial is recorded, not yet enforced).

