"""B12: Receita.reaproveitavel + Produto.reaproveitavel.

Item marcado como reaproveitavel NAO baixa estoque quando o desperdicio
for por motivo='validade' (vence mas vira outra coisa: croissant tradicional
vira almond, sourdough tradicional vira chapa). Outros motivos
(estragou/caiu/queimou) seguem baixando normal.

Default False (mantem comportamento atual pra catalogo existente —
admin marca explicitamente o que reaproveita).

Revision ID: 9c3d1a5e8b2f
Revises: 8f2c4a1b7d9e
Create Date: 2026-05-21
"""
import sqlalchemy as sa
from alembic import op

revision = '9c3d1a5e8b2f'
down_revision = '8f2c4a1b7d9e'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('receita',
                  sa.Column('reaproveitavel', sa.Boolean(),
                            nullable=False, server_default=sa.false()))
    op.add_column('produto',
                  sa.Column('reaproveitavel', sa.Boolean(),
                            nullable=False, server_default=sa.false()))


def downgrade():
    op.drop_column('produto', 'reaproveitavel')
    op.drop_column('receita', 'reaproveitavel')
