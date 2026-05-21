"""B13: PedidoItemFoto pra conferencia obrigatoria com foto por SKU.

Foto obrigatoria em 2 etapas do fluxo:
- saida: industria tira foto antes do QR de saida ser gerado
- entrega: motorista tira foto antes do QR de entrega ser gerado

1 foto por (pedido_item, etapa). Re-upload substitui (unique constraint).

Revision ID: 4a8e2d6f1c5b
Revises: 9c3d1a5e8b2f
Create Date: 2026-05-21
"""
import sqlalchemy as sa
from alembic import op

revision = '4a8e2d6f1c5b'
down_revision = '9c3d1a5e8b2f'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'pedido_item_foto',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('pedido_item_id', sa.Integer(), nullable=False),
        sa.Column('etapa', sa.String(length=10), nullable=False),
        sa.Column('imagem', sa.LargeBinary(), nullable=False),
        sa.Column('mimetype', sa.String(length=100), nullable=True),
        sa.Column('criado_em', sa.DateTime(), nullable=False),
        sa.Column('criado_por_id', sa.Integer(), nullable=True),
        sa.Column('criado_por_driver_id', sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(['pedido_item_id'], ['pedido_item.id']),
        sa.ForeignKeyConstraint(['criado_por_id'], ['usuario.id']),
        sa.ForeignKeyConstraint(['criado_por_driver_id'], ['driver_entrega.id']),
        sa.UniqueConstraint('pedido_item_id', 'etapa',
                             name='uq_pedidoitemfoto_item_etapa'),
    )
    op.create_index('ix_pedido_item_foto_pedido_item_id',
                    'pedido_item_foto', ['pedido_item_id'])


def downgrade():
    op.drop_index('ix_pedido_item_foto_pedido_item_id',
                  table_name='pedido_item_foto')
    op.drop_table('pedido_item_foto')
