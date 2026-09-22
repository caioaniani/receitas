# Correções do motor de pedidos — 22/09/2026

A auditoria encontrou divergências entre saldo atual, pedidos sugeridos, previsão da indústria e regras cadastradas. As correções tratam os seguintes casos:

- A projeção desconta apenas o consumo ainda esperado para hoje. Vendas líquidas e perdas já registradas não são descontadas novamente; o histórico de médias continua encerrando ontem.
- Pedidos recebidos ficam visíveis na grade, mas não somam novamente ao estoque. Entregas pendentes de fornadas especiais em dias úteis abastecem o saldo do fim de semana.
- Balanço, cronograma e detalhamento usam o histórico das lojas ativas e abertas no dia de entrega previsto. A restrição inclui a média residual; pedidos firmes excepcionais permanecem válidos. Estornos de outra loja não apagam consumo positivo.
- A automação e as ações humanas sobre pedidos usam a mesma trava transacional por loja, com aquisição em ordem. Proteção e gravação pertencem à mesma transação, inclusive limpeza de rascunhos zerados.
- Atualizações de produção das 06h45/19h05 compartilham o tratamento de falhas do envio do meio-dia. Falha desfaz o dia, mantém o status visível, permite dias independentes e bloqueia dependências de preparos sem confirmação.
- Produtos simples ativos entram no cálculo, na grade e nos pedidos com identidade própria, incluindo reservas, mínimos e reposição diária. Cestas são repostas pelos componentes; a tela explica essa regra e impede salvar configurações sem efeito no pai. Valores antigos ficam preservados.
- Duplicar receita preserva regras operacionais, ingredientes e etapas. A nova cópia permanece fora da vitrine até revisão.

## Validação

Regressões cobrem saldo líquido de hoje e estornos, entrega recebida/pendente, fornada especial, calendário, demanda firme, identidade de Produtos, criação/edição/remoção pela grade, proteção de pedidos humanos, recuperação de ordens e cópia das regras de receitas. Testes de concorrência usam duas sessões e aquisição de trava simulada; a trava PostgreSQL reutiliza o mecanismo existente do estoque.

Sem mudança de schema. Os efeitos seguem as próximas rodadas normais; a publicação não recalcula retroativamente pedidos humanos nem ordens em execução hoje. Uma revisão dos registros reais é necessária para identificar eventuais efeitos anteriores à correção.
