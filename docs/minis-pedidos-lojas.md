# Minis e pedidos das lojas

Decisão do owner, 24/09/2026: as lojas não fazem pedidos de minis para
recebimento da indústria pelo motorista. Minis de padaria pertencem às
encomendas de clientes.

A regra compartilhada está em `app/services/pedido_loja_catalogo.py` e
considera o nome/categoria da receita ou produto, além dos componentes de
caixas e cestas. Mini manteigas, mini potes de mel e matérias-primas não
são classificados como minis de padaria.

## Onde a regra vale

- Busca de itens, criação e edição dos pedidos das lojas.
- Sugestões por vendas/médias, grade semanal e geração automática.
- Pedidos feitos pelo assistente, incluindo IDs de uma seleção antiga.
- Junção/adoção de pedidos e confirmação/separação para expedição.
- Atribuição do motorista, geração do QR de saída e coleta de um QR antigo.

O bloqueio acontece antes da alteração do pedido ou da baixa do estoque.
Uma edição pode remover o mini, inclusive informando quantidade zero,
desde que mantenha ao menos um item permitido e respeite o prazo de edição.
Uma tentativa recusada apresenta os itens bloqueados, sem salvar metade da
operação. Pedidos antigos com minis precisam de correção antes da saída.

Não há exclusão automática de pedidos históricos, alteração de saldos ou
exceção ao corte de pedidos. O recebimento de pedidos já em transporte
continua disponível. Catálogo, encomendas do site, B2B e produção de minis
para clientes mantêm seu fluxo próprio.

## Verificação

As regressões locais cobrem bloqueio de receitas e caixas compostas,
preservação de acompanhamentos, edição atômica, geração automática, estoque
inalterado na recusa de coleta, junção de duplicatas e encomendas de clientes.
Testes privados não são publicados, conforme orientação do owner.
