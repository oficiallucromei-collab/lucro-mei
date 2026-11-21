"""Terms of use and privacy policy, served by the app itself at /termos and /privacidade.

The texts live in code so that the version users accept (settings.terms_version) and the text
published at settings.terms_url always come from the same release. Bump terms_version whenever
the wording changes in substance.

The figures (retention, grace period, trial length) come from the same settings the code uses,
so the published policy cannot drift from what the app actually does.
"""

from html import escape

from lucromei.config import Settings

CONTROLLER = "JR Tech"

_STYLE = """
:root { color-scheme: light dark; --fg: #162d25; --bg: #f5faf7; --muted: #4a5f57; --link: #146247; }
@media (prefers-color-scheme: dark) {
  :root { --fg: #e4efe9; --bg: #0f1a16; --muted: #a9bdb4; --link: #7fd1ad; }
}
body { margin: 0; background: var(--bg); color: var(--fg);
       font-family: system-ui, -apple-system, "Segoe UI", sans-serif; }
main { max-width: 42rem; margin: auto; padding: 2.5rem 1rem 4rem; line-height: 1.65; }
h1 { font-size: clamp(1.8rem, 5vw, 2.6rem); line-height: 1.15; margin: 0 0 .5rem; }
h2 { font-size: 1.2rem; margin: 2rem 0 .4rem; }
p, li { margin: .4rem 0; }
.meta { color: var(--muted); font-size: .95rem; }
a { color: var(--link); text-underline-offset: .15em; overflow-wrap: anywhere; }
code { font-weight: 600; }
"""


def _page(title: str, body: str, settings: Settings) -> str:
    return (
        '<!doctype html><html lang="pt-BR"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{escape(title)} | LucroMEI</title><style>{_STYLE}</style></head>"
        f'<body><main><h1>{escape(title)}</h1><p class="meta">Versão '
        f"{escape(settings.terms_version)}</p>{body}</main></body></html>"
    )


def _contact(settings: Settings) -> str:
    if settings.contact_email:
        email = escape(settings.contact_email)
        return f'pelo e-mail <a href="mailto:{email}">{email}</a>'
    return "respondendo pelo próprio WhatsApp do LucroMEI"


def terms_html(settings: Settings) -> str:
    contact = _contact(settings)
    body = f"""
<p>Estes termos explicam como funciona o LucroMEI, um assistente que anota pelo WhatsApp as
entradas, despesas e retiradas do seu negócio. O serviço é oferecido pela {CONTROLLER}.
Ao responder ACEITO no WhatsApp, você concorda com estes termos e com a
<a href="/privacidade">Política de Privacidade</a>.</p>

<h2>1. O que o LucroMEI faz</h2>
<ul>
<li>Anota os lançamentos que você manda por mensagem (por exemplo, "Recebi 150 por um corte").</li>
<li>Mostra resumos do dia, da semana e do mês, e os últimos lançamentos.</li>
<li>Pergunta antes de anotar quando a mensagem não está clara. Nada é anotado sem a sua
confirmação nesses casos.</li>
</ul>

<h2>2. O que o LucroMEI não faz</h2>
<ul>
<li>Não dá orientação contábil, fiscal, jurídica nem de investimentos.</li>
<li>Não emite nota fiscal, não calcula nem paga o DAS e não substitui um contador.</li>
<li>Não movimenta dinheiro nem acessa sua conta bancária.</li>
</ul>
<p>Os resumos mostram só a soma do que você mandou. Eles não consideram contas futuras,
reservas ou impostos, então não indicam quanto você pode retirar com segurança.</p>

<h2>3. Suas responsabilidades</h2>
<ul>
<li>Mandar informações verdadeiras e conferir as confirmações que o LucroMEI responde.
Se algo sair errado, mande DESFAZER ou CORRIGIR PARA (valor).</li>
<li>Usar o serviço só para o seu próprio negócio, sem enviar dados de outras pessoas além do
necessário para descrever um lançamento.</li>
<li>Não tentar atrapalhar, copiar ou usar o serviço para fins ilegais.</li>
</ul>

<h2>4. Teste grátis e assinatura</h2>
<p>Você tem {settings.trial_days} dias de teste grátis a partir do aceite destes termos. Depois
disso, para continuar anotando lançamentos, será preciso assinar. O preço e a forma de
pagamento serão informados antes, pelo WhatsApp, e nenhuma cobrança acontece sem que você
aceite. Mesmo sem assinar, você continua podendo pedir MEUS DADOS e APAGAR MEUS DADOS.</p>

<h2>5. Inteligência artificial</h2>
<p>Quando a mensagem não segue um formato que o sistema entende sozinho, um serviço de
inteligência artificial pode ser usado para interpretar o texto (ou a transcrição de um áudio).
A IA só sugere: o valor precisa estar escrito por você e o lançamento só é anotado depois que
você responde SIM. A IA pode errar; por isso a confirmação é obrigatória.</p>

<h2>6. Disponibilidade</h2>
<p>O LucroMEI está em fase inicial. Fazemos o possível para que funcione sempre, mas podem
acontecer atrasos e interrupções, inclusive do WhatsApp. Se uma resposta não chegar, mande a
mensagem de novo: o sistema reconhece mensagens repetidas e não anota duas vezes.</p>

<h2>7. Encerramento</h2>
<p>Você pode encerrar a qualquer momento mandando APAGAR MEUS DADOS. A exclusão acontece
{settings.deletion_grace_days} dias depois da confirmação, e você pode desistir nesse prazo.
A {CONTROLLER} pode suspender contas que descumpram estes termos, avisando pelo WhatsApp.</p>

<h2>8. Mudanças nestes termos</h2>
<p>Se os termos mudarem de forma importante, avisaremos pelo WhatsApp e pediremos um novo
aceite antes de continuar.</p>

<h2>9. Contato e lei aplicável</h2>
<p>Dúvidas ou reclamações: {contact}. Estes termos seguem as leis do Brasil, incluindo o Código
de Defesa do Consumidor quando aplicável.</p>
"""
    return _page("Termos de Uso do LucroMEI", body, settings)


