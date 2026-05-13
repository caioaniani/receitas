# Convenções de trabalho (Claude)

## Branches

- **Desenvolva em**: `claude/continue-controller-conversation-aGS3F`
- **Abra PR para**: `claude/bakery-recipe-cost-system-N4ieR` (é o branch que o Railway acompanha — merge dispara deploy automático em produção)
- **Nunca** force-push nem use `--no-verify` sem autorização explícita

## Deploy

Railway está conectado em `claude/bakery-recipe-cost-system-N4ieR`. Push pra produção = abrir PR e mergear.

## Sistema

Flask + SQLAlchemy + Bootstrap 5. Padaria Opão (gestão completa: receitas, pedidos, entregas, PDV, estoque, RH, copilot com Claude Haiku 4.5).
