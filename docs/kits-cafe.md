# Kits de café da manhã com entrega em casa

O dono monta cada opção de kit com os itens já disponíveis à venda no site.
O cliente escolhe os dias e horários de entrega: cada dia corresponde a um
kit completo. É uma compra para um mês, sem renovação automática.

## Montar as opções

1. Entre como **owner** e abra **Loja online → Kits de café** na barra lateral.
   O painel da loja online e o catálogo também têm atalhos. Na busca do sistema,
   procure por **criar kit** para ir direto à montagem.
2. Clique em **Criar kit**, informe o nome e uma descrição para o cliente.
3. Selecione as quantidades dos itens por entrega. A unidade, porção, tamanho
   e recheio são os do produto publicado no site. Zero deixa o item fora do kit.
4. Para oferecer sabores, em **Sucos à escolha do cliente** marque de 2 a 10
   produtos publicados. Cada entrega inclui uma unidade do suco escolhido.
   Deixe esses sucos com quantidade zero nos itens fixos para não duplicá-los.
   Em **Escolhas extras do plano**, configure também **Croissant** e
   **Sourdough**, se o plano oferecer essas alternativas. Cada grupo inclui
   uma unidade e aceita de 2 a 10 receitas ou produtos publicados, sem menu
   configurável. Deixe o grupo vazio se ele não fizer parte do plano.
5. Confira o valor por kit e escolha **Salvar e publicar no site**, ou salve
   como rascunho para revisar antes de vender.
6. Repita para criar as três opções. Os nomes e as composições são livres.

A lista destaca nome, situação e preço; **Ver composição e detalhes** abre o
conteúdo e a descrição. A ação **Pausar venda** fica dentro desses detalhes.
No editor, a busca aceita nomes sem acentos e **Só selecionados** filtra os
itens do kit. Os botões −/+ ajustam as quantidades e o resumo mostra o valor
por entrega durante a montagem. No celular, o resumo aparece após as etapas.

O visual usa os tokens do layout administrativo em `kits-cafe-admin.css`,
com estados textuais, controles de toque, foco visível e detalhes acessíveis
por teclado. Os campos e a validação do servidor continuam os mesmos.

O sistema não cadastra alimentos nem faz substituições por semelhança de nome.
Se, por exemplo, o queijo branco ou uma porção de suco ainda não estiver à
venda, regularize o produto no **Catálogo do site** antes de incluí-lo no kit.

### Composição dos três planos solicitados

As quantidades abaixo são **por entrega**, não pelo mês inteiro:

| Plano | Itens fixos | Escolhas do cliente |
| --- | --- | --- |
| 1 | 3 pães franceses, 1 peito de peru de 100 g, 1 mussarela de 100 g, 2 cookies | 1 suco de 1 litro: laranja ou verde |
| 2 | Os mesmos itens fixos do Plano 1 | O mesmo suco + 1 croissant (Almond ou Nutella com morango) + 1 sourdough (tradicional, integral, grãos ou nozes e azeitonas) |
| 3 | Os mesmos itens fixos do Plano 1 + 2 croissants tradicionais, 1 salada de frutas de 300 g, 1 granola de 500 g | As mesmas três escolhas do Plano 2 |

Mussarela de 100 g, peito de peru de 100 g e a salada de frutas de 300 g foram
confirmados pelo dono em 14/09/2026. Vincule os produtos exatos do catálogo. O
cadastro dos planos não é feito automaticamente em produção. As opções
dos Planos 2 e 3 não devem ser incluídas novamente como itens fixos.

Um menu configurável também pode entrar no kit. Sua composição padrão aparece
no editor e fica fixada no kit quando ele é salvo. Se o padrão mudar depois,
o kit conserva a escolha anterior enquanto ela ainda for válida. Para adotar
o novo padrão, o owner marca essa opção explicitamente no editor. O cliente
recebe o kit montado; não escolhe os componentes do menu nesse fluxo.

## Preço, disponibilidade e edição

O preço é calculado no servidor pela soma dos produtos e quantidades do kit,
usando os preços atuais do catálogo. Não há um preço manual separado. O total
da compra soma os kits das datas escolhidas e o frete de cada entrega. O frete
aparece separado e não está incluído no preço anunciado por kit.

Quando há sucos à escolha, o cliente precisa selecionar um deles antes de
continuar. A mesma escolha vale para todas as entregas dessa compra. Se os
preços forem diferentes, a vitrine informa **a partir de** e o resumo calcula
o preço do sabor escolhido. O calendário respeita a antecedência desse suco.
O mesmo vale para croissant e sourdough: é preciso escolher um item em cada
grupo oferecido, e a agenda considera a maior antecedência da combinação.
As escolhas ficam iguais em todas as datas da compra. Não é possível repetir
um mesmo produto entre itens fixos, sucos e grupos, nem ultrapassar 64
combinações entre as alternativas; os três planos acima usam no máximo 16.

O kit só aparece para compra se estiver publicado e todos os itens estiverem
disponíveis no site. Se algum produto sair do catálogo, ficar sem preço,
esgotar ou tiver uma composição de menu incompatível, o kit fica indisponível
para novas compras até a revisão. Não é vendido parcialmente.

