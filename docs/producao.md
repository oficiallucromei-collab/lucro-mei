# Ambiente de produção (`lucromei-prod`)

Produção é separada do teste: estado Terraform próprio (`terraform/prod`, prefixo
`lucromei-prod`), identidade de runtime própria (`lucromei-prod-runtime`), segredos próprios
(`lucromei-prod-*`), banco Neon próprio, jobs de migração e de ops próprios. O teste continua
recebendo deploy automático a cada merge no `main`; produção só recebe uma imagem que já está
rodando no teste, por **promoção manual com aprovação**.

## Custos estimados (por mês, volume baixo)

| Item | Estimativa |
| --- | --- |
| Cloud Run `lucromei-prod`, 1 instância sempre ligada (1 vCPU, 512 MiB) | US$ 50–70 |
| Neon, projeto de produção (plano Launch, com histórico de 7 dias) | a partir de ~US$ 19 |
| Secret Manager (cerca de 9 versões ativas) | ~US$ 0,54 |
| Cloud Build (promoções) e Artifact Registry | ~US$ 0 |
| WhatsApp (Meta): respostas dentro de 24 h | grátis |
| WhatsApp: templates de utilidade para o Brasil | cerca de US$ 0,008 por mensagem (conferir a tabela atual da Meta) |

## Passo a passo para criar (dono)

1. **Neon**: crie um projeto de produção (região `sa-east-1`), com dois papéis: dono (para
   migrações) e `lucromei_prod_runtime` (para o app; as migrações concedem os privilégios por
   tabela automaticamente). Ative histórico de pelo menos 7 dias.
2. **Meta**: verificação do negócio, número oficial com nome aprovado, revisão do app
   (`docs/meta-app-review.md`), templates aprovados (`docs/whatsapp-templates.md`).
3. **Criar produção**: `terraform -chdir=terraform/prod apply -var=enabled=true -var=whatsapp_phone_number_id=<id> -var=public_url=<url>`
   (revise o plano: só criações). Os segredos são criados vazios.
4. **Segredos** (cada um com `scripts/set-secret.sh <nome>`): `lucromei-prod-database-url`
   (papel de runtime), `lucromei-prod-migrate-database-url` (dono), `lucromei-prod-whatsapp-access-token`,
   `lucromei-prod-whatsapp-app-secret`, `lucromei-prod-whatsapp-verify-token`
   (`openssl rand -hex 32`), `lucromei-prod-privacy-hash-key` (`openssl rand -hex 32`, nunca
   rotacionar).
5. **Primeira promoção**: no Cloud Build, rode o gatilho `lucromei-promote` e aprove.
6. **Webhook**: no app de produção da Meta, aponte o webhook para `prod_webhook_url` (saída do
   Terraform) com o token de verificação do passo 4.
7. **Termos**: publique os termos num domínio próprio e passe `-var=terms_url=...`.

## Promover uma release

1. Confirme que o teste está saudável com a release nova (`pilot-check`, logs).
2. Cloud Build > Gatilhos > `lucromei-promote` > Executar. A execução fica **aguardando
   aprovação**; aprove em Cloud Build > Histórico.
3. O build resolve a imagem em execução no `lucromei-test` (ou a passada em `_IMAGE`), roda a
   migração de produção e faz o deploy, com verificação de `/health`.

Rollback: rode a promoção com `_IMAGE=<digest anterior>` (as migrações são aditivas).
