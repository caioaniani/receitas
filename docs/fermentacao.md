# Lista diária de fermentação

Decisão do owner em 23/09/2026: envio às 12h de Brasília, para amanhã,
no canal já configurado em `SLACK_CANAL_COPILOT` (desperdício).

Somente Ribeiro do Vale e Anésio Pinto Rosa. A mensagem contém apenas
Croissant Tradicional e Pain au Chocolat. Inclui o consumo desses dois itens
nas vendas simples, recheados, sanduíches e preparações na chapa, conforme
esclarecimento do owner em 24/09/2026. Nebraska e pedidos para indústria
ficam fora. Minis/bicolor não viram tradicional por terem massa em comum.

`VendaMapa` identifica o item por nome (SKU é conferência, não classificação).
O fator do mapa é aplicado uma vez. `ProdutoItem` e sub-receitas são expandidos
recursivamente até os dois alvos, respeitando o rendimento, com quantidades
em Decimal e sem arredondamento intermediário. Regra explícita do owner em
24/09/2026: Almond fica fora; Nutella e Nutella com morango geram tradicional
para fermentar mesmo quando sua composição usa tradicional de retorno.
Essa exceção é exclusiva da lista de fermentação: não altera ficha, estoque
nem regra da indústria. Outros retornos continuam sem nova fermentação.
O catálogo usado é o atual,
com identificação do mapa, fator e caminho da composição gravados na auditoria.
Vínculos relevantes ausentes, ciclos, componentes órfãos ou quantidades
inválidas impedem o envio de uma lista parcial.

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
diagnóstico Slack. Mostra a prévia, as três observações, os produtos vendidos
e suas contribuições, e o resultado do envio. O botão manual usa a mesma
deduplicação do cron. O owner pode corrigir uma mensagem confirmada: atualiza
o mesmo canal/ts, preservando mensagem e cálculo anteriores. Uma correção
sem resposta confirmada mantém a tentativa persistida; o retry explícito
repete o mesmo texto no mesmo ts, sem criar uma segunda instrução.

`FermentacaoEnvio` é uma tabela nova criada no startup por `db.create_all`.
Guarda a mensagem, datas, linhas de origem, médias, destino e confirmação
do Slack. Trava PostgreSQL 7767 + chave única data-alvo impedem repetição.
Reserva persistida antes da rede; timeout/crash deixa envio incerto e
exige conferir o canal antes de qualquer recuperação deliberada.

Job `slack-fermentacao` usa o agendador existente (`SERU_AUTO_SYNC`), com
proteção de instância canônica do Slack. Nenhuma automação do Codex é
necessária para a operação: o envio funciona no servidor de produção.
