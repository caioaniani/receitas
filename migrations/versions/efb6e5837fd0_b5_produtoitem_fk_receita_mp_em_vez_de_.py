"""B5: ProdutoItem com FK pra Receita/MateriaPrima (em vez de item_nome string).

Bug original: `cestas.componentes_de_cesta` buscava `Receita.query.filter_by(
nome=pi.item_nome)`. Case-sensitive, sem fuzzy. Se admin renomeasse uma
receita usada em cesta sem atualizar `item_nome`, o componente sumia
silenciosamente da baixa de estoque.

Fix canonico:
1. Adicionar `receita_id` e `materia_prima_id` em `produto_item` (FK).
2. Backfill por nome exato — `item_nome` casa com `Receita.nome` ou
   `MateriaPrima.nome`. Sucesso eh marcado nas FKs; falha vira orfao
   (FK NULL) que admin resolve em /cestas/orfaos.
3. `item_nome` continua existindo (por compat) mas baixa de estoque
   usa exclusivamente FK.

Backfill: conservador (opcao A). Itens nao-resolvidos ficam com FK NULL
+ log WARNING. Nada falha. Admin tem que vincular manualmente.

Revision ID: efb6e5837fd0
Revises: 643bd66e89c3
Create Date: 2026-05-21
"""
import logging

import sqlalchemy as sa
from alembic import op

revision = 'efb6e5837fd0'
down_revision = '643bd66e89c3'
branch_labels = None
depends_on = None

logger = logging.getLogger(__name__)


def upgrade():
    # 1) Adiciona colunas FK como nullable
    op.add_column('produto_item',
                  sa.Column('receita_id', sa.Integer(), nullable=True))
    op.add_column('produto_item',
                  sa.Column('materia_prima_id', sa.Integer(), nullable=True))
    op.create_foreign_key(
        'fk_produto_item_receita',
        'produto_item', 'receita', ['receita_id'], ['id'],
    )
    op.create_foreign_key(
        'fk_produto_item_materia_prima',
        'produto_item', 'materia_prima', ['materia_prima_id'], ['id'],
    )
    op.create_index('ix_produto_item_receita_id', 'produto_item', ['receita_id'])
    op.create_index('ix_produto_item_materia_prima_id', 'produto_item',
                     ['materia_prima_id'])

    # 2) Backfill por nome exato — match case-sensitive (mesmo comportamento
    # do codigo anterior — se nome bate, vincula; senao fica orfao).
    bind = op.get_bind()

    bind.execute(sa.text("""
        UPDATE produto_item
        SET receita_id = (
            SELECT id FROM receita WHERE nome = produto_item.item_nome LIMIT 1
        )
        WHERE tipo = 'receita' AND receita_id IS NULL
    """))

    bind.execute(sa.text("""
        UPDATE produto_item
        SET materia_prima_id = (
            SELECT id FROM materia_prima WHERE nome = produto_item.item_nome LIMIT 1
        )
        WHERE tipo = 'mp' AND materia_prima_id IS NULL
    """))

    # 3) Loga quantos ficaram orfaos
    orfaos_receita = bind.execute(sa.text(
        "SELECT COUNT(*) FROM produto_item "
        "WHERE tipo = 'receita' AND receita_id IS NULL"
    )).scalar() or 0
    orfaos_mp = bind.execute(sa.text(
        "SELECT COUNT(*) FROM produto_item "
        "WHERE tipo = 'mp' AND materia_prima_id IS NULL"
    )).scalar() or 0

    if orfaos_receita or orfaos_mp:
        logger.warning(
            'B5 backfill: %d ProdutoItem(s) com tipo=receita orfaos, '
            '%d com tipo=mp orfaos. Admin deve resolver em /cestas/orfaos.',
            orfaos_receita, orfaos_mp,
        )
    else:
        logger.info('B5 backfill: todos os ProdutoItem resolveram FK.')


def downgrade():
    op.drop_index('ix_produto_item_materia_prima_id', table_name='produto_item')
    op.drop_index('ix_produto_item_receita_id', table_name='produto_item')
    op.drop_constraint('fk_produto_item_materia_prima', 'produto_item',
                       type_='foreignkey')
    op.drop_constraint('fk_produto_item_receita', 'produto_item',
                       type_='foreignkey')
    op.drop_column('produto_item', 'materia_prima_id')
    op.drop_column('produto_item', 'receita_id')
