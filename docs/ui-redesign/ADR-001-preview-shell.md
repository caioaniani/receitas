# ADR-001: Redesign progressivo no ambiente de preview

**Status:** Aceito  
**Data:** 2026-08-17

## Contexto

O sistema tem mais de 270 templates e dezenas de fluxos operacionais ativos.
Substituir tudo de uma vez aumentaria o risco de regressão e impediria a
comparação entre a interface atual e a proposta.

## Decisão

O redesign será ativado por `PREVIEW_MODE=1` e seguirá três camadas:

1. Shell global: navegação por tarefa, tokens e componentes compartilhados.
2. Normalização: estilos seguros para telas legadas ainda não migradas.
3. Migração por fluxo: Produção, Lojas, Catálogo, Vendas, Financeiro, Pessoas,
   Relatórios e Administração.

A produção continuará usando o shell atual até aprovação explícita. Dados de
comparação serão sanitizados; senhas, tokens e dados pessoais não entram no
ambiente de preview.

## Consequências

- Todas as telas base ganham imediatamente uma navegação mais curta.
- Cada fluxo pode ser comparado e validado isoladamente.
- Por um período, telas migradas e legadas coexistirão no preview.
- Telas especializadas que não estendem `base.html` exigirão migração própria.
