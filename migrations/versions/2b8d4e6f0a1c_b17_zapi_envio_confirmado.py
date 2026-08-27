"""B17: guarda o identificador confirmado de envios Z-API.

Revision ID: 2b8d4e6f0a1c
Revises: 1f7a3c9d8e4b
Create Date: 2026-08-27
"""
import sqlalchemy as sa
from alembic import op

revision = '2b8d4e6f0a1c'
down_revision = '1f7a3c9d8e4b'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        'notificacao_whatsapp',
        sa.Column('zaap_id', sa.String(length=120), nullable=True),
    )
    op.create_index(
        'ix_notificacao_whatsapp_zaap_id',
        'notificacao_whatsapp', ['zaap_id'], unique=False,
    )


def downgrade():
    op.drop_index(
        'ix_notificacao_whatsapp_zaap_id',
        table_name='notificacao_whatsapp',
    )
    op.drop_column('notificacao_whatsapp', 'zaap_id')
