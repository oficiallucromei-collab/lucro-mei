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


## Áudios recebidos pelo WhatsApp

O webhook assinado da Cloud API registra o ID da mensagem e enfileira a transcrição no
PostgreSQL. O worker usa o número de destino configurado para resolver e baixar a mídia e
só envia áudio à OpenAI de remetentes com cadastro concluído. O FFmpeg valida o áudio
decodificado, mede a duração e gera um WAV mono em memória. A transcrição em português
é gravada junto com o próximo job, que executa as mesmas regras, confirmações, comandos e
controles de acesso das mensagens digitadas.

O fluxo atual aceita **uma operação por mensagem** e tem consultas de resumo mensal, como
“Resumo do mês”. A pergunta “Quanto eu ganhei essa semana?” e os lançamentos em lote
dependem de evoluções do fluxo compartilhado. Para lotes, a próxima etapa é criar uma
prévia com todas as operações, pedir confirmação explícita e gravar o lote em uma única
transação do banco, com idempotência vinculada à mensagem de origem.

### Configuração

| Variável | Padrão | Uso |
| --- | --- | --- |
| `LUCROMEI_WHATSAPP_AUDIO_ENABLED` | `false` | Ativa o recebimento de voz no webhook e no worker. |
| `LUCROMEI_WHATSAPP_AUDIO_MAX_DURATION_SECONDS` | `60` | Limite de duração de 1 a 60 segundos. |
| `LUCROMEI_WHATSAPP_AUDIO_MAX_SIZE_BYTES` | `4194304` | Limite de download, configurável até 16 MiB. |
| `LUCROMEI_OPENAI_API_KEY` | vazio | Injete a chave pelo Secret Manager ou equivalente. |
| `LUCROMEI_OPENAI_TRANSCRIPTION_MODEL` | `gpt-4o-mini-transcribe` | Aceita a família `gpt-4o-mini-transcribe` ou `gpt-4o-transcribe`, incluindo versões com logprobs. |
| `LUCROMEI_OPENAI_TRANSCRIPTION_TIMEOUT_SECONDS` | `25` | Tempo total permitido por chamada à OpenAI. |
| `LUCROMEI_WHATSAPP_TIMEOUT_SECONDS` | `10` | Tempo total para resolver e baixar a mídia. |
| `LUCROMEI_MESSAGE_TEXT_RETENTION_DAYS` | `90` | Retenção existente, aplicada também às transcrições. |

O serviço aceita OGG/Opus e os tipos MPEG, MP4, AAC e AMR. Áudio vazio, trechos marcados
como inaudíveis ou scores de confiança baixos geram um pedido para repetir ou digitar a
mensagem. A confiança da transcrição é uma estimativa. O valor reconhecido passa pelas
validações financeiras existentes. Nenhum arquivo de áudio é escrito em disco. O ID de
mídia sai da linha ao concluir a transcrição ou ao encerrar a tentativa. O fluxo de
exclusão da conta também apaga a linha da mensagem e os dados vinculados. A transcrição
integral fica na caixa de entrada com a retenção configurada. A descrição financeira
extraída pelo fluxo existente continua no livro-caixa.

### Implantação e verificação

1. Em homologação, aplique `alembic upgrade head` antes de iniciar o novo serviço e os
   workers. A imagem inclui FFmpeg. Em instalações fora da imagem, instale o executável
   `ffmpeg` no `PATH`.
2. Configure o número do WhatsApp, a versão do Graph, o segredo do webhook e o token Meta
   com acesso às mídias do número. Injete a chave OpenAI pelo gerenciador de segredos.
   Verifique a política de privacidade vigente e o tratamento por fornecedores antes de
   habilitar a flag. [Controles de dados da OpenAI](https://developers.openai.com/api/docs/guides/your-data)
3. Habilite a flag no webhook e nos workers. Com o worker embutido no Cloud Run, mantenha
   CPU disponível fora das requisições. Confirme que o webhook responde antes do
   processamento.
4. Depois de concluir o cadastro de teste, envie um áudio OGG/Opus com uma receita, uma
   despesa ou uma retirada. Confira a confirmação em português e o valor persistido.
   Envie “Resumo do mês”, um áudio pouco inteligível e um acima do limite configurado.
   Reentregue um webhook de teste com o mesmo ID e confira que há um único lançamento.
5. Monitore os eventos `audio_transcription_request` (volume, duração em
   `audio_duration_ms`, latência e resultado) e `audio_processing_failed` (código).
   Falhas transitórias têm até três tentativas na fila, com nova resolução da URL a cada
   tentativa. Se o job esgotar as tentativas, a fila registra o erro e pede um novo áudio
   ou texto ao remetente. Para interromper o recurso, desabilite a flag no webhook e nos
   workers. Mensagens já transcritas podem terminar pelo fluxo de texto.

O ID da mensagem é a chave de deduplicação. Após o commit da transcrição, uma falha no
processamento financeiro ou no envio da resposta pode ser reprocessada a partir da sua
própria etapa. Se o worker parar depois que a OpenAI recebeu o áudio e antes de o commit
ser concluído, a recuperação da fila pode repetir a chamada de transcrição.

### Estimativa de custo

A tarifa estimada de `gpt-4o-mini-transcribe` é **US$ 0,003 por minuto**. Para 30 áudios
de 30 segundos por usuário ao mês, a estimativa é **US$ 0,045 por usuário/mês**.
Multiplique a soma de `audio_duration_ms` nas chamadas por US$ 0,003 e divida por
60.000 para estimar o custo de transcrição. A fatura depende dos tokens cobrados e das
tentativas processadas pelo provedor. A estimativa cobre somente a transcrição.
[Preços da OpenAI](https://developers.openai.com/api/docs/pricing)

A OpenAI anuncia encerramento dos modelos da família GPT-4o Transcribe em 26 de fevereiro
de 2027. Antes dessa data, valide um sucessor com sinal de confiança equivalente e atualize
o contrato da integração. [Guia de transcrição da OpenAI](https://developers.openai.com/api/docs/guides/speech-to-text)
