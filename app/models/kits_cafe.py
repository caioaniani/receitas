"""Kits montados pelo owner e compras com entregas agendadas. Tabelas novas."""
from app.extensions import db
from app.utils import agora


class KitCafe(db.Model):
    __tablename__ = 'kit_cafe'
    id = db.Column(db.Integer, primary_key=True)
    nome = db.Column(db.String(150), nullable=False)
    descricao = db.Column(db.String(600))
    ativo = db.Column(db.Boolean, nullable=False, default=False)
    usuario_id = db.Column(db.Integer, db.ForeignKey('usuario.id'), nullable=False)
    criado_em = db.Column(db.DateTime, nullable=False, default=agora)
    atualizado_em = db.Column(db.DateTime, nullable=False, default=agora, onupdate=agora)
    itens = db.relationship('KitCafeItem', backref='kit', cascade='all, delete-orphan',
                             order_by='KitCafeItem.id')
    sucos = db.relationship('KitCafeSuco', backref='kit', cascade='all, delete-orphan',
                            order_by='KitCafeSuco.produto_id')
    opcoes = db.relationship('KitCafeOpcao', backref='kit', cascade='all, delete-orphan',
                             order_by='KitCafeOpcao.grupo, KitCafeOpcao.id')


class KitCafeSuco(db.Model):
    """Opções de um suco por entrega, escolhidas pelo owner no catálogo."""
    __tablename__ = 'kit_cafe_suco'
    kit_id = db.Column(db.Integer, db.ForeignKey('kit_cafe.id'), primary_key=True)
    produto_id = db.Column(db.Integer, db.ForeignKey('produto.id'), primary_key=True)


class KitCafeOpcao(db.Model):
    """Um croissant e/ou sourdough escolhidos entre opções do próprio kit."""
    __tablename__ = 'kit_cafe_opcao'
    id = db.Column(db.Integer, primary_key=True)
    kit_id = db.Column(db.Integer, db.ForeignKey('kit_cafe.id'), nullable=False, index=True)
    grupo = db.Column(db.String(20), nullable=False)
    kind = db.Column(db.String(10), nullable=False)
    receita_id = db.Column(db.Integer, db.ForeignKey('receita.id'))
    produto_id = db.Column(db.Integer, db.ForeignKey('produto.id'))
    __table_args__ = (
        db.CheckConstraint("grupo IN ('croissant', 'sourdough')", name='ck_kit_cafe_opcao_grupo'),
        db.CheckConstraint("(kind = 'receita' AND receita_id IS NOT NULL AND produto_id IS NULL) "
                           "OR (kind = 'produto' AND produto_id IS NOT NULL AND receita_id IS NULL)",
                           name='ck_kit_cafe_opcao_alvo'),
        db.UniqueConstraint('kit_id', 'receita_id', name='uq_kit_cafe_opcao_receita'),
        db.UniqueConstraint('kit_id', 'produto_id', name='uq_kit_cafe_opcao_produto'),
    )


class KitCafeItem(db.Model):
    __tablename__ = 'kit_cafe_item'
    id = db.Column(db.Integer, primary_key=True)
    kit_id = db.Column(db.Integer, db.ForeignKey('kit_cafe.id'), nullable=False, index=True)
    kind = db.Column(db.String(10), nullable=False)
    receita_id = db.Column(db.Integer, db.ForeignKey('receita.id'))
    produto_id = db.Column(db.Integer, db.ForeignKey('produto.id'))
    quantidade = db.Column(db.Integer, nullable=False)
    # Configuração do menu escolhida pelo owner, quando o componente é um menu.
    comp_json = db.Column(db.Text)
    __table_args__ = (
        db.CheckConstraint('quantidade > 0', name='ck_kit_cafe_item_quantidade'),
        db.CheckConstraint("(kind = 'receita' AND receita_id IS NOT NULL AND produto_id IS NULL) "
                           "OR (kind = 'produto' AND produto_id IS NOT NULL AND receita_id IS NULL)",
                           name='ck_kit_cafe_item_alvo'),
    )


