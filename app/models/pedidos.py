"""Modelos do dominio: pedidos.

Faz parte de `app.models` (split em multiplos arquivos por dominio
em 2026-05-21). Importar via `from app.models import X` continua
funcionando porque `app/models/__init__.py` re-exporta tudo.
"""

from app.extensions import db
from app.utils import agora, hoje


class PedidoLoja(db.Model):
    __tablename__ = 'pedido_loja'

    id = db.Column(db.Integer, primary_key=True)
    loja_id = db.Column(db.Integer, db.ForeignKey('loja.id'), nullable=False)
    data_pedido = db.Column(db.Date, default=hoje)
    data_entrega = db.Column(db.Date)
    status = db.Column(db.String(20), default='pendente')
    observacao = db.Column(db.Text)
    criado_por = db.Column(db.Integer, db.ForeignKey('usuario.id'))
    criado_em = db.Column(db.DateTime, default=agora)
    # Motorista que pegou esse pedido na industria (handshake de saida).
    # Painel /driver/<token> filtra por isso pra cada motorista so ver os
    # pedidos que ele coletou.
    driver_id = db.Column(db.Integer, db.ForeignKey('driver_entrega.id'),
                           nullable=True, index=True)

    loja = db.relationship('Loja', backref='pedidos')
    criador = db.relationship('Usuario')
    driver = db.relationship('Driver', foreign_keys=[driver_id])
    itens = db.relationship('PedidoItem', backref='pedido', cascade='all, delete-orphan')
    qrcodes = db.relationship('PedidoQRCode', back_populates='pedido', cascade='all, delete-orphan')

    @property
    def tem_divergencia(self):
        return any(
            i.quantidade_recebida is not None and i.quantidade_recebida != i.quantidade
            for i in self.itens
        )

    @property
    def itens_divergentes(self):
        return [
            i for i in self.itens
            if i.quantidade_recebida is not None and i.quantidade_recebida != i.quantidade
        ]

class PedidoItem(db.Model):
    __tablename__ = 'pedido_item'

    id = db.Column(db.Integer, primary_key=True)
    pedido_id = db.Column(db.Integer, db.ForeignKey('pedido_loja.id'), nullable=False, index=True)
    receita_id = db.Column(db.Integer, db.ForeignKey('receita.id'), nullable=True)
    produto_id = db.Column(db.Integer, db.ForeignKey('produto.id'), nullable=True)
    materia_prima_id = db.Column(db.Integer, db.ForeignKey('materia_prima.id'), nullable=True)
    quantidade = db.Column(db.Integer, nullable=False)
    quantidade_recebida = db.Column(db.Integer, nullable=True)
    observacao = db.Column(db.String(200))

    receita = db.relationship('Receita')
    produto = db.relationship('Produto')
    materia_prima = db.relationship('MateriaPrima')

    @property
    def nome_item(self):
        if self.receita:
            return self.receita.nome
        if self.produto:
            return self.produto.nome
        if self.materia_prima:
            return self.materia_prima.nome + ' (MP)'
        return '?'


# ── Estoque de Loja ──

class PedidoLocal(db.Model):
    """Pedido cadastrado manualmente, fora do VNDA. Aparece junto com os
    pedidos VNDA na operacao do dia."""
    __tablename__ = 'pedido_local'

    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(50), unique=True, nullable=False, index=True)
    destinatario = db.Column(db.String(200), nullable=False)
    telefone = db.Column(db.String(50), nullable=False)
    endereco = db.Column(db.String(500), nullable=False)
    data_entrega = db.Column(db.Date, nullable=False, index=True)
    periodo = db.Column(db.String(80))
    cartinha = db.Column(db.Text)
    observacao = db.Column(db.Text)
    criado_em = db.Column(db.DateTime, default=agora)
    criado_por = db.Column(db.Integer, db.ForeignKey('usuario.id'))

    itens = db.relationship('PedidoLocalItem', backref='pedido', cascade='all, delete-orphan', lazy='joined')

    @property
    def total(self):
        return sum((i.quantidade or 0) * (i.preco_unitario or 0) for i in self.itens)

