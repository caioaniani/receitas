"""Outbox da entrega B2B, avisos Sicredi e delegação fiscal — somente tabelas novas."""
import sqlalchemy as sa
from alembic import op

revision = 'c83a91d52f06'
down_revision = 'b7248c1d9e02'
branch_labels = None
depends_on = None


def upgrade():
    existentes = set(sa.inspect(op.get_bind()).get_table_names())
    if 'delegacao_fiscal_b2b' not in existentes:
        op.create_table('delegacao_fiscal_b2b',
                        sa.Column('usuario_id', sa.Integer(), sa.ForeignKey('usuario.id', ondelete='CASCADE'), primary_key=True),
                        sa.Column('concedida_por_id', sa.Integer(), nullable=False),
                        sa.Column('concedida_em', sa.DateTime(), nullable=False))
    if 'tentativa_nf_b2b' not in existentes:
        op.create_table('tentativa_nf_b2b',
                        sa.Column('chave', sa.String(60), primary_key=True),
                        sa.Column('estado', sa.String(20), nullable=False),
                        sa.Column('iniciada_em', sa.DateTime(), nullable=False),
                        sa.Column('usuario_id', sa.Integer()), sa.Column('erro', sa.String(500)),
                        sa.Column('assinatura', sa.String(64)))
    if 'automacao_cobranca' not in existentes:
        op.create_table('automacao_cobranca',
                        sa.Column('id', sa.Integer(), primary_key=True),
                        sa.Column('chave', sa.String(60), nullable=False, unique=True),
                        sa.Column('tipo', sa.String(10), nullable=False),
                        sa.Column('documento_id', sa.Integer(), nullable=False),
                        sa.Column('referencia', sa.String(120), nullable=False),
                        sa.Column('usuario_id', sa.Integer()),
                        sa.Column('criado_em', sa.DateTime(), nullable=False),
                        sa.Column('atualizado_em', sa.DateTime(), nullable=False),
                        sa.Column('estado', sa.String(30), nullable=False),
                        sa.Column('erro', sa.String(500)),
                        sa.Column('cobranca_ids', sa.JSON(), nullable=False))
        op.create_index('ix_automacao_cobranca_estado', 'automacao_cobranca', ['estado'])
    if 'aviso_remessa' not in existentes:
        op.create_table('aviso_remessa',
                        sa.Column('id', sa.Integer(), primary_key=True),
                        sa.Column('remessa_id', sa.Integer(), nullable=False),
                        sa.Column('destinatario', sa.String(254), nullable=False),
                        sa.Column('estado', sa.String(20), nullable=False),
                        sa.Column('criado_em', sa.DateTime(), nullable=False),
                        sa.Column('enviado_em', sa.DateTime()),
                        sa.Column('provedor_id', sa.String(150)), sa.Column('erro', sa.String(500)),
                        sa.UniqueConstraint('remessa_id', 'destinatario', name='uq_aviso_remessa_destino'))
        op.create_index('ix_aviso_remessa_remessa_id', 'aviso_remessa', ['remessa_id'])
    if 'confirmacao_registro_boleto' not in existentes:
        op.create_table('confirmacao_registro_boleto',
                        sa.Column('cobranca_id', sa.Integer(), primary_key=True),
                        sa.Column('remessa_id', sa.Integer(), nullable=False),
                        sa.Column('nosso_numero', sa.String(9), nullable=False),
                        sa.Column('valor', sa.Numeric(10, 2), nullable=False),
                        sa.Column('vencimento', sa.Date(), nullable=False),
                        sa.Column('usuario_id', sa.Integer(), nullable=False),
                        sa.Column('confirmado_em', sa.DateTime(), nullable=False))


def downgrade():
    # Rollback operacional: reverter código sem apagar o histórico financeiro.
    # A remoção explícita destas tabelas exige backup e decisão humana.
    pass
