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
- idempotent inbound-message processing;
- unit and API tests;
- Docker and PostgreSQL development configuration.

The in-memory ledger is intentional for this first verified slice. The next milestone replaces it
with PostgreSQL models, migrations, an audit-event ledger, and a real WhatsApp webhook adapter.

## Local development

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
pytest
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

## Roadmap

1. PostgreSQL persistence and Alembic migrations.
2. Immutable transaction corrections and monthly summaries.
3. WhatsApp Cloud API webhook verification, signature checks, and mocked sender.
4. Structured AI fallback for messages that deterministic rules cannot classify.
5. Minimal onboarding and subscription portal.

