# Segurança e conformidade

Revisão feita em 21/11/2026 sobre o `main` (Fases 0 a 2) e a infraestrutura de teste.

## Superfície exposta

| Entrada | Proteção |
| --- | --- |
| `POST /webhooks/whatsapp` (público) | assinatura `X-Hub-Signature-256` (HMAC do corpo com o segredo do app, comparação em tempo constante); corpo limitado a 256 KiB; limite por cliente de 600 req/min e de 20 assinaturas inválidas/min (`ratelimit.py`, por instância); grava e responde 200, sem processamento síncrono |
| `GET /webhooks/whatsapp` | token de verificação comparado em tempo constante |
| `/v1/*` | desligado sem `LUCROMEI_INTERNAL_API_KEY`; com ela, exige o cabeçalho `X-Internal-Api-Key` |
| `/termos`, `/privacidade`, `/exclusao-de-dados` | HTML estático, CSP `default-src 'none'`, sem JS |
| `/docs`, `/openapi.json` | só com `LUCROMEI_ENVIRONMENT=local` |
| todas as respostas | `Strict-Transport-Security`, `X-Content-Type-Options`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`, `X-Request-ID` |

## Controles revisados

- **Segredos**: só no Secret Manager, montados como variáveis do Cloud Run por versão numerada; o
  Terraform guarda referências, nunca valores; `repr` das configurações e do cliente da Meta não
  mostra tokens. `scripts/set-secret.sh` grava versões novas sem eco.
- **Banco**: o serviço usa o papel `lucromei_test_runtime` (sem DDL; sem UPDATE no livro-caixa);
  migrações rodam como dono só no job `lucromei-migrate`; tabelas novas recebem GRANT pela
  própria migração (`lucromei.dbgrants`).
- **Logs**: JSON com lista de campos permitidos; nada de texto de mensagem, valores, nomes ou
  telefones; mensagens de exceção descartadas; consultas sem query string.
- **Sentry**: sem PII, sem variáveis locais, sem corpo de requisição.
- **IA**: entrada limitada (300 caracteres), limite diário por negócio, resposta revalidada pelas
  mesmas regras, descrição tirada do texto do usuário e confirmação SIM obrigatória.
- **Idempotência**: ID da mensagem da Meta como chave única; respostas únicas por mensagem;
  fila com `SKIP LOCKED`; lançamentos em lote com chaves `<id>#<n>`.
- **LGPD**: MEUS DADOS, APAGAR MEUS DADOS com carência, prova anônima por HMAC, retenção de 90
  dias do texto.
- **Imagem**: usuário não root, `pip` atualizado, smoke test com configuração de produção antes
  do push; deploy por digest.
- **Pipeline**: conta de serviço própria, `requestedVerifyOption: VERIFIED`, projeto e commit
  validados no primeiro passo.

## Achados e decisões

| # | Achado | Situação |
| --- | --- | --- |
| 1 | Webhook sem limite de requisições nem de tamanho | corrigido neste PR |
| 2 | Respostas sem cabeçalhos de segurança | corrigido neste PR |
| 3 | Meta exige URL de instruções de exclusão de dados | `/exclusao-de-dados` neste PR |
| 4 | Merges seguidos disparam builds concorrentes; um build antigo pode terminar depois e implantar a imagem anterior | corrigido neste PR: cada build marca a hora em que começou e o deploy não substitui uma release que começou depois (rótulo `lucromei-release-started` no serviço) |
| 5 | Senha do dono do banco Neon de teste foi exposta | **ação do dono** (abaixo) |
| 6 | Versões antigas do token do WhatsApp | versões 1 e 2 já estão `disabled` no Secret Manager; falta revogar o token antigo na Meta (**ação do dono**) |
| 7 | Papel antigo `cloudbuild.connectionViewer` da conta operadora | conferido em 21/11/2026: não aparece mais na política do projeto |
| 8 | Limite de requisições é por instância | aceito: o teste roda com no máximo 1 instância; em produção, avaliar Cloud Armor se houver abuso |
| 9 | O link dos termos usa a URL do Cloud Run | aceito para o teste; produção deve usar domínio próprio |

## Rotação de segredos

Regra geral: gerar o novo valor, gravar com `scripts/set-secret.sh <segredo>`, subir o número
da versão em `local.pinned_secret_versions` (`terraform/main.tf`), `terraform apply`, conferir
o serviço e só então **desativar** (não destruir) a versão antiga:
`gcloud secrets versions disable <n> --secret=<segredo> --project=lucro-mei`.

| Segredo | Periodicidade | Como gerar / observações |
| --- | --- | --- |
| `lucromei-whatsapp-access-token` | a cada 90 dias ou se vazar | token de System User no Business Manager (permissões `whatsapp_business_messaging`, `whatsapp_business_management`); revogar o antigo na Meta |
| `lucromei-whatsapp-app-secret` | se vazar | "Redefinir" em Configurações do app > Básico; trocar e aplicar logo, porque webhooks assinados com o segredo antigo passam a falhar |
| `lucromei-whatsapp-verify-token` | se vazar | `openssl rand -hex 32`; atualizar também no painel de Webhooks da Meta |
| `lucromei-internal-api-key` | a cada 90 dias | `openssl rand -hex 32` |
| `lucromei-test-database-url` / `lucromei-test-migrate-database-url` | a cada 90 dias ou se vazar | trocar a senha do papel no console do Neon (Roles > Reset password) e gravar a URL nova; o job de migração usa a chave `LUCROMEI_MIGRATE_DATABASE_URL` de `pinned_secret_versions` |
| `lucromei-privacy-hash-key` | **não rotacionar** | as provas de exclusão já gravadas dependem dela; se vazar, as provas continuam sem revelar números, mas registre o incidente |
| `lucromei-sentry-dsn` | se vazar | gerar nova chave no Sentry |
| chaves da Anthropic/OpenAI/Stripe | a cada 90 dias | criar nova no painel do provedor, gravar, aplicar, revogar a antiga |

## Pendências do dono (Fase 2, item 2)

1. **Senha do dono do Neon de teste**: no console do Neon, projeto de teste > Roles > dono >
   Reset password. Monte a URL nova no mesmo formato da atual e grave:
   `scripts/set-secret.sh lucromei-test-migrate-database-url`. Depois suba a versão em
   `local.pinned_secret_versions` (`LUCROMEI_MIGRATE_DATABASE_URL = "2"`) e aplique.
2. **Token antigo do WhatsApp**: no Business Manager > Usuários do sistema, revogue os tokens
   que não são o atual (versão 3 do segredo).
