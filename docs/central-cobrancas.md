# Central de cobranças

## Acesso e organização

Financeiro → **Cobranças** (`/cobrancas/`), seguindo o layout v2 e mantendo
compatibilidade com o layout clássico. A permissão permanece a mesma do
módulo anterior: usuário autenticado com acesso de administrador.

- **Cobranças:** saldo a receber, vencidas, pagas e canceladas; busca por
  cliente/referência, vencimento e situação do envio; 30 cobranças por página.
- **Fechamentos:** faturas consolidadas do B2B (`/b2b/faturas`).
- **Banco:** remessa, retorno e correções (`/cobrancas/banco`). As rotas e
  serviços que geram/baixam títulos continuam os existentes.
- **Documentos:** `/cobrancas/fatura/<id>/documentos` ou
  `/cobrancas/parcela/<id>/documentos`, acessível também na fatura/venda.

## Saldos e fontes

Uma linha por fatura consolidada, parcela não faturada ou boleto avulso.
Parcelas/vendas vinculadas a uma fatura não são somadas novamente. Cobranças
zeradas não entram; canceladas ficam em filtro próprio e fora dos totais.
Os totais respeitam cliente e período, antes da paginação.

O saldo vem dos recebimentos efetivamente registrados nas parcelas; uma
fatura legada marcada como paga com recebimento parcial ainda exibe o saldo.
Se há boleto, o vencimento exibido é o dele. A central é uma projeção de
leitura: não altera valores, vencimentos, parcelas, pagamentos ou estoque.
Vendas sem parcela/fechamento continuam no B2B, fora dos títulos a receber.

## Envio e histórico

O envio conjunto exige NF autorizada e boleto disponível, não cancelado,
com valor igual ao saldo. Cobranças pagas e boletos antigos com valor cheio
após pagamento parcial não podem ser reenviados pela nova ação.

O usuário confere o destinatário e confirma o envio. Se o estado local é
apenas `remessa`, precisa confirmar que verificou o registro no banco;
isso **não** muda automaticamente a situação bancária do título.

Os dois PDFs precisam ser obtidos antes do disparo. Não emite NF nova nem
gera outra cobrança. Uma chave única, gravada antes do envio, impede
duplicação por repetição da mesma solicitação. Uma nova tentativa deliberada
usa uma nova abertura da tela. Resultado incerto/interrompido exige conferir
o serviço de e-mail antes de reenviar; não há repetição automática.

`envio_cobranca` registra referência, destinatário, documentos/nomes dos
anexos, usuário, horário, resultado e identificador do provedor. Não guarda
PDFs ou credenciais. Os caminhos antigos de envio de NF, boleto e ambos
também passam a registrar seus resultados, sem inventar envios anteriores.

- **Sem histórico:** não existe registro no ERP; não prova que nunca enviou.
- **Aceito pelo serviço de e-mail:** API aceitou com identificador; não
  comprova entrega, leitura ou pagamento.
- **Falha:** recusa ou erro antes do envio.
- **Não confirmado:** resultado incerto; conferir antes de tentar de novo.
- **Iniciado, sem confirmação:** intenção gravada sem resultado final.

Não foram adicionados disparos automáticos, cobranças em massa ou lembretes.

## Publicação e retorno

Migração aditiva `6d9e3c7a2f10`, após `2b8d4e6f0a1c`: cria somente a tabela
de histórico e seus índices, sem alterar tabelas financeiras existentes.
É idempotente com o `db.create_all()` anterior ao Alembic no startup atual,
que já serializa o setup entre workers PostgreSQL.

Não exige nova variável de ambiente. Reutiliza Tiny, Sicredi e o serviço de
e-mail já configurados. Após publicar, verificar GET da central, banco e
documentos de uma fatura existente. Não usar envio real como smoke test sem
aprovação e confirmação do destinatário.

Para reverter o código, preservar `envio_cobranca`: não executar downgrade
destrutivo dessa tabela, pois ela contém o histórico de comunicação.

## Validação

`tests/test_central_cobrancas.py` cobre consolidação, saldos parciais,
filtros/paginação, ausência de histórico, autorização/CSRF, destinatário,
dois anexos ou nenhum, idempotência, erro/incerteza, envios legados,
atribuição por parcela, migração idempotente e layout clássico/v2.

Testes automatizados usam banco isolado e provedores simulados.
Conferência visual local usa dados fictícios, sem rede de saída e com
requisições de escrita bloqueadas. Não modifica dados de produção.
