"""B16: Estados de produto — Receita.familia + estado em PedidoItem/Estoque*.

Permite a mesma receita aparecer em pedido/estoque em estados distintos
(assado/backup/NULL=padrao) sem duplicar cadastro. Ver
`app/constants.py:FAMILIAS_RECEITA` e o plano em
`/root/.claude/plans/virtual-yawning-adleman.md`.

Revision ID: 1f7a3c9d8e4b
Revises: e7b4c2a8d5f1
Create Date: 2026-05-22
"""
import sqlalchemy as sa
from alembic import op

revision = '1f7a3c9d8e4b'
down_revision = 'e7b4c2a8d5f1'
branch_labels = None
depends_on = None


def upgrade():
    # Receita.familia (nullable; backfill manual)
    op.add_column('receita',
                  sa.Column('familia', sa.String(length=30), nullable=True))
    op.create_index('ix_receita_familia', 'receita', ['familia'])

    # PedidoItem.estado (nullable; NULL = padrao da familia)
    op.add_column('pedido_item',
                  sa.Column('estado', sa.String(length=20), nullable=True))

    # EstoqueProducao.estado
    op.add_column('estoque_producao',
                  sa.Column('estado', sa.String(length=20), nullable=True))
    op.create_index('ix_estoque_producao_receita_estado',
                    'estoque_producao', ['receita_id', 'estado'])
    op.create_index('ix_estoque_producao_produto_estado',
                    'estoque_producao', ['produto_id', 'estado'])

    # EstoqueLoja.estado
    op.add_column('estoque_loja',
                  sa.Column('estado', sa.String(length=20), nullable=True))
    op.create_index('ix_estoque_loja_loja_receita_estado',
                    'estoque_loja', ['loja_id', 'receita_id', 'estado'])
    op.create_index('ix_estoque_loja_loja_produto_estado',
                    'estoque_loja', ['loja_id', 'produto_id', 'estado'])


def downgrade():
    op.drop_index('ix_estoque_loja_loja_produto_estado',
                  table_name='estoque_loja')
    op.drop_index('ix_estoque_loja_loja_receita_estado',
                  table_name='estoque_loja')
    op.drop_column('estoque_loja', 'estado')

    op.drop_index('ix_estoque_producao_produto_estado',
                  table_name='estoque_producao')
    op.drop_index('ix_estoque_producao_receita_estado',
                  table_name='estoque_producao')
    op.drop_column('estoque_producao', 'estado')

    op.drop_column('pedido_item', 'estado')

    op.drop_index('ix_receita_familia', table_name='receita')
    op.drop_column('receita', 'familia')
