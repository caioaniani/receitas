# Emissão do site com CNPJ e acompanhamento de atendimento

Decisão do owner em 17/09/2026: completar dados fiscais da empresa e insistir
nos avisos de casos sem atendimento, inclusive após uma contenção automática.

## Dados fiscais

Cada checkout, incluindo cada entrega de kit, congela CPF/CNPJ em
`FiscalPedidoOnline`. Pagamento, emissão e autorização do bot usam esse
documento. Ao mudar o documento de um cliente, pedidos legados sem snapshot
preservam o valor anterior. Dados antigos já incorretos exigem conferência.

Antes de incluir NF com CNPJ, consulta o cadastro interno do Tiny por documento
exato (duplicatas são pendência), consulta a base CNPJ pública para razão social
e endereço e aproveita a IE do Tiny quando está na mesma UF. As APIs públicas
atualmente usadas não fornecem IE: ausência não significa isenção.

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
- Enquanto aguarda humano: nova cobrança ao dono a cada quinze minutos.
- Caso grave: primeiro aviso sem a espera inicial; após resposta humana,
  acompanha a cada sessenta minutos até `resolved` confirmado no Chatwoot.
- Contenção, automação, campanha, nota interna ou envio falho não contam como
  atendimento. Agradecimento não resolve uma ocorrência grave.
- Reconhecer o banner significa ter visto o aviso, não resolver o caso.
- Conversas abertas são paginadas; incidentes ativos fora da lista têm status
  consultado diretamente. Falha da API não significa resolução.
- Contenção ao cliente mantém deduplicação por contato e por conversa. O dono
  recebe lembretes; o cliente não recebe a mesma contenção a cada lembrete.
- O teto de cinco avisos por ciclo prioriza cobranças mais atrasadas, incluindo
  casos ainda sem aviso. Claim anterior ao envio evita duplicação no restart;
  falha confirmada permite nova tentativa.

As duas tabelas são novas e criadas pelo `create_all` serializado do startup.
Não há alteração de colunas antigas nem emissão retroativa em massa.
