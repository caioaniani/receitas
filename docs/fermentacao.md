# Lista diária de fermentação

Decisão do owner em 23/09/2026: envio às 12h de Brasília, para amanhã,
com destinos separados por loja desde a decisão de 26/09/2026:

- Anésio Pinto Rosa: `SLACK_CANAL_FERMENTACAO_ANESIO`, padrão `C09C7P4KJD6`.
- Ribeiro do Vale: `SLACK_CANAL_FERMENTACAO_RIBEIRO`, padrão `C09BD3S3FTP`.

Cada canal recebe somente a lista de sua loja. O desperdício continua usando
`SLACK_CANAL_COPILOT`. Os novos destinos têm os IDs acima como padrão de produção,
e podem ser substituídos por configuração. Canal vazio impede o envio daquela loja;
não há redirecionamento para Copilot. O bot precisa ter acesso aos canais.

Somente Ribeiro do Vale e Anésio Pinto Rosa. A mensagem contém apenas
Croissant Tradicional e Pain au Chocolat. Inclui o consumo desses dois itens
nas vendas simples, recheados, sanduíches e preparações na chapa, conforme
esclarecimento do owner em 24/09/2026. Nebraska e pedidos para indústria
ficam fora. Minis/bicolor não viram tradicional por terem massa em comum.

`VendaMapa` identifica o item por canal e nome. O SKU antigo do vínculo pode
diferir do SKU do snapshot do PDV; ambos são auditados, sem classificar por
código nem bloquear o vínculo nominal por essa diferença histórica.
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

Decisão do owner em 25/09/2026, para Ribeiro do Vale: usar as últimas sete
ocorrências do mesmo dia da semana, ordenar o consumo de cada produto do
maior para o menor e calcular `teto((maior + quarto maior) / 2)`. Vale tanto
para croissant tradicional quanto para pain au chocolat, com ordenação
independente. Empates contam como observações distintas. Exemplo:
`[60, 57, 52, 51, 43, 41, 40]` resulta em `teto(55,5) = 56`.
Para sábado 26/09/2026, usar os sábados 08, 15, 22 e 29/08 e 05, 12 e 19/09.
Anésio Pinto Rosa mantém a média das últimas três ocorrências do mesmo dia
da semana, arredondada para cima. A mudança não alcança Nebraska, pedidos
das lojas ou ordens de produção industrial.
Zero só é válido quando há histórico fechado da loja naquele dia. Não
substituir dias ausentes por zero nem por outras datas. Não desconta estoque.

Correção em 27/09/2026: para um dia-alvo normal, excluir feriados da referência
histórica e buscar ocorrências anteriores do mesmo dia até completar sete ou
três datas. O calendário da cidade de São Paulo inclui feriados nacionais,
estaduais e municipais recorrentes, fixos e móveis (Paixão de Cristo e Corpus
Christi). Fonte: https://clic.prefeitura.sp.gov.br/calendario . Não inclui
pontos facultativos, emendas, recessos ou datas comerciais. Não altera outros
motores nem usa o calendário de entregas do site como calendário de feriados.
As exclusões ficam na auditoria e na mensagem. Se a data substituta não tiver
histórico fechado, o cálculo continua bloqueado; não reduz o denominador.
Para 28/09/2026, sai 07/09 (Independência do Brasil); entram 03/08 na Ribeiro e
31/08 na Anésio. As fórmulas e as composições dos produtos são preservadas.
Se o próprio alvo for feriado, mantém a seleção semanal anterior, com aviso
explícito de que a quantidade não tem ajuste específico de feriado. Uma regra
para comparar feriados entre si ainda não foi definida pelo owner.
Registros já persistidos mantêm a auditoria original; a atualização de uma
mensagem confirmada usa “Corrigir a mensagem no Slack”, no mesmo canal/ts.

Fonte: `VendaSeruDiaria`, com vínculo confirmado de `SeruLojaMap`. O serviço
lê o histórico existente, sem recapturar ou alterar estoque. Datas sem
totais/itens ou com captura anterior ao fechamento, vínculos divergentes,
quantidades inválidas e vendas sem itens detalhados geram aviso no Slack,
nunca uma ordem parcial de preparo. O timestamp é uma guarda contra
histórico intradia, não garantia de completude da API externa.