class PedidoLocalItem(db.Model):
    __tablename__ = 'pedido_local_item'

    id = db.Column(db.Integer, primary_key=True)
    pedido_local_id = db.Column(db.Integer, db.ForeignKey('pedido_local.id', ondelete='CASCADE'), nullable=False, index=True)
    nome = db.Column(db.String(200), nullable=False)
    quantidade = db.Column(db.Integer, default=1)
    preco_unitario = db.Column(db.Float, default=0)

class LembretePedidoOptOut(db.Model):
    """Marcador que uma loja NAO vai fazer pedido em uma data especifica.

    Quando alguem clica "nao vai ter pedido" no lembrete do Slack, cria
    uma linha aqui. O job de lembrete checa isso pra nao repetir.
    """
    __tablename__ = 'lembrete_pedido_optout'

    id = db.Column(db.Integer, primary_key=True)
    loja_id = db.Column(db.Integer, db.ForeignKey('loja.id'), nullable=False, index=True)
    data_entrega = db.Column(db.Date, nullable=False, index=True)
    marcado_por_slack_uid = db.Column(db.String(30))
    marcado_por_id = db.Column(db.Integer, db.ForeignKey('usuario.id'))
    criado_em = db.Column(db.DateTime, default=agora)

    loja = db.relationship('Loja')
    marcado_por = db.relationship('Usuario')

    __table_args__ = (
        db.UniqueConstraint('loja_id', 'data_entrega', name='uq_lembrete_optout'),
    )


# ── B2B (venda da industria pra clientes externos) ──

class PedidoQRCode(db.Model):
    """Token unico pra um handshake fisico via QR Code.

    Cenarios:
    - tipo='saida': producao gera; motorista escaneia + digita PIN do
      Driver → status separado → em_transporte.
    - tipo='entrega': motorista gera no /driver/<token>; funcionario da
      loja escaneia + digita PIN da Loja → status em_transporte → entregue.

    TTL implicito de 2h (campo expira_em). Apos usado, usado_em e
    usado_por_id ficam preenchidos pra auditoria.
    """
    __tablename__ = 'pedido_qrcode'

    id = db.Column(db.Integer, primary_key=True)
    token = db.Column(db.String(40), unique=True, nullable=False, index=True)
    pedido_id = db.Column(db.Integer, db.ForeignKey('pedido_loja.id'),
                           nullable=False, index=True)
    tipo = db.Column(db.String(10), nullable=False)  # 'saida' | 'entrega'
    criado_em = db.Column(db.DateTime, default=agora, nullable=False)
    criado_por_id = db.Column(db.Integer, db.ForeignKey('usuario.id'))
    expira_em = db.Column(db.DateTime, nullable=False)
    usado_em = db.Column(db.DateTime, nullable=True)
    usado_por_descricao = db.Column(db.String(100))  # 'driver:Joao' | 'loja:Anesio'

    pedido = db.relationship('PedidoLoja', back_populates='qrcodes')
    criado_por = db.relationship('Usuario')

    @property
    def valido(self):
        """True se nao expirou nem foi usado."""
        return self.usado_em is None and self.expira_em > agora()

class HandshakeAudit(db.Model):
    """Log de cada tentativa de handshake (scan + PIN), pra diagnosticar
    pedidos que ficaram "travados" no fluxo. Registra sucesso E falha."""
    __tablename__ = 'handshake_audit'

    id = db.Column(db.Integer, primary_key=True)
    momento = db.Column(db.DateTime, default=agora, nullable=False, index=True)
    token = db.Column(db.String(40), index=True)
    # ondelete='SET NULL' pra preservar audit como historico quando o
    # pedido eh deletado (admin excluindo via /pedidos/<id>/excluir).
    pedido_id = db.Column(db.Integer,
                           db.ForeignKey('pedido_loja.id', ondelete='SET NULL'),
                           index=True)
    tipo = db.Column(db.String(10))  # 'saida' | 'entrega'
    etapa = db.Column(db.String(20), nullable=False)  # 'scan' | 'pin_ok' | 'pin_fail' | 'erro_status' | 'erro_executor' | 'sucesso'
    detalhe = db.Column(db.String(500))
    status_pedido = db.Column(db.String(20))  # status do pedido NA HORA da tentativa
    ip = db.Column(db.String(45))
    user_agent = db.Column(db.String(300))
