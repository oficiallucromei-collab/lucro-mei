# Backups e recuperação

## O que existe

O banco é PostgreSQL no Neon. O Neon guarda o histórico de alterações (WAL) e permite
**restaurar a qualquer ponto no tempo** dentro da janela de histórico do plano (no plano
gratuito, poucas horas; nos planos pagos, de 7 a 30 dias, configurável em Settings > Storage >
History retention). Não há outra cópia: confirme a janela no console do Neon e, para produção,
use um plano com pelo menos 7 dias.

- **RPO** (quanto se pode perder): segundos, dentro da janela.
- **RTO** (tempo para voltar): cerca de 30 minutos, seguindo o procedimento abaixo.

## Restaurar (incidente real)

1. Pare a escrita: no painel do app na Meta, em Webhooks, desmarque o campo `messages`. A Meta
   guarda as mensagens e reenvia por até 7 dias.
2. No console do Neon: Branches > `main` > **Restore** > escolha o horário anterior ao problema.
   O Neon guarda o estado atual num branch de backup automático.
3. Rode `reapply-deletions` (regra da LGPD, abaixo):
   `gcloud run jobs execute lucromei-ops --region=southamerica-east1 --wait --args=reapply-deletions`
4. Rode `pilot-check` e confira contagens.
5. Religue o webhook na Meta. As mensagens represadas chegam e são deduplicadas pelo ID.

## Teste de restauração (fazer a cada trimestre, sem mexer no `main`)

1. No Neon, crie um branch a partir de um ponto no tempo de ontem (Branches > New branch >
   "Point in time"). Copie a connection string do papel de runtime para esse branch.
2. Grave num segredo temporário: crie `lucromei-restore-test-url` e rode
   `scripts/set-secret.sh lucromei-restore-test-url`.
3. Execute o job de ops apontando para ele:
   `gcloud run jobs execute lucromei-ops --region=southamerica-east1 --wait --update-secrets=LUCROMEI_DATABASE_URL=lucromei-restore-test-url:latest --args=pilot-check,--hours,48`
   e compare com o mesmo comando no banco atual.
4. Registre aqui a data, o ponto no tempo usado, o tempo gasto e as diferenças encontradas.
5. Apague o branch de teste no Neon e desative a versão do segredo temporário.

| Data | Ponto no tempo | Duração | Resultado |
| --- | --- | --- | --- |
| (pendente: primeiro teste depende do acesso do dono ao console do Neon) | | | |

## LGPD e backups

- Exclusões pedidas pelos usuários são feitas no banco ativo. As cópias do histórico do Neon
  expiram sozinhas no fim da janela (no máximo 30 dias), e a política de privacidade diz isso.
- Depois de **qualquer** restauração, rode `reapply-deletions` antes de reabrir o serviço:
  ele apaga de novo toda conta cujo pedido de exclusão já estava concluído (a tabela
  `deletion_requests` guarda o id do negócio, sem telefone).
- Pedidos que estavam no prazo de carência continuam valendo, porque o próprio pedido volta com
  a restauração; pedidos feitos depois do ponto restaurado se perdem: avise o titular, se for o
  caso, para pedir de novo (ou refaça a partir do registro do incidente).
- Um branch de teste de restauração contém dados pessoais: apague-o no mesmo dia.
