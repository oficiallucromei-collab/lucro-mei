"""Optionally select a weekly experiment from predefined actions using aggregate counts."""

import json

import httpx

ACTIONS = {
    "onboarding": (
        "Simplifique o exemplo do primeiro lançamento.",
        "Simplify the first transaction example.",
    ),
    "activation": (
        "Teste um lembrete para registrar a próxima operação.",
        "Test a prompt to log the next transaction.",
    ),
    "checkout": (
        "Teste a clareza do preço e do comando ASSINAR.",
        "Test how clearly pricing and ASSINAR are presented.",
    ),
    "retention": (
        "Teste um convite para consultar o resumo semanal.",
        "Test an invitation to view the weekly summary.",
    ),
    "observe": (
        "Colete mais dados antes de alterar o funil.",
        "Gather more data before changing the funnel.",
    ),
}


def suggested_experiment(settings, report):
    if not settings.growth_ai_key or not settings.growth_ai_model:
        return None
    measured = report["measured"]
    if measured["trial_signups"] < 5:
        return None
    # Explicit allowlist: no customer IDs, financial ledger data, or campaign tags.
    fields = (
        "trial_signups",
        "onboarding_completed",
        "activated",
        "paid_conversion",
        "trial_retention",
        "cost_per_activated_cents",
    )
    evidence = {name: measured[name] for name in fields}
    try:
        response = httpx.post(
            "https://api.openai.com/v1/responses",
            headers={"Authorization": f"Bearer {settings.growth_ai_key}"},
            json={
                "model": settings.growth_ai_model,
                "store": False,
                "input": json.dumps(evidence),
                "instructions": (
                    "Choose a single experiment for this acquisition funnel. "
                    "These are descriptive aggregate metrics and recent cohorts may be immature. "
                    "Choose observe if sample size or time is insufficient."
                ),
                "max_output_tokens": 256,
                "text": {
                    "format": {
                        "type": "json_schema",
                        "name": "weekly_experiment",
                        "strict": True,
                        "schema": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["action"],
                            "properties": {
                                "action": {"type": "string", "enum": list(ACTIONS)},
                            },
                        },
                    }
                },
            },
            timeout=12,
        )
        response.raise_for_status()
        result = response.json()
        if result["status"] != "completed":
            return None
        choice = next(
            json.loads(item["text"])["action"]
            for output in result["output"]
            if output["type"] == "message"
            for item in output["content"]
            if item["type"] == "output_text"
        )
        return ACTIONS[choice][int(settings.growth_report_language == "en")]
    except (httpx.HTTPError, KeyError, ValueError, StopIteration, TypeError):
        return None
