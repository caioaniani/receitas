# Módulo Financeiro — Plano de implementação

Sistema atual é **cego pra dinheiro**: tem operações de produção e venda, mas
não tem contas a pagar/receber, fluxo de caixa, NF-e, conciliação bancária.
Este documento descreve o módulo Financeiro completo, dividido em fases.

## Escopo

O que entra:
- Contas a pagar (despesas fixas, MP, fornecedores)
- Contas a receber (clientes, parcelas, atrasos)
- Lançamentos manuais de caixa (entrada/saída avulsa)
- Conciliação bancária (extrato vs sistema)
- Categorização de despesas (DRE)
- Fechamento mensal com DRE simplificado

O que **fica de fora** desta primeira versão:
- Emissão de NF-e/NFC-e (módulo separado, exige homologação fiscal)
- Integração PIX (vem depois — exige certificado bancário)
- Folha de pagamento (já existe em /rh/folha, será apenas consumida aqui)

## Modelos propostos

### Conta bancária (`conta_bancaria`)
```python
id, nome ('Itaú PJ', 'Caixinha'), tipo ('banco' | 'caixa' | 'aplicacao'),
banco, agencia, conta, saldo_inicial, saldo_atual (calculado),
ativa, criado_em
```

### Categoria financeira (`categoria_financeira`)
```python
id, nome ('Aluguel', 'Insumos', 'Vendas balcão', ...),
tipo ('receita' | 'despesa'), parent_id (nullable, pra hierarquia),
cor (pra dashboards), ativa
```
**Seed inicial:**
- Receitas: Vendas balcão, Vendas delivery, Pedidos entre lojas, Outras
- Despesas operacionais: MP, Embalagens, Energia, Água, Aluguel, Internet
- Despesas pessoal: Salários, Vale transporte, Vale refeição, INSS, FGTS
- Despesas tributárias: Simples, ISS, IRRF
- Investimentos: Equipamentos, Reformas

### Lançamento financeiro (`lancamento`)
```python
id, conta_bancaria_id, categoria_id, tipo ('entrada' | 'saida'),
valor, data_vencimento, data_pagamento (nullable — se preenchido = pago),
descricao, referencia_externa (NF, boleto, etc),
fornecedor_id (nullable, FK pra Fornecedor),
status ('previsto' | 'pago' | 'cancelado'),
recorrente_id (nullable, FK pra LancamentoRecorrente),
criado_por, criado_em, atualizado_em
```

Index em: `data_vencimento`, `data_pagamento`, `categoria_id`, `status`.

### Lançamento recorrente (`lancamento_recorrente`)
```python
id, nome ('Aluguel mensal'), categoria_id, conta_bancaria_id,
valor, dia_do_mes, fornecedor_id, ativa,
ultima_geracao (data), proxima_geracao
```

Job diário (ou no boot da app) gera o lançamento do mês com status='previsto'.

### Conciliação (`conciliacao_extrato`)
```python
id, conta_bancaria_id, data, valor, descricao_banco,
lancamento_id (FK, pode ser null se ainda não conciliado),
importado_em
```

Tela de conciliação compara extrato bancário (importado de CSV/OFX) com
lançamentos do sistema. Usuário liga 1-pra-1.

## Endpoints (blueprint `financeiro`)

| Rota | Função |
|---|---|
| `/financeiro/` | Dashboard: saldo total, contas a pagar próx 7 dias, contas a receber, alerta vencidas |
| `/financeiro/lancamentos` | Lista de lançamentos com filtros (período, categoria, status, conta) |
| `/financeiro/lancamento/novo` | Form de criar lançamento manual |
| `/financeiro/lancamento/<id>` | Detalhe + editar |
| `/financeiro/lancamento/<id>/pagar` | Marcar como pago (data_pagamento = hoje) |
| `/financeiro/contas` | CRUD de contas bancárias |
| `/financeiro/categorias` | CRUD de categorias |
| `/financeiro/recorrentes` | CRUD de lançamentos recorrentes |
| `/financeiro/conciliacao/<conta_id>` | Importar extrato + conciliar |
| `/financeiro/dre/<ano>/<mes>` | DRE mensal: receitas, despesas, lucro líquido, % por categoria |
| `/financeiro/fluxo` | Fluxo de caixa projetado: hoje, semana, mês — incluindo previstos |

