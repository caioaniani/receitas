# Cobrança após entrega B2B e aviso de remessa

## Contrato

Somente novos eventos: marcar **entregue** no padeiro enfileira pedidos avulsos;
fechar uma conta mensal enfileira **uma fatura consolidada**. Clientes mensais
nunca são cobrados individualmente pela entrega. Fechamento mensal permanece
manual, com os mesmos critérios de período e parcelas existentes. Não há varredura
de vendas/faturas antigas. Divulgação, canceladas, pagamentos parciais, valores
inconsistentes e condições explícitas Pix/dinheiro/transferência não são cobrados
automaticamente. O evento do padeiro significa despacho no sistema atual, não
comprovante físico de entrega ao cliente.

Entrega/fechamento → intenção durável → NF → boleto/remessa → aviso interno →
upload manual no Sicredi → confirmação do registro → e-mail NF + boleto.

Avisos: caio@opao.online e dakson@opao.online, individualmente, uma vez por remessa.
Cópias ocultas ao cliente preservadas: caio, dakson e contato@opao.online.
O arquivo CNAB não é enviado automaticamente ao banco nem anexado ao aviso;
o aviso aponta para a área autenticada `/cobrancas/automacao`.

## Limites de segurança

- Intenção criada na mesma transação do evento, chave única por origem.
- Worker a cada minuto, no scheduler existente; até 20 origens por ciclo.
  `SERU_AUTO_SYNC=0` desliga o scheduler existente inteiro, não só este fluxo.
  O painel mostra o último ciclo confirmado; ausência por mais de cinco minutos
  fica visível como aviso. GET não atualiza artificialmente esse carimbo.
- Lock entre processos (advisory PostgreSQL, flock SQLite) sobre worker, documento
  e geração de remessa. Número bancário/remessa serializado também entre ações manuais.
- Intenção fiscal persistida antes da criação no Tiny. Resposta incerta sem ID
  bloqueia uma segunda criação. O operador deve reconciliar no Tiny; nunca apagar
  a intenção por conta própria para forçar nova NF.
- Nota existente é reutilizada; NF autorizada não pode ser refeita pela automação.
- Assinatura de cliente/itens/valores capturada antes da emissão impede que uma
  edição concorrente transforme a NF antiga em cobrança de um pedido diferente.
- Registro no banco = ocorrência de retorno ou atestado explícito do operador
  referente à remessa, nosso número, valor e vencimento exatos. Gerar/baixar/enviar
  arquivo não conta como registro. O atestado não falsifica o status do retorno.
- E-mail persistido antes de chamar o provedor. Aceitação não comprova entrega.
  Tentativas interrompidas/incertas/falhas não são reenviadas automaticamente.
  Reenvio é ação manual no histórico existente.
- Dakson recebe delegação específica de NF B2B pelo owner na tela de usuários;
  não vira owner nem ganha permissão de recriar notas do zero. Outros admins não
  recebem a delegação. Revogação é imediata.
- Sem alteração no checkout público, estoque, regras fiscais ou configuração Sicredi.

## Banco e publicação

Somente cinco tabelas novas. `_setup_schema` executa `db.create_all()` protegido
pelo lock de startup PostgreSQL antes de servir requisições. Migração Alembic
idempotente registra a mesma estrutura; nenhum ALTER em tabela existente.
Nenhuma credencial nova exigida. Usam-se os provedores/configurações já existentes.

Testar com Tiny/Postmark falsos: fluxo avulso/mensal, idempotência, timeout,
registro manual/retorno, BCC, divulgação, pagamentos, mudanças de dados,
permissão e CSRF. Executar Ruff e suíte completa. Conferir interface desktop/móvel.
Antes de publicar, revisar diff e CI. Após publicar, conferir deployment do SHA,
health, página autenticada, grant específico para Dakson e checkout público.
Não marcar entrega real, emitir NF nem enviar cobrança apenas para fazer smoke test.

Rollback: reverter o commit da funcionalidade; conservar as tabelas de histórico.
Antes de reativar depois de incidente, reconciliar intenções fiscais/e-mails sem
confirmação nos provedores. Não apagar registros de intenção nem recriar documentos
para “destravar”. Avisos internos com falha ficam visíveis no painel.

## Revisão antes da publicação — 02/09/2026

Revisão própria de segurança, concorrência e falhas: criações fiscais incertas
receberam trava persistente; comparação de assinatura impede NF/boleto com dados
divergentes; envios manual e automático compartilham a trava; consulta de remessas
usa join para evitar uma consulta por título. Grant fiscal exige owner, é revogável
e não permite refazer NF. Todas as mutações de interface são POST com CSRF.

Suíte completa local: 4.496 testes passaram, 2 skipped, 3 xpassed, antes do teste
adicional de heartbeat. Fluxo local verificado em 390px e desktop: aviso, confirmação
de registro e grant fiscal, somente com dados e provedores fictícios. PostgreSQL
advisory locks revisados; integração PostgreSQL real não foi exercitada localmente.
Verificações finais de CI/deployment devem corresponder ao SHA publicado.