Conferência do owner: `/admin/slack/fermentacao`, também acessível pelo
diagnóstico Slack. Mostra a prévia, as observações de cada loja, os produtos vendidos
e suas contribuições, e o resultado do envio. O botão manual usa a mesma
deduplicação do cron. O owner pode corrigir uma mensagem confirmada: atualiza
o mesmo canal/ts, preservando mensagem e cálculo anteriores. Uma correção
sem resposta confirmada mantém a tentativa persistida; o retry explícito
repete o mesmo texto no mesmo ts, sem criar uma segunda instrução.

`FermentacaoEnvio` preserva os envios unificados anteriores. Se já houver uma
tentativa para a data nesse histórico (inclusive incerta), não se publica outra
lista nos novos canais. A transição vale para os próximos disparos.
`FermentacaoEnvioLoja` é uma tabela nova criada no startup por `db.create_all`,
serializado pela trava de schema existente, sem ALTER da tabela anterior.
Guarda a mensagem, datas, linhas de origem, método por loja, consumos ordenados,
maior/quarto maior quando aplicável, referência antes do arredondamento, destino e confirmação
do Slack. Trava PostgreSQL 7767 + chave única (data-alvo, loja) impedem repetição mesmo se o canal mudar.
Reserva persistida antes da rede; timeout/crash deixa envio incerto e
exige conferir o canal antes de qualquer recuperação deliberada.

Job `slack-fermentacao` usa o agendador existente (`SERU_AUTO_SYNC`), com
proteção de instância canônica do Slack. Nenhuma automação do Codex é
necessária para a operação: o envio funciona no servidor de produção.

Cada tentativa é reservada antes de chamar Slack, com confirmação independente.
Falha ou timeout em uma loja não impede enviar à outra. Uma reserva incerta nunca
é reenviada automaticamente. A correção usa o canal e o ts confirmados daquela
loja, conserva o histórico e não muda as instruções da outra loja. A tela mostra
os destinos configurados e o canal/estado efetivo de cada envio.


## Conferência e recuperação do Slack (27/09/2026)

Salvar uma tentativa não é confirmação de publicação. A tela usa “Mensagem enviada”
apenas para estados confirmados; tentativas ambíguas aparecem como “Envio não confirmado”.
Rejeições explícitas conhecidas do Slack ficam em `falhou`, com código sanitizado
na auditoria JSON `_entrega`; timeout, erros internos e respostas desconhecidas
continuam em `incerto`. Nenhuma resposta bruta ou credencial é exibida.

O owner pode “Verificar e recuperar envio” individualmente. Rejeição explícita
permite uma nova tentativa deliberada depois de corrigir o acesso. Para tentativas
ambíguas, inclusive as anteriores a esta melhoria, é obrigatória uma leitura
completa do histórico pelo próprio bot: texto e autor iguais identificam a mensagem
já existente, sem publicar de novo. Falhas de consulta, paginação incompleta, mais
de uma correspondência ou ausência de permissão bloqueiam o reenvio. Tentativas
recentes aguardam cinco minutos. Só a ausência confirmada ou uma recusa explícita
permite publicar novamente. Desde o ajuste de feriados, confere o cálculo atual
antes dessa nova postagem e preserva texto/cálculo antigos em `historico_recalculos`.
Histórico atual incompleto bloqueia reenvio de uma lista antiga. A consulta procura
sempre o texto original; se ele já existe, recupera o ts e a correção segue pelo
botão existente, atualizando a mesma mensagem. Reserva a tentativa antes da rede.

O envio normal do cron continua sem repetir tentativas persistidas. O link “Abrir
mensagem no Slack” é obtido pela API apenas para um canal/ts confirmado, com acesso
restrito ao owner e URL HTTPS do Slack validada. Confirmar que o aplicativo está
nos canais de destino faz parte da configuração; mudar o ID não concede acesso.
