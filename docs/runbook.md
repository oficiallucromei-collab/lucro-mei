# Runbook de incidentes

Comandos para o ambiente de teste (`lucromei-test`, região `southamerica-east1`, projeto
`lucro-mei`). Para produção, troque o serviço e os jobs pelos equivalentes `-prod`.
Ferramentas internas: `gcloud run jobs execute lucromei-ops --region=southamerica-east1 --wait --args=<comando>,...`
e a saída em `gcloud logging read 'resource.labels.job_name="lucromei-ops"' --freshness=15m --format='value(textPayload)'`.
Nada nas saídas traz texto de mensagem, nome ou telefone.

## Primeiro passo, sempre

1. `curl -s https://lucromei-test-vn7ehb54iq-rj.a.run.app/health`
2. Erros recentes: `gcloud logging read 'resource.labels.service_name="lucromei-test" AND severity>=WARNING' --freshness=1h --format='value(timestamp,jsonPayload.event,jsonPayload.error)'`
3. Contagens: `lucromei-ops` com `pilot-check,--hours,2`.
4. Anote horário de início, sintoma e o que foi feito (para o registro do incidente).

## Sintomas e ações

| Sintoma | Causa provável | Ação |
| --- | --- | --- |
| `/health` falha ou 5xx | release ruim, banco fora | voltar para a imagem anterior (README do Terraform, "Roll back"); ver status do Neon |
| `job_dead` com `meta_190` | token da Meta expirou ou foi revogado | gerar token novo (`docs/seguranca.md`), `scripts/set-secret.sh lucromei-whatsapp-access-token`, subir a versão no Terraform e aplicar; depois `requeue-dead,--kind,send_message,--hours,24` |
| `job_dead` com `meta_131047` | janela de 24 h fechada | esperado para respostas atrasadas; avisos proativos usam templates |
| `job_dead` com `meta_131030` | número fora da lista de teste da Meta | só em número de teste: cadastrar o destinatário no painel |
| `whatsapp_delivery_failed` com `meta_130497` | a Meta bloqueia envio para o país do destinatário | verificar restrições da conta no WhatsApp Manager |
| webhooks com 401 | segredo do app trocado na Meta e não no Secret Manager | atualizar `lucromei-whatsapp-app-secret` e aplicar; a Meta reenvia por até 7 dias |
| `queue_health` com `oldest_due_seconds` alto | worker parado ou lento | conferir se a instância está viva (`cpu_idle=false`); subir `max_instances` ou ligar o worker separado |
| muitas 429 no webhook | abuso ou pico | conferir origem no Cloud Logging; se for a Meta, subir `LUCROMEI_WEBHOOK_REQUESTS_PER_MINUTE` |
| usuário diz que a resposta não chegou | entrega falhou | `business,<id>` (ache o id com `find-business,<4 últimos dígitos>`) e ver "respostas com falha" |

## Pedidos de usuários

- **Estender o teste**: `extend-trial,<id>,--days,7`.
- **Acesso manual (cliente piloto, sem cobrança)**: `grant-subscription,<id>,--days,30`.
- **Exclusão de dados por e-mail** (usuário sem acesso ao WhatsApp): confirmar que a pessoa é
  titular do número (por exemplo, pedindo que mande uma mensagem do número, ou por documento),
  e só então seguir com o pedido; registrar data do pedido e da execução.

## Incidente de segurança ou vazamento de dados

1. Conter: trocar o segredo afetado (`docs/seguranca.md`), desativar versões antigas, revisar
   acessos no IAM e no Neon.
2. Avaliar: quais dados, quantos titulares, desde quando.
3. Comunicar: se houver risco ou dano relevante aos titulares, a LGPD (art. 48) e o regulamento
   da ANPD (Resolução CD/ANPD nº 15/2024) pedem comunicação à ANPD e aos titulares em até 3
   dias úteis do conhecimento do incidente. Guarde o registro mesmo quando não comunicar.
4. Registrar: o que aconteceu, impacto, ações e prevenção.

## Rollback de release

Veja `terraform/README.md` ("Roll back"). Migrações são só aditivas; a imagem anterior
funciona com o banco migrado. O próximo merge no `main` faz deploy de novo, então reverta ou
corrija o commit problemático.
