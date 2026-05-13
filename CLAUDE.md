# Convenções de trabalho (Claude)

## Branches

- **Branch de produção (Railway acompanha)**: `claude/continue-controller-conversation-aGS3F`
- **Auto-deploy ativo no Railway** (Auto deploys ON, Wait for CI OFF). Qualquer push
  pra esse branch dispara build + deploy em ~2-3 min automaticamente.
- **Workflow padrão**: commit direto no branch de produção (preferência do usuário:
  Opção B). Se a mudança for grande/arriscada, abra PR mirando ele pra ter
  janela de revisão antes do merge.
- **Nunca** force-push nem use `--no-verify` sem autorização explícita.

## Deploy

Railway → projeto `receitas` → serviço `web` → conectado em
`caioaniani/receitas` branch `claude/continue-controller-conversation-aGS3F`.
Push = deploy automático.

## Sistema

Flask + SQLAlchemy + Bootstrap 5. Padaria Opão (gestão completa: receitas,
pedidos, entregas, PDV, estoque, RH, copilot com Claude Haiku 4.5).
