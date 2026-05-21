"""B14: PedidoLoja.driver_id (atribuicao apos handshake da saida).

Quando motorista escaneia o QR de saida e digita PIN, o pedido fica
amarrado ao Driver dele. Painel /driver/<token> filtra por driver_id.
Idempotencia: se ja tem driver_id setado, ignora novo handshake (a nao
ser que o admin libere via "Forcar entrega").

Revision ID: d2f5c9a1b7e3
Revises: 4a8e2d6f1c5b
Create Date: 2026-05-21
"""
import sqlalchemy as sa
from alembic import op

revision = 'd2f5c9a1b7e3'
down_revision = '4a8e2d6f1c5b'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('pedido_loja',
                  sa.Column('driver_id', sa.Integer(), nullable=True))
    op.create_foreign_key('fk_pedido_loja_driver',
                          'pedido_loja', 'driver_entrega',
                          ['driver_id'], ['id'])
    op.create_index('ix_pedido_loja_driver_id',
                    'pedido_loja', ['driver_id'])


def downgrade():
    op.drop_index('ix_pedido_loja_driver_id', table_name='pedido_loja')
    op.drop_constraint('fk_pedido_loja_driver',
                       'pedido_loja', type_='foreignkey')
    op.drop_column('pedido_loja', 'driver_id')
