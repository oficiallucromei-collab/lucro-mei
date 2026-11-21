# Material para a revisão do app pela Meta

Necessário para usar um número oficial em produção e sair do modo de desenvolvimento. Quem envia
é o dono, no painel do app (developers.facebook.com) e no Business Manager.

## Pré-requisitos

- Verificação do negócio (Business Verification) concluída no Business Manager.
- Nome de exibição do número aprovado ("LucroMEI").
- URLs públicas (troque pelo domínio de produção quando existir):
  - Política de privacidade: `https://lucromei-test-vn7ehb54iq-rj.a.run.app/privacidade`
  - Termos de uso: `https://lucromei-test-vn7ehb54iq-rj.a.run.app/termos`
  - Instruções de exclusão de dados: `https://lucromei-test-vn7ehb54iq-rj.a.run.app/exclusao-de-dados`
- Ícone 1024×1024 e categoria do app ("Negócios e páginas" / "Produtividade").

## Permissões pedidas e justificativa

| Permissão | Para quê |
| --- | --- |
| `whatsapp_business_messaging` | receber as mensagens que o usuário manda ao LucroMEI e responder com a confirmação do lançamento, perguntas de esclarecimento, resumos e avisos da conta (templates aprovados) |
| `whatsapp_business_management` | ler e gerenciar o número e os templates da própria conta do LucroMEI |

Texto sugerido para "Como o app usa esta permissão" (`whatsapp_business_messaging`):

> O LucroMEI é um assistente financeiro para microempreendedores. O usuário inicia a conversa
> pelo WhatsApp e manda mensagens como "Recebi 150 por um corte". O app grava o lançamento e
> responde com a confirmação. Também responde a pedidos de resumo ("resumo do mês"), a comandos
> de privacidade (MEUS DADOS, APAGAR MEUS DADOS) e envia avisos sobre a conta, como o fim do
> teste grátis, usando templates aprovados. Não enviamos mensagens para quem não iniciou a
> conversa nem marketing para terceiros.

## Roteiro do vídeo (screencast, 2 a 3 minutos)

1. Celular com o WhatsApp aberto na conversa com o número do LucroMEI.
2. Mandar "Oi" e concluir o cadastro (nome, tipo de negócio, ACEITO), mostrando o link dos termos.
3. Mandar "Recebi 150 por um corte" e mostrar a confirmação.
4. Mandar "Paguei 300 para o fornecedor", responder "1" e mostrar a confirmação.
5. Mandar "resumo do mês".
6. Mandar "MEUS DADOS" e depois "APAGAR MEUS DADOS" + "APAGAR", mostrando o prazo de exclusão, e
   "CANCELAR EXCLUSÃO".
7. Mostrar no navegador as páginas de privacidade e de exclusão de dados.

## Dados tratados (questionário de uso de dados)

- Número do WhatsApp, nome informado, tipo de negócio, texto das mensagens (até 90 dias),
  lançamentos financeiros do próprio negócio.
- Não há venda nem compartilhamento para publicidade. Operadores: Meta, Google Cloud, Neon,
  Anthropic (só texto da mensagem, quando a IA é usada), OpenAI (só áudio, quando ligado).
- Exclusão: pelo próprio WhatsApp, com 7 dias de carência; detalhes em `/exclusao-de-dados`.
