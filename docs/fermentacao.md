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


## Conferência do envio, aviso ao dono e sonda (03/10/2026)

Caso real: o aplicativo nunca foi adicionado a `#fermentadora-filial`. Desde a
separação por canal (27/09), o Slack recusou a lista da Anésio todo dia
(`not_in_channel`) e a recusa ficou só no registro `falhou` e na tela de admin —
sete listas sem ninguém saber. A matriz só funcionou porque o aplicativo entrou em
`#fermentadora-matriz` em 27/09 às 17:14, quando foi mencionado no canal. No mesmo
período, a lista de domingo da Ribeiro saiu bloqueada por `CROISSANT AZUL`
(vendido em 27/09, sem vínculo no PDV): esse dia bloqueia as listas de domingo
até 22/11 se o produto não for vinculado. “Ignorar” não destrava: nome relevante
(`croiss`, `pain`, `cesta`, `kit`, `box`, `combo`) ou vínculo que leva a croissant
ou pain exige vínculo confirmado. Também: 21/08 com vendas sem itens bloqueia as
listas das sextas 02/10 e 09/10.

- `enviar_amanha()` devolve `lojas` (uma entrada por loja: `loja`, `estado`,
  `mensagem`) e `data_alvo`; a mensagem composta prefixa o nome da loja. Exceção
  inesperada no cálculo de uma loja vira `indisponivel` para ela, com log, sem
  registro (a conferência tenta de novo) e sem impedir a outra loja.
- Conferência `verificar_envio_de_amanha()` às **12:10** e às **16:00**
  (`seru_cron`, jobs `slack-fermentacao-conferencia[-tarde]`, trava 7769).
  Lê o estado GRAVADO, não o retorno em memória do envio:
  1. loja sem nenhuma tentativa registrada (job das 12h perdido em deploy,
     processo morto antes da reserva, erro no cálculo) → mesmo gesto do botão
     “Enviar mensagens pendentes de amanhã” (`enviar_amanha()`; não toca em
     tentativa já registrada, então não reenvia resposta incerta);
  2. toda loja que não ficou `enviado` vira um bloco do WhatsApp ao dono
     (`ZAPI_NUMERO_DESTINO`, senão `ZAPI_BOT_DONO_NUMERO` — mesma ordem do digest
     da Z-API; os outros avisos ao dono não têm fonte única de destino, dívida
     registrada no código): recusa do Slack (texto de `ERROS_ENTREGA` + código e
     nome do canal), envio ou correção não confirmados (com "espere 5 minutos
     após a tentativa"), aviso de cálculo bloqueado (até cinco erros e a dica de
     cada tipo de erro — uma dica por erro, a primeira que casa) ou motivo de não
     ter enviado (canal ou bot não configurado cita a variável; trava presa na
     última rodada orienta reiniciar o serviço), com o gesto da tela. Quando um
     erro do cálculo não se resolve no sistema (venda sem itens, erro interno), o
     gesto é combinar a quantidade com a equipe — a recuperação só publica lista
     completa e a correção não destrava a lista.
  3. na rodada das 16:00, a lista que a própria conferência publicou (loja sem
     tentativa ao meio-dia) vira bloco informativo "publicada com atraso".
  Um aviso por PROBLEMA por dia: claim por loja (`whatsapp.claim_envio`, chave
  `fermentacao_alerta_dono_<loja>`, tick = data-alvo + assinatura do problema:
  estado, código do Slack e erros do cálculo). A rodada das 16:00 não repete o
  que as 12:10 já avisaram e avisa só o problema novo; WhatsApp fora devolve os
  claims e a rodada seguinte tenta de novo; claim que não grava não envia
  (contrato do `claim_envio`). Vai como `critico=True` (o teto/hora o reduziria
  a 60 caracteres num digest); a cópia de homologação é barrada antes, na
  checagem de instância canônica do início. Antes da última rodada, “envio em
  andamento” não avisa: loja sem registro com a trava 7767 ocupada e
  `enviando` de menos de 5 minutos esperam as 16:00 — sem calar a outra loja.
  Exceção dentro da própria conferência vai para o log e, na rodada das 16:00,
  vira aviso próprio (chave `fermentacao_alerta_dono`). O botão manual não
  manda WhatsApp.
- A tela, a sonda, a recuperação e a correção usam `calcular_seguro`: erro
  inesperado aparece como bloqueio com `erro_interno` (nunca página 500); com
  ele, a recuperação não reenvia, a correção não altera a mensagem do Slack e a
  tela não oferece “Corrigir a mensagem” nem a prévia feita do erro.
- Unlock da trava 7767 que falha descarta a conexão (`_soltar_trava`): voltar
  ao pool com a trava presa deixaria todo envio seguinte "em andamento".
- Sonda read-only `GET /api/claude/fermentacao` (`?data=`, `?dias=`,
  `?fontes=1`, `?slack=1`): envios da data com estado, código do Slack e texto,
  histórico por loja, cálculo atual por loja e, com `slack=1`, se o aplicativo é
  membro de cada canal (`slack_entrega.info_canal`, timeout curto).

Limitações conhecidas: sem banco não há claim nem aviso — e o `/health` que o
Uptime Kuma vigia devolve `ok` sem tocar o banco (`app/__init__.py`), então banco
fora só aparece no monitor da loja online (`opao.online`, cuja página depende do
banco; a configuração real do Kuma não é verificável daqui); agendador
parado não roda envio nem conferência, e o heartbeat das 08:00 no Slack só
acusa isso pela AUSÊNCIA da mensagem diária; enquanto o problema persistir, sai
um aviso por dia (ex.: CROISSANT AZUL sem vínculo, todo sábado até 21/11 — o
27/09 entra nas sete ocorrências de domingo até a lista de 22/11, porque o
feriado de 15/11 sai da conta).

Testes: `tests/test_fermentacao_aviso_dono.py`.
