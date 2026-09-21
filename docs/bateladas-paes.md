# Bateladas de pães por sabor

Decisão do proprietário, 16/09/2026: sourdough em bateladas completas de
25.000 g de farinha por sabor; pão francês em bateladas de 12.000 g. Não
agrupar os novos itens em massa-base. Não alterar brioche, preparos auxiliares,
montagens ou retornos. Para viennoiserie, aplica-se a extensão de 17/09/2026
documentada em [bateladas-viennoiserie.md](bateladas-viennoiserie.md).

## Planejamento e pesagem

- A farinha é a soma das farinhas diretas da ficha. Levain e grãos são
  insumos adicionais e mantêm suas proporções. A farinha interna do levain
  não é descontada dos 25/12 kg.
- O rendimento usa a massa crua e o peso por pão, sem descontar perda de
  forno. Unidades inteiras são arredondadas para baixo; ingredientes nunca
  são reduzidos para acompanhar esse arredondamento.
- Uma necessidade positiva é completada por bateladas inteiras. O excedente
  projetado cobre dias posteriores do mesmo sabor, sem criar estoque físico.
- O piso diário existente continua sendo um mínimo, completado por bateladas;
  tetos automáticos não cortam uma batelada. Impedimentos aparecem na grade.
- A ficha do padeiro mostra ingredientes **por batelada**, quantas repetições
  fazer e o total de farinha. Não pesar todos os lotes em um único batimento.
- Avisos de capacidade comparam a massa final, incluindo levain, ao equipamento.
  Tempos e temperaturas cadastrados não são inventados nem ajustados.

## Ordens e estoque

`PlanejamentoItemBatelada` é uma tabela nova de snapshots: preserva a ficha,
os IDs dos ingredientes, rendimento, etapas e prazo aprovados. Não adiciona
colunas a tabelas antigas. Startup cria a tabela pelo caminho existente.

Ordens já enviadas sem snapshot continuam legadas. GET não converte ordens;
o envio automático preserva as travas do dia corrente e por antecedência dos
insumos. Novos itens e rascunhos entram no padrão na criação/envio.

Somente a confirmação credita pães no estoque. Confirmações parciais consomem
a proporção correspondente da ficha congelada, por deltas acumulados (precisão
de seis casas do controle de insumos), sem arredondar cada confirmação para
outra batelada. Trata-se de consumo estimado pela quantidade confirmada, não
de medição independente da farinha efetivamente pesada.

Na conversão, libera-se primeiro a reserva da fração confirmada, depois
registra-se o consumo, na mesma transação. Nas ordens com bateladas, o saldo
disponível de MP é assinado: negativo significa déficit depois das reservas.
Truncá-lo em zero fabricaria saldo ao liberar a reserva. Cancelamento devolve
exatamente o reservado. Histórico com produção não pode ser excluído.

Produção avulsa desses pães deve usar uma ordem. A lista geral não permite
baixar ingredientes novamente de uma ordem do cronograma. Planos manuais
padronizados confirmam produção pelo mesmo serviço de estoque e insumos.

No botão **Produção extra** da TV, um pão que não está pendente na ordem de
hoje ganha uma ordem manual concluída automaticamente. O padeiro informa a
quantidade real: 220 unidades creditam 220, mesmo que a ficha estime 237 por
batelada. A diferença não vira produção futura. O snapshot e o consumo
proporcional ficam registrados; um recibo por envio impede duplicação na
repetição após falha de rede. Se houver ordem do dia em aberto para o pão,
a tela encaminha para registrar nela. Massa compartilhada continua seguindo
o planejamento semanal, pois exige distribuir o batimento entre derivados.

## Verificação e retorno

Testes cobrem fórmulas reais, tipos de ingrediente, cálculo semanal, piso,
overrides, ordens congeladas, MRP, reserva, confirmações parciais, histórico,
pesagem e etapas do padeiro. A publicação não recalcula ordens diretamente,
não lança produção e não confirma entregas.

Não reverter somente o código enquanto houver ordens com snapshot em aberto:
o código antigo não entende seu consumo congelado. Em incidente, preservar
a tabela e corrigir mantendo o leitor dos snapshots; avaliar explicitamente
as ordens abertas antes de qualquer retorno à regra anterior.
