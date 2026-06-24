"""B4: dinheiro de Float pra Numeric(10, 2) — precisao exata em centavos.

Float (IEEE 754) nao representa exatamente decimais binarios — 0.1 + 0.2
da 0.30000000000000004 em Python/Postgres. Isso fez parcelas de R$ 33,33
× 3 nao quitarem uma venda de R$ 100 (acumulado 99.99000000000... < 100).

Numeric(10, 2) armazena valor exato em base decimal. 8 digitos de inteiro
+ 2 de centavos = ate R$ 99.999.999,99 (mais que suficiente).

USING ROUND(coluna::numeric, 2): se ja ha valor com mais de 2 casas
(pode acontecer com float), arredonda antes do cast — evita perda de
dados silenciosa pra mais de 2 casas.

Colunas afetadas (so dinheiro em R$ — porcentagens ficam Float):
- venda_b2b.valor_total
- venda_b2b_item.preco_unitario
- venda_b2b_parcela.valor
- venda_b2b_parcela.valor_pago
- venda_manual_loja.valor_unitario

Revision ID: 643bd66e89c3
Revises: ac57b6648ec4
Create Date: 2026-05-21
"""
import sqlalchemy as sa
from alembic import op

revision = '643bd66e89c3'
down_revision = 'ac57b6648ec4'
branch_labels = None
depends_on = None


COLUNAS = [
    ('venda_b2b', 'valor_total', False),         # NOT NULL
    ('venda_b2b_item', 'preco_unitario', False),
    ('venda_b2b_parcela', 'valor', False),
    ('venda_b2b_parcela', 'valor_pago', True),   # nullable
    ('venda_manual_loja', 'valor_unitario', True),
]


def upgrade():
    bind = op.get_bind()
    dialect = bind.dialect.name
    for tabela, coluna, nullable in COLUNAS:
        if dialect == 'postgresql':
            op.alter_column(
                tabela, coluna,
                type_=sa.Numeric(10, 2),
                existing_type=sa.Float(),
                existing_nullable=nullable,
                postgresql_using=f'ROUND({coluna}::numeric, 2)',
            )
        else:
            # SQLite nao suporta ALTER COLUMN TYPE diretamente, mas a
            # implementacao do SQLAlchemy/Alembic faz table rename + copy.
            # Os dados vao como REAL (sem perda); modelo passa a tratar
            # como Decimal porque SQLAlchemy faz o cast.
            op.alter_column(
                tabela, coluna,
                type_=sa.Numeric(10, 2),
                existing_type=sa.Float(),
                existing_nullable=nullable,
            )


def downgrade():
    bind = op.get_bind()
    dialect = bind.dialect.name
    for tabela, coluna, nullable in COLUNAS:
        if dialect == 'postgresql':
            op.alter_column(
                tabela, coluna,
                type_=sa.Float(),
                existing_type=sa.Numeric(10, 2),
                existing_nullable=nullable,
                postgresql_using=f'{coluna}::double precision',
            )
        else:
            op.alter_column(
                tabela, coluna,
                type_=sa.Float(),
                existing_type=sa.Numeric(10, 2),
                existing_nullable=nullable,
            )
