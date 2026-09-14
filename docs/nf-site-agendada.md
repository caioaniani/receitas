# NF dos pedidos do site

Regra definida pelo dono em 14/09/2026: emissão automática uma hora antes
do horário de entrega e envio automático da nota ao cliente.

- Entrega/retirada das 09:00 às 10:00: emissão a partir das 08:00 BRT.
- Kits: cada data tem sua NF, com os produtos e o frete daquela entrega.
- Express, sem hora fixa, entra na fila assim que o pagamento confirma.
- Pagamento confirmado depois do horário programado entra no próximo ciclo.
- Pagamento externo confirmado pelo owner segue a mesma regra.
- Cancelados, pedidos sem pagamento e pedidos de divulgação não emitem por
  esta rotina. Reembolso de kit em andamento impede nova emissão.
- Data/horário ausente ou ilegível gera pendência, sem inventar um horário.

O cron verifica a fila a cada minuto, processa até 25 pendências por ciclo
e depende da autorização do Tiny/SEFAZ. Indisponibilidade ou volume acima
do lote pode atrasar a conclusão. O agendamento não garante autorização no
segundo exato. Falhas certas são retentadas após 5, 10, 20, 40 e até 60 minutos.

Após autorização, o cliente recebe e-mail com link público para o DANFE
(PDF). Falha no envio conserva a NF e retenta apenas o e-mail. Timeout do
provedor de e-mail pode produzir reenvio, pois não confirma se houve aceite.
O painel mostra horário programado, pendência e data do envio confirmado.
O owner pode emitir antes manualmente e o envio também entra na fila.

Inclusão Tiny sem resposta confirmada não é repetida automaticamente: a
tarefa fica visível para o owner conferir no Tiny. No próprio pedido, ele
pode vincular o ID da nota existente ou confirmar explicitamente que não
existe nota/rascunho para liberar nova tentativa. Isso também cobre interrupção do processo durante a criação.
Uma NF autorizada não pode ser recriada pelo botão “Refazer”.

Pedidos pagos em andamento sem tarefa são recuperados automaticamente.
O histórico já entregue sem tarefa não recebe novas notas, e NFs antigas
já autorizadas não provocam reenvio em massa. Tarefas existentes dos kits
preservam seus identificadores, conclusão e comprovante de envio.

A fila usa a tabela existente `tarefa_fiscal_kit`, agora exposta como
`TarefaFiscalPedido`; `TarefaFiscalKit` é alias do mesmo mapper. Não há
ALTER de coluna nem nova migração. O cron conserva o lock global 7765;
NF e envio de DANFE compartilham a trava por pedido 7766. A função canônica
de prazo é `app.services.loja_fiscal.horario_emissao`.
