# Emissão do site com CNPJ e acompanhamento de atendimento

Decisão do owner em 17/09/2026: completar dados fiscais da empresa e insistir
nos avisos de casos sem atendimento, inclusive após uma contenção automática.

## Dados fiscais

Cada checkout, incluindo cada entrega de kit, congela CPF/CNPJ em
`FiscalPedidoOnline`. Pagamento, emissão e autorização do bot usam esse
documento. Ao mudar o documento de um cliente, pedidos legados sem snapshot
preservam o valor anterior. Dados antigos já incorretos exigem conferência.

No checkout comum e nos kits, digitar CNPJ consulta razão social, endereço fiscal
e IE na base pública CNPJ.ws. Só aproveita uma IE ativa, do CNPJ exato e da UF
do endereço fiscal, sem ambiguidade. A cobertura de IE depende da UF (inclui SP).
É uma base cadastral, não uma validação em tempo real na SEFAZ. O cliente confere
os campos; endereço de entrega e contato do comprador não são substituídos.

O servidor assina o resultado da consulta por duas horas. Documento e campos
devem corresponder à consulta para a IE ser considerada encontrada. Alteração
manual, ausência de IE ou consulta indisponível permitem a compra, mas deixam
a emissão pendente de conferência do owner. Ausência nunca significa isenção.
Cada entrega de kit recebe seu próprio snapshot na mesma transação, sem novas
chamadas externas; a conferência do cliente não registra aprovação do owner.

Cache e reserva global de chamadas ficam em tabelas novas, compartilhados entre
workers. Respeitam o limite público de três consultas por minuto sem esperar
dentro da transação do checkout. Quando necessário, BrasilAPI/MinhaReceita
completam somente os dados cadastrais. O endpoint público usa CSRF, limite por
cliente e lista explícita de campos; não consulta nem expõe contatos do Tiny.

Pedidos legados continuam consultando o cadastro interno do Tiny por documento
exato (duplicatas são pendência), a base CNPJ para razão social e endereço e a IE
do Tiny quando está na mesma UF. Uma reconsulta explícita do owner pode atualizar
o snapshot; emissões automáticas preservam o que foi conferido no checkout.

Sem dados completos, a emissão fica pendente no detalhe do pedido. Somente o
owner pode consultar novamente ou confirmar razão social, endereço e condição
de contribuinte, após conferência do cadastro no Tiny. NF já autorizada ou
inclusão incerta não pode ser alterada por esse formulário. Um rascunho existente
pode ser conferido localmente, mas sua correção continua no Tiny, sem duplicá-lo.

O endereço de entrega segue separado; o cadastro Tiny não é sobrescrito pela
inclusão. Antes de solicitar autorização, compara os dados retornados do rascunho
com o cadastro fiscal persistido. Divergências impedem a autorização. A emissão
continua uma hora antes da entrega, com envio por e-mail após autorização.

O bot não fornece rascunho como NF: consulta a autorização e transfere
deterministicamente quando encontra pendência. As situações Tiny 6 (Autorizada)
e 7 (Emitida DANFE, após autorização/impressão) confirmam; 2 (Emitida) não basta.

## Atendimento

`EsperaAtendimento` persiste o acompanhamento entre deploys. O monitor roda no
cron existente a cada cinco minutos, independentemente do modelo de IA.

- Caso comum: primeiro aviso após dez minutos sem resposta humana.
- Após gerar um alerta: nova cobrança ao dono a cada quinze minutos até
  `resolved` confirmado no Chatwoot. Uma resposta humana mantém o caso em
  atendimento; não encerra a cobrança. Casos graves dispensam a espera inicial.
- Contenção, automação, campanha, nota interna ou envio falho não contam como
  atendimento. Agradecimento não resolve uma ocorrência grave.
- Reconhecer o banner significa ter visto o aviso, não resolver o caso.
- O Painel do Dia abre um pop-up com as pendências para usuários com acesso ao
  atendimento. Consulta a fila local a cada vinte segundos, sem chamadas externas.
  Fechar adia o aviso por cinco minutos apenas naquela aba; novas pendências
  continuam avisando. Um som breve reforça as pendências a cada cinco minutos,
  após a interação que habilita áudio no navegador. O indicador permite abrir a lista.
- Abrir a conversa não resolve nem reconhece o atendimento. Uma resposta enviada
  com sucesso mantém um caso já alertado até resolução confirmada.
  Falhas não encerram a pendência. Acompanhamentos ativos não expiram ao mudar o dia.
- O pop-up respeita mensagens sendo digitadas. Rascunhos de conversas diferentes
  ficam separados em memória, sem gravar texto de clientes no armazenamento local.
- Atendimento respondido antes do prazo, sem alerta anterior, não vira cobrança
  por uma reconciliação tardia. Casos legados respondidos só retomam a cobrança
  quando há alerta do episódio e o Chatwoot confirma que continuam abertos.
- Conversas abertas são paginadas; incidentes ativos fora da lista têm status
  consultado diretamente. Falha da API não significa resolução.
- Contenção ao cliente mantém deduplicação por contato e por conversa. O dono
  recebe lembretes; o cliente não recebe a mesma contenção a cada lembrete.
- O teto de cinco avisos por ciclo prioriza cobranças mais atrasadas, incluindo
  casos ainda sem aviso. Claim anterior ao envio evita duplicação no restart;
  falha confirmada permite nova tentativa.

As tabelas adicionais são novas e criadas pelo `create_all` serializado do startup.
Não há alteração de colunas antigas nem emissão retroativa em massa.
