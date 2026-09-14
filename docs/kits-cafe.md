# Kits de café da manhã com entrega em casa

O dono monta cada opção de kit com os itens já disponíveis à venda no site.
O cliente escolhe os dias e horários de entrega: cada dia corresponde a um
kit completo. É uma compra para um mês, sem renovação automática.

## Montar as opções

1. Entre como **owner** e abra **Kits de café da manhã** no menu da gestão.
2. Clique em **Criar kit**, informe o nome e uma descrição para o cliente.
3. Selecione as quantidades dos itens por entrega. A unidade, porção, tamanho
   e recheio são os do produto publicado no site. Zero deixa o item fora do kit.
4. Confira o valor por kit e escolha **Salvar e publicar no site**, ou salve
   como rascunho para revisar antes de vender.
5. Repita para criar as três opções. Os nomes e as composições são livres.

O sistema não cadastra alimentos nem faz substituições por semelhança de nome.
Se, por exemplo, o queijo branco ou uma porção de suco ainda não estiver à
venda, regularize o produto no **Catálogo do site** antes de incluí-lo no kit.

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

O kit só aparece para compra se estiver publicado e todos os itens estiverem
disponíveis no site. Se algum produto sair do catálogo, ficar sem preço,
esgotar ou tiver uma composição de menu incompatível, o kit fica indisponível
para novas compras até a revisão. Não é vendido parcialmente.

**Pausar venda** interrompe novas compras. Alterar ou pausar o kit não modifica
os itens, valores ou a agenda das compras já realizadas, que são registrados
no momento da compra. Para mudar a composição, abra **Editar kit**, ajuste as
quantidades e salve. Administradores comuns e outros usuários não podem abrir
essa área nem chamar suas ações diretamente.

## Compra e operação das entregas

O cliente abre **Kits agendados**, escolhe um kit e adiciona as datas e os
horários disponíveis para os próximos 31 dias. Cada data recebe uma unidade
do kit completo. A sugestão ao adicionar uma data é uma semana depois da
anterior; o cliente pode ajustar as datas dentro da agenda disponível.

O endereço é o mesmo para toda a compra. O resumo apresenta a soma dos
produtos, dos fretes de cada entrega e o total antes de seguir para Pix ou
cartão. É um pagamento único e não há renovação automática.

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

As notas fiscais entram numa fila persistente por entrega após o pagamento.
O sistema processa até cinco pendências a cada cinco minutos e registra
falhas para nova tentativa. A emissão manual continua disponível no pedido.

## Referências técnicas

- `app/services/kits_cafe.py`: composição, publicação, preço e catálogo do editor.
- `app/blueprints/kits_cafe_admin/routes.py`: criação, edição e pausa com
  `login_required`, `owner_required` e CSRF em todos os formulários.
- `KitCafe` e `KitCafeItem`: novas tabelas; edição trava o kit antes de trocar
  seus itens, em conjunto com a trava usada na compra.
- A montagem reaproveita `loja_checkout.montar_itens`. A seleção do navegador
  não define preços, e menus inválidos não são substituídos em silêncio.
