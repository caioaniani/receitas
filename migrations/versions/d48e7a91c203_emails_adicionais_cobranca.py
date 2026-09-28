"""Endereços adicionais do cliente e snapshot de cópias no envio.

Expansão nullable: não altera destinatários nem inventa histórico anterior.
"""
import sqlalchemy as sa
from alembic import op

revision = 'd48e7a91c203'
down_revision = 'a9d4e7b2c610'
branch_labels = None
depends_on = None


def upgrade():
    insp = sa.inspect(op.get_bind())
    for tabela, coluna in (('cliente_b2b', 'emails_cobranca'), ('envio_cobranca', 'copias')):
        colunas = {c['name'] for c in insp.get_columns(tabela)}
        if coluna not in colunas:
            op.add_column(tabela, sa.Column(coluna, sa.JSON(), nullable=True))


def downgrade():
    op.drop_column('envio_cobranca', 'copias')
    op.drop_column('cliente_b2b', 'emails_cobranca')
