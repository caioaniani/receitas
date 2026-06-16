# Loja Online — Fase 0 (fundação)

> Plano completo: `/root/.claude/plans/modular-tinkering-owl.md`
> (decidido em 16/06/2026; ver "Decisões do dono").
>
> Esta página é o estado vivo da Fase 0 — qualquer sessão Claude/dono
> entra aqui pra saber o que falta.

## Por que existe esta fase

Antes de qualquer código de produto (modelo de pedido, vitrine, pagamento),
precisamos travar 4 coisas que dependem do dono / de terceiros e bloqueiam
o resto se faltarem.

## Status (atualizar conforme avança)

| Item | Status | Quem | Observação |
|---|---|---|---|
| Conta Pagar.me sandbox + chaves | ⏳ | dono | API key + webhook secret → `PAGARME_API_KEY`, `PAGARME_WEBHOOK_SECRET` no Railway |
| Sign-off do contador | ⏳ | dono | regime fiscal, CFOP do pedido de site, gatilho de emissão NF |
| Auditoria de catálogo | 🟡 em curso | Claude + dono | Tela `/admin/loja-online/auditoria-catalogo` (admin/owner). Meta: ≥80% "prontos pra vitrine" |
| Prefixo da rota | ⏳ | dono | `/loja`, `/teste-loja`, etc — decidir antes da Fase 1 |
| Confirmar tabela de frete | ⏳ | dono | `frete.py` vai virar preço REAL do checkout — anéis (grátis≤1km, +R$5/km, teto 15km) estão OK? |

## Perguntas pro contador (rascunho)

1. **Regime fiscal** atual da padaria — Simples Nacional? Anexo?
2. **CFOP** para venda no e-commerce com entrega em SP capital (o VNDA hoje
   usa qual?).
3. **Quando emitir a NF**: na confirmação do pagamento (recomendado) ou
   no envio/entrega?
4. **Cancelamento**: gatilho de cancelar NF se o pedido for cancelado antes
   da entrega? Janela?
5. **Tiny já está cadastrado pra emitir NF do site** ou só fazia a recepção
   automática do VNDA? O token atual tem permissão pra emitir?
6. **Substituição tributária / ICMS-ST**: aplica em algum produto nosso?
7. **Boletos** que a padaria recebe (B2B, parceiros): mudam algo no
   tratamento de pedido do site?

## O que JÁ existe no sistema (pra reuso na Fase 1+)

Mapeado pelo plano (`/root/.claude/plans/modular-tinkering-owl.md`,
seção "Arquitetura — reuso vs. construir"):

- Catálogo: `app/models/catalogo.py` (`Receita.preco_site`, `Produto.preco_site`,
  imagens Dropbox via `imagem_dropbox_url`).
- Estoque + baixa idempotente: `app/models/estoque.py` + padrão de
  `app/services/vnda_sync.py` e `seru_sync.py`.
- Frete: `app/services/frete.py::consultar_frete()`.
- NF (consulta): `app/services/tiny.py` + `NFLog` em
  `app/models/integracoes.py`.
- Idempotência de webhook: padrão `ChatwootEventoProcessado` /
  `app/blueprints/lalamove/routes.py`.

## Próximo passo

Quando os 4 itens "⏳" virarem ✅, começa a **Fase 1** (modelos + flag
`disponivel_site` + `venda_site` em constants).
