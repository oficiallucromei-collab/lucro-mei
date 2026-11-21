# Roteiro do teste real controlado (`lucromei-test`)

Quem envia as mensagens é o dono, do próprio celular, para o número de teste do WhatsApp do
LucroMEI. Nenhuma outra pessoa participa. O roteiro leva uns 20 minutos. Anote o horário de
início: as verificações usam essa janela.

Antes de começar:

- O serviço `lucromei-test` está no ar: `curl -s https://lucromei-test-vn7ehb54iq-rj.a.run.app/health`
  responde `{"status":"ok"}`.
- O número do dono **já tem cadastro** (fez o onboarding antes)? Então pule a etapa 1 ou, para
  testar o onboarding de novo, faça primeiro a etapa 10 (exclusão) num outro dia e espere os 7
  dias de carência.
- Áudio só vale se `LUCROMEI_WHATSAPP_AUDIO_ENABLED=true` e houver chave da OpenAI no Secret
  Manager. Hoje não há: pule a etapa 8.

## Mensagens e respostas esperadas

Mande uma mensagem de cada vez e espere a resposta antes da próxima (alguns segundos).

| # | Você manda | O LucroMEI deve responder (início) |
| --- | --- | --- |
| 1a | `Oi` | "Olá! Eu sou o LucroMEI..." e pergunta o nome |
| 1b | seu nome | "Prazer, ...! Qual é o seu tipo de negócio?" |
| 1c | `Barbearia` | "Último passo: ... Termos de Uso ... (versão 2026-11-21). Você pode ler tudo aqui: https://lucromei-test-vn7ehb54iq-rj.a.run.app/termos" |
| 1d | abra o link | a página dos termos abre no celular, com link para a política de privacidade |
| 1e | `ACEITO` | "Tudo certo...! Seu teste grátis de 14 dias começou hoje e vai até ..." |
| 2 | `Recebi 150 por um corte` | "Entrada registrada: R$ 150,00. Responda 'desfazer' para corrigir." |
| 3 | `ontem gastei 40 em produtos` | "Despesa registrada em DD/MM/AAAA: R$ 40,00..." (data de ontem) |
| 4 | `Paguei 300 para o fornecedor` | "Antes de anotar R$ 300,00 (...), me diz: é despesa do negócio ou retirada pessoal? Responda 1 ... ou 2 ..." |
| 5 | `1` | "Despesa registrada: R$ 300,00..." |
| 6 | `desfazer` | "Lançamento de R$ 300,00 desfeito." |
| 7a | duas linhas: `Recebi 80 de barba` (Enter) `Retirei 50 para mim` | "Vou anotar 2 lançamentos: 1. Entrada R$ 80,00 ... 2. Retirada R$ 50,00 ... Responda SIM..." |
| 7b | `SIM` | "Pronto, anotei 2 lançamentos. Total: entradas R$ 80,00, retiradas R$ 50,00." |
| 8 | (só se o áudio estiver ligado) áudio: "recebi cem reais de um corte" | "Entrada registrada: R$ 100,00..." |
| 9a | `resumo do mês` | "Resumo de <mês>/<ano>: Entradas: R$ 230,00 ... Despesas do negócio: R$ 40,00 ..." |
| 9b | `quanto ganhei essa semana?` | "Resumo da semana (DD/MM a DD/MM): ..." |
| 9c | `últimos lançamentos` | lista com 4 lançamentos (o desfeito não aparece) |
| 10 | **mensagem repetida**: copie a mensagem da etapa 2 e mande de novo, igual | "Entrada registrada: R$ 150,00..." (é uma mensagem nova para o WhatsApp, então é um lançamento novo; mande `desfazer` em seguida). A deduplicação de verdade é a de reentrega da Meta, verificada no passo "Banco" abaixo. |
| 11 | `meus dados` | "Seus dados no LucroMEI em ...", com nome, tipo de negócio, termos aceitos, teste grátis e a lista de lançamentos |
| 12a | `apagar meus dados` | explica a exclusão e pede "responda APAGAR nos próximos 10 minutos" |
| 12b | `APAGAR` | "Combinado. Seus dados serão apagados em DD/MM/AAAA (7 dias)..." |
| 12c | `Recebi 10 por um corte` | "Sua conta está marcada para ser apagada em ... não registro nada novo..." |
| 12d | `CANCELAR EXCLUSÃO` | "Pronto, cancelei a exclusão. Sua conta continua ativa." |
| 13 | `ajuda` | lista de exemplos e comandos |

Se alguma resposta não chegar em 1 minuto, anote o número da etapa e o horário e siga.

## Verificação (feita por quem opera o projeto)

### Logs (sem texto, valores, nomes ou telefones)

```bash
gcloud logging read 'resource.type="cloud_run_revision" AND resource.labels.service_name="lucromei-test" AND jsonPayload.event=("job_dead" OR "job_retry_scheduled" OR "whatsapp_delivery_failed" OR "worker_batch_failed")' \
  --project=lucro-mei --freshness=2h --format='value(timestamp,jsonPayload.event,jsonPayload.job_kind,jsonPayload.error)'
```

Esperado: nada, ou só `job_retry_scheduled` isolados. Depois confira que cada mensagem gerou
um `whatsapp_webhook_received` com `accepted=1` e um `job_done` de `send_message`:

```bash
gcloud logging read 'resource.type="cloud_run_revision" AND resource.labels.service_name="lucromei-test" AND jsonPayload.event=("whatsapp_webhook_received" OR "job_done")' \
  --project=lucro-mei --freshness=2h --format='value(timestamp,jsonPayload.event,jsonPayload.job_kind,jsonPayload.accepted,jsonPayload.duplicates,jsonPayload.statuses)'
```

Reentregas da Meta aparecem como `duplicates=1` e não geram resposta nem lançamento novos.

### Banco (só contagens)

O banco de teste só aceita conexões do Google Cloud, então a verificação roda no job
`lucromei-ops` (mesma imagem do serviço, papel de runtime do banco) e o resultado vai para o
Cloud Logging:

```bash
gcloud run jobs execute lucromei-ops --project=lucro-mei --region=southamerica-east1 --wait \
  --args=pilot-check,--hours,2
gcloud logging read 'resource.type="cloud_run_job" AND resource.labels.job_name="lucromei-ops"' \
  --project=lucro-mei --freshness=15m --order=asc --format='value(textPayload)'
```

Esperado depois do roteiro completo:

- `Mensagens com mais de um lançamento (deve ser 0)`: 0.
- `Jobs mortos`: nenhum; `Fila atrasada`: 0.
- `Respostas` todas `sent`; `Respostas enviadas sem recibo delivered/read`: 0 (alguns
  minutos depois do fim).
- `Lançamentos` coerentes com o roteiro (entradas 150 + 80 (+150 da etapa 10), despesas 40 +
  300, retiradas 50; 300 e o repetido da etapa 10 marcados como desfeitos).
- `Perguntas e lotes`: um `single/resolved` e um `batch/resolved`.
- `Comandos`: `undo`, `summary`, `list`, `help`.
- `Pedidos de exclusão`: um `cancelled`.

### Teste de reentrega (opcional, sem celular)

A Meta reenvia webhooks quando não recebe 200 a tempo. Para simular, reenvie um webhook
assinado com o mesmo ID de mensagem (o segredo do app é necessário, então só quem opera o
projeto faz isso, a partir de um ambiente seguro): a resposta HTTP traz `duplicates: 1` e
nenhuma resposta nova é enviada.
