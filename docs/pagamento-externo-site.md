# Pagamento de pedido do site recebido fora do site

Implementado a pedido do dono em 13/09/2026. Usar quando o cliente criou
um pedido no site, mas pagou por Pix ou transferência diretamente para
a conta da padaria, fora do QR Code do pedido.

## Como operar

1. Entre com a conta do dono e abra **Pedidos do site**
   (`/admin/loja-online/pedidos`). Busque pelo cliente ou código e abra o pedido.
2. No cartão **Confirmar pagamento recebido fora do site**, confira o pedido
   e o total. O valor recebido precisa ser exatamente o total do pedido.
3. Preencha **Referência ou observação do recebimento** (até 200 caracteres),
   com uma informação que permita localizar o pagamento no extrato.
4. Confira o recebimento na conta da padaria e marque a confirmação na tela.
5. Clique **Confirmar recebimento e marcar como pago**. O pedido passa ao
   fluxo normal de preparação e entrega.

O cartão aparece somente para o dono e para pedido aguardando pagamento ou
cancelado por Pix expirado, sem pagamento confirmado. No caso de Pix
expirado, a tela avisa que a confirmação reabre o pedido como pago. Pedidos
já pagos, divulgações e cancelamentos por outros motivos não são elegíveis.

## Histórico e devoluções

O cartão **Pagamentos** identifica o método **Externo (Pix/transferência
direta)** e mostra o nome do dono que confirmou, data, horário, valor e
referência. Tentativas anteriores pelo Pagar.me continuam no histórico.

Uma eventual devolução desse dinheiro deve ser feita fora do site. Este
fluxo não implementa reembolso externo, cancelamento de pedido pago
externamente nem redução de itens com estorno. Os botões de reembolso
pelo Pagar.me ficam indisponíveis para esse recebimento.

## Implementação

- POST `/admin/loja-online/pedidos/<codigo>/confirmar-pagamento-externo`,
  protegido por `owner_required` e CSRF. Preserva o modo `embed` do painel
  de entregas ao voltar para o detalhe.
- Serviço `app/services/pagamento_externo.py`: valida permissão,
  elegibilidade, valor, referência e confirmação; registra o recebimento
  e executa o fluxo normal de pagamento confirmado.
- `PagamentoExternoOnline`: registro de auditoria vinculado ao pedido,
  pagamento e usuário, com data, referência e valor.
- `PagamentoOnline.metodo = 'externo'`: pagamento confirmado sem criar
  IDs fictícios ou cobranças no Pagar.me.
- Tela em `app/templates/admin/loja_online_pedido_detalhe.html`; orientação
  também no Manual de operação, seção **Quando precisar**.

Validar usando pedidos de teste. A publicação da funcionalidade não
confirma o recebimento de nenhum pedido real.
