"""WhatsApp message templates, for messages sent outside the 24-hour customer service window.

Meta only delivers free-form text within 24 hours of the user's last message. Anything later
(the end of the free trial, reminders) must use a template approved in the WhatsApp Manager.
Each template here must be registered there with exactly this name, language, category and
body, where {{1}}, {{2}} are the parameters in order. docs/whatsapp-templates.md has the
registration steps and sample values.

The same text, with the parameters filled in, is sent as a normal message when the window is
still open, so the user reads the same words either way.
"""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Template:
    name: str
    category: str  # UTILITY or MARKETING, as registered at Meta
    body: str
    examples: tuple[str, ...]
    language: str = "pt_BR"

    def render(self, parameters: list[str]) -> str:
        text = self.body
        for index, value in enumerate(parameters, 1):
            text = text.replace("{{" + str(index) + "}}", value)
        return text


TRIAL_ENDING = Template(
    name="lucromei_teste_terminando",
    category="UTILITY",
    body=(
        "Olá, {{1}}! Seu teste grátis do LucroMEI termina em {{2}}. Depois disso, para "
        "continuar anotando suas entradas e despesas, responda ASSINAR. Seus lançamentos "
        "continuam guardados."
    ),
    examples=("Ana", "04/12/2026"),
)

TRIAL_ENDED = Template(
    name="lucromei_teste_encerrado",
    category="UTILITY",
    body=(
        "Olá, {{1}}! Seu teste grátis do LucroMEI terminou em {{2}}. Seus lançamentos "
        "continuam guardados e você ainda pode pedir resumos. Para voltar a anotar, "
        "responda ASSINAR."
    ),
    examples=("Ana", "04/12/2026"),
)

REMINDER = Template(
    name="lucromei_lembrete_lancamentos",
    category="UTILITY",
    body=(
        "Olá, {{1}}! Faz {{2}} dias que você não anota nenhuma movimentação no LucroMEI. "
        "Para anotar, é só mandar, por exemplo: Recebi 150 por um corte. Para não receber "
        "mais lembretes, responda PARAR LEMBRETES."
    ),
    examples=("Ana", "7"),
)

SUBSCRIPTION_ACTIVE = Template(
    name="lucromei_assinatura_ativa",
    category="UTILITY",
    body=(
        "Pagamento confirmado! Sua assinatura do LucroMEI está ativa até {{1}}. Pode continuar "
        "mandando suas movimentações por aqui."
    ),
    examples=("04/01/2027",),
)

CATALOG = {
    template.name: template
    for template in (TRIAL_ENDING, TRIAL_ENDED, REMINDER, SUBSCRIPTION_ACTIVE)
}
