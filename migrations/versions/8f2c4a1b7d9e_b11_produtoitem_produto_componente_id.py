"""B11: ProdutoItem aceita tipo='produto' com produto_componente_id.

Cesta agora pode conter Produto comprado pronto (ex: "Iogurte 200ml")
que a loja estoca como produto, sem receita e sem MP.

Antes:
- ProdutoItem.tipo so aceitava 'receita' ou 'mp'
- UI nao oferecia tipo='produto'
- Quem queria por iogurte pronto (Produto) era forcado a 'mp' → backfill
  por nome em MateriaPrima falhava → orfao silencioso

Depois:
- Coluna `produto_componente_id` (FK pra produto, nullable) — separa do
  `produto_id` que continua sendo a cesta pai
- Backfill conservador dos orfaos:
  pra cada ProdutoItem com tipo='mp' E materia_prima_id IS NULL, se
  item_nome casar EXATAMENTE com Produto.nome, converte pra tipo='produto'
  + produto_componente_id setado. Resto fica orfao e admin resolve em UI.

Revision ID: 8f2c4a1b7d9e
Revises: efb6e5837fd0
Create Date: 2026-05-21
"""
import logging

import sqlalchemy as sa
from alembic import op

revision = '8f2c4a1b7d9e'
down_revision = 'efb6e5837fd0'
branch_labels = None
depends_on = None

logger = logging.getLogger(__name__)


def upgrade():
    # 1) Adiciona coluna FK como nullable
    op.add_column('produto_item',
                  sa.Column('produto_componente_id', sa.Integer(),
                            nullable=True))
    op.create_foreign_key(
        'fk_produto_item_produto_componente',
        'produto_item', 'produto', ['produto_componente_id'], ['id'],
    )
    op.create_index('ix_produto_item_produto_componente_id',
                    'produto_item', ['produto_componente_id'])

    # 2) Backfill: orfaos tipo='mp' cujo item_nome casa com Produto.nome
    #    viram tipo='produto' + produto_componente_id setado.
    bind = op.get_bind()
    bind.execute(sa.text("""
        UPDATE produto_item
        SET tipo = 'produto',
            produto_componente_id = (
                SELECT id FROM produto
                WHERE produto.nome = produto_item.item_nome LIMIT 1
            )
        WHERE tipo = 'mp'
          AND materia_prima_id IS NULL
          AND EXISTS (
            SELECT 1 FROM produto WHERE produto.nome = produto_item.item_nome
          )
    """))

    # 3) Loga quantos sobraram orfaos por categoria
    n_r = bind.execute(sa.text(
        "SELECT COUNT(*) FROM produto_item "
        "WHERE tipo = 'receita' AND receita_id IS NULL"
    )).scalar() or 0
    n_p = bind.execute(sa.text(
        "SELECT COUNT(*) FROM produto_item "
        "WHERE tipo = 'produto' AND produto_componente_id IS NULL"
    )).scalar() or 0
    n_m = bind.execute(sa.text(
        "SELECT COUNT(*) FROM produto_item "
        "WHERE tipo = 'mp' AND materia_prima_id IS NULL"
    )).scalar() or 0
    logger.warning(
        'B11 backfill: orfaos restantes — receita=%d, produto=%d, mp=%d. '
        'Admin resolve em /produtos/cestas/orfaos.',
        n_r, n_p, n_m,
    )


def downgrade():
    op.drop_index('ix_produto_item_produto_componente_id',
                  table_name='produto_item')
    op.drop_constraint('fk_produto_item_produto_componente', 'produto_item',
                       type_='foreignkey')
    op.drop_column('produto_item', 'produto_componente_id')