def privacy_html(settings: Settings) -> str:
    contact = _contact(settings)
    retention = settings.message_text_retention_days
    grace = settings.deletion_grace_days
    body = f"""
<p>Esta política explica quais dados o LucroMEI usa, para quê, com quem compartilha e por quanto
tempo guarda. A {CONTROLLER} é a controladora dos dados, nos termos da Lei Geral de Proteção de
Dados (LGPD, Lei 13.709/2018). Contato do encarregado: {contact}.</p>

<h2>1. Dados que usamos</h2>
<ul>
<li><strong>Cadastro:</strong> número do WhatsApp, o nome e o tipo de negócio que você informar,
a data do aceite dos termos e as datas do teste grátis.</li>
<li><strong>Mensagens:</strong> o texto das mensagens trocadas com o LucroMEI e, se você mandar
áudio, a transcrição desse áudio. O arquivo de áudio não é guardado.</li>
<li><strong>Lançamentos:</strong> tipo, valor, data, descrição e o histórico de correções.</li>
<li><strong>Registros técnicos:</strong> identificadores das mensagens, status de entrega do
WhatsApp, quantas vezes a inteligência artificial foi usada (sem o conteúdo) e de qual
campanha de divulgação você veio, quando for o caso.</li>
</ul>

<h2>2. Para que usamos</h2>
<ul>
<li>Anotar lançamentos, responder comandos e calcular seus resumos (execução do contrato,
art. 7º, V, da LGPD).</li>
<li>Evitar lançamentos duplicados, manter o serviço seguro e investigar falhas (legítimo
interesse, art. 7º, IX).</li>
<li>Guardar a prova do seu aceite e dos pedidos de exclusão (cumprimento de obrigação legal e
exercício de direitos, art. 7º, II e VI).</li>
</ul>
<p>Não vendemos seus dados e não os usamos para publicidade.</p>

<h2>3. Com quem compartilhamos</h2>
<p>Usamos fornecedores que tratam os dados só para prestar o serviço ao LucroMEI (operadores):</p>
<ul>
<li><strong>Meta (WhatsApp):</strong> canal por onde as mensagens chegam e saem.</li>
<li><strong>Google Cloud:</strong> executa o sistema, em São Paulo.</li>
<li><strong>Neon:</strong> hospeda o banco de dados.</li>
<li><strong>Anthropic:</strong> interpreta, com inteligência artificial, mensagens que o sistema
não entende sozinho. Recebe só o texto daquela mensagem, sem seu número ou nome.</li>
<li><strong>OpenAI:</strong> transcreve mensagens de áudio, quando esse recurso está ligado.
Recebe só o áudio daquela mensagem.</li>
</ul>
<p>Alguns desses fornecedores podem tratar dados fora do Brasil, como nos Estados Unidos. Nesses
casos usamos fornecedores que oferecem garantias contratuais de proteção compatíveis com a LGPD
(art. 33). Dados também podem ser informados a autoridades quando a lei exigir.</p>

<h2>4. Por quanto tempo guardamos</h2>
<ul>
<li><strong>Texto das mensagens:</strong> até {retention} dias, para suporte. Depois é apagado
automaticamente.</li>
<li><strong>Lançamentos e cadastro:</strong> enquanto sua conta existir, porque são a base dos seus
resumos.</li>
<li><strong>Depois de um pedido de exclusão:</strong> tudo é apagado {grace} dias depois da sua
confirmação. Fica só uma prova anônima de que o pedido foi atendido (um código que não permite
descobrir seu número).</li>
<li><strong>Cópias de segurança:</strong> o banco de dados mantém cópias de segurança por poucos
dias, para recuperar o serviço em caso de falha. Dados apagados somem dessas cópias quando elas
expiram, em até 30 dias. Se uma cópia antiga precisar ser restaurada, refazemos as exclusões já
pedidas antes de voltar a usar os dados.</li>
</ul>

<h2>5. Seus direitos</h2>
<p>Você pode, a qualquer momento e de graça:</p>
<ul>
<li>ver todos os seus dados mandando <code>MEUS DADOS</code> no WhatsApp;</li>
<li>pedir a exclusão mandando <code>APAGAR MEUS DADOS</code> e confirmando com
<code>APAGAR</code>; para desistir no prazo, mande <code>CANCELAR EXCLUSÃO</code>;</li>
<li>corrigir um lançamento com <code>DESFAZER</code> ou <code>CORRIGIR PARA</code> (valor);</li>
<li>pedir informações sobre o uso dos seus dados ou fazer outros pedidos da LGPD
{contact};</li>
<li>reclamar à Autoridade Nacional de Proteção de Dados (ANPD), em
<a href="https://www.gov.br/anpd">gov.br/anpd</a>.</li>
</ul>

<h2>6. Segurança</h2>
<p>As conexões são criptografadas, o acesso ao banco é restrito e os registros técnicos do
sistema não guardam o texto das mensagens, valores, nomes ou números de telefone. Nunca pedimos
senhas, códigos de verificação ou dados bancários pelo WhatsApp.</p>

<h2>7. Menores de idade</h2>
<p>O LucroMEI é destinado a maiores de 18 anos que têm um negócio.</p>

<h2>8. Mudanças nesta política</h2>
<p>Se esta política mudar de forma importante, avisaremos pelo WhatsApp antes.</p>
"""
    return _page("Política de Privacidade do LucroMEI", body, settings)


