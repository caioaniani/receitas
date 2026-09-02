"""Central de cobranças: histórico de envios, sem alterar tabelas financeiras.

Revision ID: 6d9e3c7a2f10
Revises: 2b8d4e6f0a1c
"""
import sqlalchemy as sa
from alembic import op

revision = '6d9e3c7a2f10'
down_revision = '2b8d4e6f0a1c'
branch_labels = None
depends_on = None


def upgrade():
    # Startup usa create_all antes do Alembic; migração é idempotente.
    if sa.inspect(op.get_bind()).has_table('envio_cobranca'):
        return
    op.create_table(
        'envio_cobranca',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('chave', sa.String(36), nullable=False, unique=True),
        sa.Column('fatura_id', sa.Integer()),
        sa.Column('venda_id', sa.Integer()),
        sa.Column('cobranca_ids', sa.JSON(), nullable=False),
        sa.Column('referencia', sa.String(120), nullable=False),
        sa.Column('destinatario', sa.String(254), nullable=False),
        sa.Column('documentos', sa.String(20), nullable=False),
        sa.Column('nf_id', sa.String(40)),
        sa.Column('anexos', sa.JSON(), nullable=False),
        sa.Column('status', sa.String(20), nullable=False),
        sa.Column('provedor_id', sa.String(150)),
        sa.Column('erro', sa.String(500)),
        sa.Column('criado_em', sa.DateTime(), nullable=False),
        sa.Column('concluido_em', sa.DateTime()),
        sa.Column('usuario_id', sa.Integer()),
        sa.Column('usuario_nome', sa.String(100)),
    )
    for coluna in ('fatura_id', 'venda_id', 'criado_em'):
        op.create_index(f'ix_envio_cobranca_{coluna}', 'envio_cobranca', [coluna])


def downgrade():
    op.drop_table('envio_cobranca')
