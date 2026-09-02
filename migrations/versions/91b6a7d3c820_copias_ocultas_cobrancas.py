"""Registra as cópias ocultas dos novos envios, sem inventar dados antigos.

Revision ID: 91b6a7d3c820
Revises: 6d9e3c7a2f10
"""
import sqlalchemy as sa
from alembic import op

revision = '91b6a7d3c820'
down_revision = '6d9e3c7a2f10'
branch_labels = None
depends_on = None


def upgrade():
    columns = {c['name'] for c in sa.inspect(op.get_bind()).get_columns('envio_cobranca')}
    if 'copias_ocultas' not in columns:
        op.add_column('envio_cobranca', sa.Column('copias_ocultas', sa.JSON(), nullable=True))


def downgrade():
    op.drop_column('envio_cobranca', 'copias_ocultas')
