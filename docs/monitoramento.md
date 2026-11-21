# Monitoramento e alertas

## Sinais

| Sinal | De onde vem | Alerta (`terraform/monitoring.tf`) |
| --- | --- | --- |
| Serviço fora do ar | verificação `/health` a cada 5 min, de várias regiões | falha em 2+ regiões por 10 min |
| Erros 5xx | métrica nativa do Cloud Run `request_count` (`5xx`) | 5+ em 5 min |
| Jobs `dead` | log `job_dead` (`severity=ERROR`, com `job_kind` e `error`) | qualquer um |
| Falhas de entrega da Meta | log `whatsapp_delivery_failed` (código da Meta) | 3+ em 15 min |
| Atraso da fila | log `queue_health` a cada minuto por worker (`due_jobs`, `oldest_due_seconds`, `dead_jobs` na última hora) | job vencido há mais de 5 min, por 10 min |
| Exceções | Sentry (sem PII, sem variáveis locais, sem corpo de requisição) | e-mail do Sentry |

Os logs são JSON com `severity`, então o Cloud Logging filtra por gravidade. Nenhum log traz
texto de mensagem, valor, nome ou telefone (lista de campos permitidos em `observability.py`).

## Como ligar

1. O dono do projeto concede à conta operadora `roles/monitoring.editor` e
   `roles/logging.configWriter` (já listados em `terraform/iam.tf`; ver lista de ações do dono).
2. `terraform apply -var=monitoring_enabled=true` (ou mude o padrão em `variables.tf`).
3. Confirme o e-mail de `alert_email` se o Cloud Monitoring pedir.
4. Sentry (opcional): crie o projeto no Sentry, rode `scripts/set-secret.sh lucromei-sentry-dsn`
   e aplique com `-var=sentry_enabled=true`.

## Consultas úteis

```bash
# Últimos erros do serviço de teste
gcloud logging read 'resource.type="cloud_run_revision" AND resource.labels.service_name="lucromei-test" AND severity>=WARNING' \
  --project=lucro-mei --freshness=1d --format='value(timestamp,jsonPayload.event,jsonPayload.job_kind,jsonPayload.error)'

# Saúde da fila no último dia
gcloud logging read 'resource.labels.service_name="lucromei-test" AND jsonPayload.event="queue_health" AND jsonPayload.oldest_due_seconds>60' \
  --project=lucro-mei --freshness=1d --format='value(timestamp,jsonPayload.due_jobs,jsonPayload.oldest_due_seconds)'
```