**Pausar venda** interrompe novas compras. Alterar ou pausar o kit não modifica
os itens, valores ou a agenda das compras já realizadas, que são registrados
no momento da compra. Para mudar a composição, abra **Editar kit**, ajuste as
quantidades e salve. Administradores comuns e outros usuários não podem abrir
essa área nem chamar suas ações diretamente.

O pedido de cada data guarda o produto de suco efetivamente escolhido; é ele
que aparece na separação, no faturamento e na baixa de estoque da coleta.
Alterar as opções disponíveis depois não muda pedidos já comprados.

## Compra e operação das entregas

O cliente abre **Kits de café**, escolhe um kit e adiciona as datas e os
horários disponíveis para os próximos 31 dias. Cada data recebe uma unidade
do kit completo. A sugestão ao adicionar uma data é uma semana depois da
anterior; o cliente pode ajustar as datas dentro da agenda disponível.

O endereço é o mesmo para toda a compra. O resumo apresenta a soma dos
produtos, dos fretes de cada entrega e o total antes de seguir para Pix ou
cartão. É um pagamento único e não há renovação automática.

A vitrine apresenta as opções lado a lado, com preço por kit e composição.
Quando os componentes têm fotos no catálogo, elas ilustram os produtos que
compõem o kit; não são apresentadas como foto da embalagem completa. A tela
de agendamento organiza escolha do suco, datas, endereço e dados do cliente.
O resumo reúne as entregas escolhidas e o total com fretes. Datas repetidas,
sem disponibilidade ou sem horário impedem avançar para pagamento.
O estilo público usa a identidade da loja (Fraunces, Funnel Sans e fundo
quente), em `loja/kits.css`, separado do visual da gestão.

No painel da gestão, cada data tem seu próprio pedido, com itens, frete e
valor individuais. O detalhe mostra a agenda completa e os links de todas
as entregas. A confirmação de pagamento recebido fora do site continua
exclusiva do owner e exige o valor da compra inteira.

A capacidade de produção das datas fica reservada durante os 35 minutos
do checkout. Pagamento aprovado mantém essas reservas; expiração ou
cancelamento da compra pendente libera todas. Se o gateway confirmar um
pagamento depois da liberação e faltar capacidade, a demanda recebida é
registrada e o painel destaca o excesso para o owner conferir a produção.

O estoque físico é baixado somente na coleta de cada entrega. Repetir a
confirmação da coleta não baixa novamente. O pagamento do mês não desconta
antecipadamente os produtos de todas as semanas.

O owner pode reembolsar e cancelar uma entrega paga pelo gateway, devolvendo
o valor dos itens e do frete daquela entrega; as outras datas permanecem.
Se a resposta do gateway for incerta, o painel mostra a tentativa e bloqueia
nova solicitação e coleta até a conferência no Pagar.me. Pagamentos externos
continuam sem estorno automático pelo gateway. A edição de logística permite
corrigir contato/endereço, mas não trocar a agenda já reservada de um kit;
para isso, é preciso cancelar a entrega e fazer uma nova compra.

As notas fiscais entram numa fila persistente por entrega após o pagamento,
com emissão programada para uma hora antes do início de cada janela.
O sistema verifica as pendências a cada minuto e envia o DANFE por e-mail
assim que a nota for autorizada. Falhas ficam visíveis no pedido e são
retentadas; uma resposta incerta ao criar a nota exige conferência no Tiny
para evitar duplicidade. A emissão manual continua disponível ao owner.

## Referências técnicas

- `app/services/kits_cafe.py`: composição, publicação, preço e catálogo do editor.
- `app/blueprints/kits_cafe_admin/routes.py`: criação, edição e pausa com
  `login_required`, `owner_required` e CSRF em todos os formulários.
- `KitCafe` e `KitCafeItem`: novas tabelas; edição trava o kit antes de trocar
  seus itens, em conjunto com a trava usada na compra.
- A montagem reaproveita `loja_checkout.montar_itens`. A seleção do navegador
  não define preços, e menus inválidos não são substituídos em silêncio.
- `KitCafeSuco`: alternativas de suco em tabela própria, com chave composta
  por kit e produto. Criada pelo startup serializado; sem coluna nova em
  tabela existente. Kits anteriores sem alternativas conservam sua composição.
- `KitCafeOpcao`: tabela nova para as alternativas de croissant/sourdough,
  ligada a uma receita ou produto e ao kit. O startup cria a tabela sem alterar
  colunas dos pedidos existentes. Edição e compra recarregam as opções após
  travar o kit. As escolhas efetivas usam os mesmos snapshots, reservas e
  validações do checkout; a alteração posterior do plano não reescreve pedidos.

## Publicação e retorno de versão

Aguarde a versão com `KitCafeOpcao` estar ativa em todos os processos antes
de configurar os grupos em produção. Em caso de retorno ao código anterior,
pause primeiro os planos que usam croissant/sourdough à escolha: versões
antigas desconhecem os grupos e poderiam calcular apenas os itens fixos e o
suco. Não apague a tabela nem os pedidos já comprados para retornar a versão.
