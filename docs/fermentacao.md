# Lista diária de fermentação

Decisão do owner em 23/09/2026: envio às 12h de Brasília, para amanhã,
no canal já configurado em `SLACK_CANAL_COPILOT` (desperdício).

Somente Ribeiro do Vale e Anésio Pinto Rosa. Tradicional simples inclui os
nomes exatos Croissant Francês/Tradicional (SKU 272) e Croissant Esquentado
(355). Pain au Chocolat: SKU 271. Não inclui recheados, sanduíches, minis,
bicolor, Nebraska, pedidos para indústria nem consumo de ingredientes.

Quantidade = teto da soma das vendas das últimas três ocorrências do mesmo
dia da semana / 3. Exemplo: 24/09/2026 usa 03, 10 e 17/09. Zero só é válido
quando há histórico fechado da loja naquele dia. Não desconta estoque.

Fonte: `VendaSeruDiaria`, com vínculo confirmado de `SeruLojaMap`. O serviço
lê o histórico existente, sem recapturar ou alterar estoque. Datas sem
totais/itens ou com captura anterior ao fechamento, vínculos divergentes,
quantidades inválidas e vendas sem itens detalhados geram aviso no Slack,
nunca uma ordem parcial de preparo. O timestamp é uma guarda contra
histórico intradia, não garantia de completude da API externa.

Conferência do owner: `/admin/slack/fermentacao`, também acessível pelo
diagnóstico Slack. Mostra a prévia, as três observações e o resultado do
envio. O botão manual usa a mesma deduplicação do cron.

`FermentacaoEnvio` é uma tabela nova criada no startup por `db.create_all`.
Guarda a mensagem, datas, linhas de origem, médias, destino e confirmação
do Slack. Trava PostgreSQL 7767 + chave única data-alvo impedem repetição.
Reserva persistida antes da rede; timeout/crash deixa envio incerto e
exige conferir o canal antes de qualquer recuperação deliberada.

Job `slack-fermentacao` usa o agendador existente (`SERU_AUTO_SYNC`), com
proteção de instância canônica do Slack. Nenhuma automação do Codex é
necessária para a operação: o envio funciona no servidor de produção.
