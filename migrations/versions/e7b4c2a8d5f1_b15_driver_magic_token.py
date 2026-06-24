"""B15: DriverMagicToken pra magic link diario.

Cron 05:00 BRT gera token novo pra cada Driver ativo + envia via Z-API.
Velhos viram revogados=True.

Revision ID: e7b4c2a8d5f1
Revises: d2f5c9a1b7e3
Create Date: 2026-05-21
"""
import sqlalchemy as sa
from alembic import op

revision = 'e7b4c2a8d5f1'
down_revision = 'd2f5c9a1b7e3'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'driver_magic_token',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('driver_id', sa.Integer(), nullable=False),
        sa.Column('token', sa.String(length=64), nullable=False),
        sa.Column('criado_em', sa.DateTime(), nullable=False),
        sa.Column('expira_em', sa.DateTime(), nullable=False),
        sa.Column('revogado', sa.Boolean(), nullable=False,
                  server_default=sa.false()),
        sa.Column('enviado_em', sa.DateTime(), nullable=True),
        sa.Column('enviado_ok', sa.Boolean(), nullable=True),
        sa.ForeignKeyConstraint(['driver_id'], ['driver_entrega.id']),
        sa.UniqueConstraint('token', name='uq_driver_magic_token_token'),
    )
    op.create_index('ix_driver_magic_token_driver_id',
                    'driver_magic_token', ['driver_id'])
    op.create_index('ix_driver_magic_token_token',
                    'driver_magic_token', ['token'])


def downgrade():
    op.drop_index('ix_driver_magic_token_token',
                  table_name='driver_magic_token')
    op.drop_index('ix_driver_magic_token_driver_id',
                  table_name='driver_magic_token')
    op.drop_table('driver_magic_token')
