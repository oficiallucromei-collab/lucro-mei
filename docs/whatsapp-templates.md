# Templates do WhatsApp (Meta)

Mensagens fora da janela de 24 horas (fim do teste grátis, lembretes, confirmação de pagamento)
só são entregues com um template aprovado pela Meta. O código (`src/lucromei/templates.py`) já
envia por template; falta cadastrar e aprovar cada um no WhatsApp Manager. Até lá, deixe
`LUCROMEI_NOTIFICATIONS_ENABLED=false` (padrão): nenhum aviso proativo é enviado.

## Como cadastrar (dono da conta Meta)

1. Abra o [WhatsApp Manager](https://business.facebook.com/wa/manage/message-templates/) da conta
   do LucroMEI e escolha **Criar modelo**.
2. Para cada template abaixo: categoria **Utilidade**, idioma **Português (BR)**, nome exatamente
   como está, tipo de variável **Número**, corpo copiado sem mudar uma vírgula e os exemplos
   indicados. Sem cabeçalho, rodapé ou botões.
3. Envie para análise. A aprovação costuma sair em minutos ou poucas horas.
4. Depois que todos estiverem **Ativos**, ligue os avisos no serviço de teste:
   `LUCROMEI_NOTIFICATIONS_ENABLED=true` (e, se quiser lembretes, `LUCROMEI_REMINDERS_ENABLED=true`).
   Em Terraform: variável `notifications_enabled`.

Se a Meta mudar a categoria para Marketing, o envio continua funcionando, mas com outro preço e
sujeito ao opt-out de marketing do usuário; nesse caso, avise antes de ligar os lembretes.

## Templates

### `lucromei_teste_terminando`

- Categoria: UTILITY
- Idioma: pt_BR
- Exemplos: {{1}} = Ana, {{2}} = 04/12/2026

```text
Olá, {{1}}! Seu teste grátis do LucroMEI termina em {{2}}. Depois disso, para continuar anotando suas entradas e despesas, responda ASSINAR. Seus lançamentos continuam guardados.
```

### `lucromei_teste_encerrado`

- Categoria: UTILITY
- Idioma: pt_BR
- Exemplos: {{1}} = Ana, {{2}} = 04/12/2026

```text
Olá, {{1}}! Seu teste grátis do LucroMEI terminou em {{2}}. Seus lançamentos continuam guardados e você ainda pode pedir resumos. Para voltar a anotar, responda ASSINAR.
```

### `lucromei_lembrete_lancamentos`

- Categoria: UTILITY
- Idioma: pt_BR
- Exemplos: {{1}} = Ana, {{2}} = 7

```text
Olá, {{1}}! Faz {{2}} dias que você não anota nenhuma movimentação no LucroMEI. Para anotar, é só mandar, por exemplo: Recebi 150 por um corte. Para não receber mais lembretes, responda PARAR LEMBRETES.
```

### `lucromei_assinatura_ativa`

- Categoria: UTILITY
- Idioma: pt_BR
- Exemplos: {{1}} = 04/01/2027

```text
Pagamento confirmado! Sua assinatura do LucroMEI está ativa até {{1}}. Pode continuar mandando suas movimentações por aqui.
```

## Quando cada um é usado

| Template | Quando | Chave de deduplicação |
| --- | --- | --- |
| `lucromei_teste_terminando` | até 48 h antes do fim do teste, sem assinatura ativa | `notice:<negócio>:trial_ending` |
| `lucromei_teste_encerrado` | até 3 dias depois do fim do teste, sem assinatura ativa | `notice:<negócio>:trial_ended` |
| `lucromei_lembrete_lancamentos` | `LUCROMEI_REMINDERS_ENABLED=true`, sem lançamentos há 7 dias, sem PARAR LEMBRETES | `notice:<negócio>:reminder:<data>` |
| `lucromei_assinatura_ativa` | pagamento confirmado pelo provedor (Fase 3) | por evento do provedor |

Os avisos saem só entre 9h e 20h (horário de Brasília), nunca para contas com exclusão pedida,
e no máximo uma vez por chave. Dentro da janela de 24 horas o mesmo texto vai como mensagem
comum (sem custo de template).
