"""Classificação auditável de divulgações, sem reclassificar dados automaticamente.

Revision ID: b7248c1d9e02
Revises: 91b6a7d3c820
"""
import sqlalchemy as sa
from alembic import op

revision = 'b7248c1d9e02'
down_revision = '91b6a7d3c820'
branch_labels = None
depends_on = None


def upgrade():
    columns = {c['name'] for c in sa.inspect(op.get_bind()).get_columns('venda_b2b')}
    if 'dispensa_cobranca' not in columns:
        op.add_column('venda_b2b', sa.Column('dispensa_cobranca', sa.JSON(none_as_null=True), nullable=True))


def downgrade():
    op.drop_column('venda_b2b', 'dispensa_cobranca')
