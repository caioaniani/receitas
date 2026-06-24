"""b9 SeruDebitoMov pra rastrear contribuicoes fracionarias por pedido.

Cria a tabela `seru_debito_mov`: cada baixa com `fator != 1.0` grava
uma linha aqui, registrando a contribuicao bruta (`a_baixar_float`) do
pedido pra `SeruDebito.fracao_pendente`. No estorno, `_estornar_pedido`
le essas linhas e devolve a contribuicao — se o acumulador ja foi
zerado por vendas posteriores que baixaram inteiros, devolve inteiros
ao estoque.

Antes desta tabela, fracao contribuida por pedido cancelado ficava
"presa" no acumulador (bug B9 da auditoria de 2026-05-21).

Revision ID: ac57b6648ec4
Revises: 69d82afed149
Create Date: 2026-05-21
"""
import sqlalchemy as sa
from alembic import op

revision = 'ac57b6648ec4'
down_revision = '69d82afed149'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'seru_debito_mov',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('loja_id', sa.Integer(), nullable=False),
        sa.Column('seru_produto_map_id', sa.Integer(), nullable=False),
        sa.Column('seru_pedido_id', sa.String(length=64), nullable=False),
        sa.Column('fracao', sa.Float(), nullable=False),
        sa.Column('criado_em', sa.DateTime(), nullable=False),
        sa.Column('estornado_em', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['loja_id'], ['loja.id']),
        sa.ForeignKeyConstraint(['seru_produto_map_id'], ['seru_produto_map.id']),
    )
    op.create_index(
        'ix_seru_debito_mov_pedido_status',
        'seru_debito_mov',
        ['seru_pedido_id', 'estornado_em'],
    )
    op.create_index(
        op.f('ix_seru_debito_mov_seru_pedido_id'),
        'seru_debito_mov',
        ['seru_pedido_id'],
    )


def downgrade():
    op.drop_index(op.f('ix_seru_debito_mov_seru_pedido_id'),
                  table_name='seru_debito_mov')
    op.drop_index('ix_seru_debito_mov_pedido_status',
                  table_name='seru_debito_mov')
    op.drop_table('seru_debito_mov')
