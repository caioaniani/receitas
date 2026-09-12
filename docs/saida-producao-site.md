# Baixa das encomendas do site na saída da produção

Compras pagas de itens marcados **Sob encomenda** descontam o estoque da
indústria quando o motorista confirma o início da rota. Na Lalamove, a baixa
ocorre na confirmação de coleta (`PICKED_UP`); contratar a corrida ainda não
é coleta. Marcar o pedido como **A caminho** no detalhe administrativo também
confirma a saída. A confirmação de **Entregue** cobre pedidos que não tiveram
uma confirmação anterior de saída, inclusive retirada pelo cliente.

Para menu configurável, o sistema usa a composição escolhida e gravada no
pedido, multiplicada pela quantidade comprada. Duas cestas com 10 minis de
um sabor descontam 20 unidades desse sabor. Alterar a pré-seleção do menu
depois da compra não altera essa baixa.

Cada item recebe um registro permanente com origem, horário, componentes,
quantidade descontada e eventual falta. Repetir o clique ou receber de novo
o webhook não desconta novamente. Uma composição sem vínculo de estoque
impede a confirmação; ela precisa ser corrigida no cadastro. Saldo insuficiente
fica registrado no histórico como **Saída site direto sem estoque**, sem
deixar saldo negativo nem cobrar a falta de novo numa confirmação repetida.

Os itens despachados saem da fila e do pré-preparo do padeiro e deixam de
contar como demanda firme. O estoque da loja continua fora da baixa das
compras sob encomenda. Itens comuns e brindes mantêm seus fluxos próprios.

O acerto manual de despacho direto continua servindo para eventos: ignora
os componentes dos itens já baixados automaticamente e trata os itens comuns
restantes do pedido. Um pedido já acertado manualmente não é baixado de novo
na saída automática. Reembolso depois da saída não significa devolução física
e não repõe estoque; a redução de itens também fica bloqueada após a saída.

Não há reprocessamento automático do histórico. Pedidos que já estavam
entregues e a ferramenta de sincronização de status antigos não geram uma
nova saída. Divergências antigas precisam ser conferidas antes de um acerto.

## Implementação e validação

`SaidaProducaoSite` é uma tabela nova, criada pelo `db.create_all()` no startup,
sem alteração de colunas existentes. A chave por item do pedido impede
duplicidade. O serviço participa da transação de confirmação e não faz commit.
No PostgreSQL, compartilha o advisory lock 7757 com o acerto manual e relê o
pedido e os saldos sob `FOR UPDATE`.

Os testes cobrem composição escolhida, quantidade de cestas, baixa na coleta,
Lalamove, repetição, falta de saldo, rollback, acerto manual nas duas ordens,
pedido misto, reembolso, itens órfãos e retirada da demanda do padeiro.