class CompraKit(db.Model):
    __tablename__ = 'compra_kit'
    id = db.Column(db.Integer, primary_key=True)
    kit_id = db.Column(db.Integer, db.ForeignKey('kit_cafe.id'), nullable=False, index=True)
    kit_nome = db.Column(db.String(150), nullable=False)
    pedido_principal_id = db.Column(db.Integer, db.ForeignKey('pedido_online.id'),
                                    nullable=False, unique=True)
    subtotal = db.Column(db.Numeric(10, 2), nullable=False)
    frete_total = db.Column(db.Numeric(10, 2), nullable=False)
    valor_total = db.Column(db.Numeric(10, 2), nullable=False)
    criado_em = db.Column(db.DateTime, nullable=False, default=agora)
    expira_em = db.Column(db.DateTime, nullable=False, index=True)
    # Estado financeiro do ciclo é independente da entrega do primeiro kit.
    pago_em = db.Column(db.DateTime)
    checkout_token = db.Column(db.String(64), nullable=False, unique=True)
    pedido_principal = db.relationship('PedidoOnline', foreign_keys=[pedido_principal_id])
    entregas = db.relationship('EntregaKit', backref='compra', order_by='EntregaKit.ordem')


class EntregaKit(db.Model):
    __tablename__ = 'entrega_kit'
    pedido_id = db.Column(db.Integer, db.ForeignKey('pedido_online.id'), primary_key=True)
    compra_id = db.Column(db.Integer, db.ForeignKey('compra_kit.id'), nullable=False, index=True)
    ordem = db.Column(db.Integer, nullable=False)
    # Baixa dos itens de prateleira ocorre na coleta desta entrega, não na compra do mês.
    coletado_em = db.Column(db.DateTime)
    # Capacidade da data, independente do estoque físico. Criada no checkout,
    # liberada ao expirar e mantida ao pagar (sem reservar novamente).
    reserva_plano = db.Column(db.Boolean, nullable=False, default=False)
    # Pagamento tardio pode chegar após redistribuição da capacidade liberada.
    alerta_capacidade = db.Column(db.Text)
    pedido = db.relationship('PedidoOnline')
    __table_args__ = (db.UniqueConstraint('compra_id', 'ordem', name='uq_entrega_kit_ordem'),)

    @property
    def id(self):
        return self.pedido_id


class ReembolsoKit(db.Model):
    """Uma devolução financeira por entrega; tentativa incerta nunca é reenviada."""
    __tablename__ = 'reembolso_kit'
    pedido_id = db.Column(db.Integer, db.ForeignKey('pedido_online.id'), primary_key=True)
    valor = db.Column(db.Numeric(10, 2), nullable=False)
    pagarme_charge_id = db.Column(db.String(60), nullable=False)
    status = db.Column(db.String(20), nullable=False, default='solicitado')
    erro = db.Column(db.Text)
    criado_em = db.Column(db.DateTime, nullable=False, default=agora)
    confirmado_em = db.Column(db.DateTime)

    @property
    def id(self):
        return self.pedido_id


class TarefaFiscalKit(db.Model):
    """Fila durável de NF por entrega, persistida junto ao pagamento do mês."""
    __tablename__ = 'tarefa_fiscal_kit'
    pedido_id = db.Column(db.Integer, db.ForeignKey('pedido_online.id'), primary_key=True)
    criado_em = db.Column(db.DateTime, nullable=False, default=agora)
    concluido_em = db.Column(db.DateTime)
    email_enviado_em = db.Column(db.DateTime)
    tentativas = db.Column(db.Integer, nullable=False, default=0)
    erro = db.Column(db.Text)
    proxima_tentativa_em = db.Column(db.DateTime, nullable=False, default=agora, index=True)

    @property
    def id(self):
        return self.pedido_id
