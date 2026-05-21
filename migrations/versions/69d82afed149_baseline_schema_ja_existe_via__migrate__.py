"""Baseline (no-op).

Esta migration eh INTENCIONALMENTE vazia. O schema atual do banco prod
foi construido pelos helpers `_migrate_postgres()`/`_migrate_sqlite()` em
`app/__init__.py`, ANTES da adocao do Alembic.

Procedimento aplicado uma unica vez quando Alembic foi adotado:
  1. flask db stamp head
     (marca o banco como ja estando nesta revisao, sem executar nada)
  2. A partir daqui, qualquer mudanca de schema:
       - Editar o modelo em app/models.py
       - flask db migrate -m "descricao"
       - Revisar o arquivo gerado em migrations/versions/
       - flask db upgrade (local) ou deixar Railway aplicar no deploy

Os helpers `_migrate_*` continuam rodando em deploys antigos por
seguranca, mas devem ser progressivamente esvaziados conforme
migrations Alembic forem cobrindo as mesmas alteracoes.

Revision ID: 69d82afed149
Revises:
Create Date: 2026-05-21
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '69d82afed149'
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    """No-op: schema ja existe via _migrate_* legados."""
    pass


def downgrade():
    """No-op: rollback nao se aplica ao baseline."""
    pass