def deletion_html(settings: Settings) -> str:
    """Data deletion instructions, the page Meta asks for in the app settings."""
    contact = _contact(settings)
    body = f"""
<p>Você pode apagar todos os seus dados do LucroMEI a qualquer momento, pelo próprio WhatsApp.</p>
<h2>Como pedir</h2>
<ol>
<li>Mande <code>APAGAR MEUS DADOS</code> para o número do LucroMEI.</li>
<li>Confirme respondendo <code>APAGAR</code> em até {settings.deletion_confirm_minutes}
minutos.</li>
<li>Pronto. Sua conta fica bloqueada e tudo é apagado {settings.deletion_grace_days} dias
depois. Para desistir nesse prazo, mande <code>CANCELAR EXCLUSÃO</code>.</li>
</ol>
<p>Se quiser uma cópia antes, mande <code>MEUS DADOS</code>.</p>
<h2>O que é apagado</h2>
<p>Cadastro, lançamentos, histórico de mensagens, perguntas pendentes, registros de uso e o
aceite dos termos. Fica só uma prova anônima de que o pedido foi atendido, que não permite
descobrir o seu número. Cópias de segurança expiram em até 30 dias.</p>
<h2>Sem acesso ao WhatsApp?</h2>
<p>Fale conosco {contact}, informando o número usado no LucroMEI.</p>
<p>Mais detalhes na <a href="/privacidade">Política de Privacidade</a>.</p>
"""
    return _page("Como apagar seus dados do LucroMEI", body, settings)


def checkout_html(settings: Settings, *, completed: bool) -> str:
    if completed:
        body = (
            "<p>Recebemos seu pagamento. Assim que o Stripe confirmar, você recebe uma mensagem "
            "no WhatsApp e pode continuar anotando normalmente.</p>"
            "<p>Pode fechar esta página e voltar para o WhatsApp.</p>"
        )
        return _page("Obrigado por assinar o LucroMEI", body, settings)
    body = (
        "<p>O pagamento não foi concluído e nada foi cobrado. Quando quiser, mande ASSINAR no "
        "WhatsApp para receber um novo link.</p>"
    )
    return _page("Assinatura não concluída", body, settings)