## Integrações com módulos existentes

1. **Fornecedores → Contas a pagar**
   - Ao receber MP (entrada em `MovimentacaoEstoque` com `fornecedor_id`),
     opção de gerar lançamento `previsto` automático (`saida`,
     categoria='Insumos', vencimento = data + 30 dias)

2. **PedidoLocal → Contas a receber**
   - Ao criar PedidoLocal (entrega avulsa), gerar lançamento `previsto`
     `entrada` (categoria 'Vendas delivery', vencimento = data_entrega)
   - Ao marcar como entregue → status 'pago' automático

3. **PedidoLoja → relatório interno**
   - Transferências entre lojas não são vendas reais, mas viram
     "movimento de estoque" registrado em categoria interna

4. **Folha de pagamento** (já existe)
   - Ao gerar folha do mês, criar 1 lançamento `previsto` saida
     pra cada funcionário (categoria='Salários', vencimento = 5 do
     mês seguinte)

5. **PDV (Seru)** — futuro
   - Sync diário do PDV traz totais por método pagamento
   - Gera lançamento `pago` saida (taxa) + entrada (líquido)
   - Por enquanto deixa em /pdv e copilot consultar manual

## Fases de implementação

### Fase 1 — Base (1 semana)
- Modelos: ContaBancaria, CategoriaFinanceira, Lancamento
- Migration + seed de categorias padrão
- CRUD básico: criar lançamento manual, marcar como pago
- Dashboard financeiro simples: saldo total, próx 7 dias, vencidas

### Fase 2 — Recorrência (3-4 dias)
- LancamentoRecorrente + job diário (em `_migrate` ou comando)
- Tela de recorrentes
- Gerar lançamentos do mês ao boot

### Fase 3 — Integrações (1 semana)
- Hook em MovimentacaoEstoque (entrada com fornecedor) → cria conta a pagar
- Hook em PedidoLocal → cria conta a receber
- Hook em FolhaPagamento → cria conta a pagar
- Toggle "Gerar lançamento automático" nos forms (não força)

### Fase 4 — Conciliação (1 semana)
- Modelo ConciliacaoExtrato
- Upload de CSV/OFX (parser simples)
- Tela de conciliação (lista lado a lado, drag-link)

### Fase 5 — DRE + Fluxo (3-4 dias)
- DRE mensal (categorias receita - categorias despesa)
- Fluxo de caixa projetado (gráfico semanal com previstos)
- Export PDF/Excel

### Fase 6 — Integrações fiscais (longo prazo)
- NF-e (estudo de viabilidade — homologação SEFAZ)
- PIX (estudo de viabilidade — certificado bancário)
- Conciliação automática via Open Finance

## Decisões em aberto

- **Multi-empresa?** Hoje tudo numa CNPJ só. Se virar holding com várias
  CNPJs, modelo `Empresa` vira FK em ContaBancaria, Lancamento, etc.
- **Permissões granulares?** Hoje só admin vê tudo. Pode ter perfil
  "financeiro" que vê /financeiro mas não vê /rh, e vice-versa.
- **Centro de custo?** Lançamentos podem ter `loja_id` opcional pra
  ratear despesa por filial. Vale a pena se quiser DRE por filial.
- **Conta de contrapartida?** Em contabilidade dupla, todo lançamento
  tem origem e destino. Simplificação: usar só a conta bancária; pra
  transferência entre contas, criar 2 lançamentos linkados.

## Estimativa total

- Fase 1+2+3: ~3 semanas pra ter financeiro útil operacional
- Fase 4+5: ~2 semanas pra fechamento mensal limpo
- Fase 6: meses (depende de homologação fiscal)

## Custo de não fazer

- Sem visibilidade de margem real (custo MP varia, salário entra, energia)
- Não consegue pedir financiamento sem DRE
- Difícil identificar furto/desvio sem conciliação bancária
- Imposto via contabilidade externa fica mais caro (sem dados estruturados)
