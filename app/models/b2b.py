"""Modelos do dominio: b2b.

Faz parte de `app.models` (split em multiplos arquivos por dominio
em 2026-05-21). Importar via `from app.models import X` continua
funcionando porque `app/models/__init__.py` re-exporta tudo.
"""

from app.extensions import db
from app.utils import agora, hoje


class ClienteB2B(db.Model):
    """Cliente B2B recorrente (hotel, restaurante, cafeteria, padaria).

    Cadastro opcional: vendas avulsas usam VendaB2B.cliente_nome em vez de
    cliente_id. Cliente recorrente eh util pra historico, contas a receber
    consolidadas e preco diferenciado (campo desconto_percentual).
    """
    __tablename__ = 'cliente_b2b'

    id = db.Column(db.Integer, primary_key=True)
    nome = db.Column(db.String(150), nullable=False, unique=True)
    cnpj_cpf = db.Column(db.String(20))
    telefone = db.Column(db.String(30))
    email = db.Column(db.String(120))
    endereco = db.Column(db.String(250))
    contato = db.Column(db.String(100))  # nome da pessoa que compra
    desconto_percentual = db.Column(db.Float, default=0)  # % sobre preco atacado
    observacao = db.Column(db.Text)
    ativo = db.Column(db.Boolean, default=True, nullable=False)
    criado_em = db.Column(db.DateTime, default=agora)

class VendaB2B(db.Model):
    """Venda B2B: cabecalho. Itens vinculados via VendaB2BItem,
    pagamento parcelado via VendaB2BParcela.

    Estoque eh baixado do EstoqueProducao (industria) ao salvar.
    Cancelamento estorna automaticamente.
    """
    __tablename__ = 'venda_b2b'

    id = db.Column(db.Integer, primary_key=True)
    data_venda = db.Column(db.Date, nullable=False, default=lambda: agora().date(), index=True)
    cliente_id = db.Column(db.Integer, db.ForeignKey('cliente_b2b.id'), nullable=True, index=True)
    cliente_nome = db.Column(db.String(150))  # pra venda avulsa sem cadastro
    status = db.Column(db.String(20), default='ativa', nullable=False)  # ativa, cancelada
    # Numeric(10, 2): precisao exata em centavos. Float dava erro de
    # arredondamento em soma de parcelas (R$33,33 × 3 != R$100,00).
    valor_total = db.Column(db.Numeric(10, 2), nullable=False, default=0)
    observacao = db.Column(db.Text)
    nf_numero = db.Column(db.String(50))  # numero da NF se houver
    criado_por_id = db.Column(db.Integer, db.ForeignKey('usuario.id'))
    criado_em = db.Column(db.DateTime, default=agora)
    cancelado_em = db.Column(db.DateTime, nullable=True)
    cancelado_por_id = db.Column(db.Integer, db.ForeignKey('usuario.id'), nullable=True)

    cliente = db.relationship('ClienteB2B')
    itens = db.relationship('VendaB2BItem', backref='venda', cascade='all, delete-orphan')
    parcelas = db.relationship('VendaB2BParcela', backref='venda', cascade='all, delete-orphan')
    criado_por = db.relationship('Usuario', foreign_keys=[criado_por_id])

    @property
    def cliente_display(self):
        if self.cliente:
            return self.cliente.nome
        return self.cliente_nome or '(avulso)'

    @property
    def valor_pago(self):
        from decimal import Decimal
        total = Decimal('0')
        for p in self.parcelas:
            total += Decimal(p.valor_pago or 0)
        return total

    @property
    def valor_aberto(self):
        from decimal import Decimal
        return Decimal(self.valor_total or 0) - self.valor_pago

class VendaB2BItem(db.Model):
    __tablename__ = 'venda_b2b_item'

    id = db.Column(db.Integer, primary_key=True)
    venda_id = db.Column(db.Integer, db.ForeignKey('venda_b2b.id'), nullable=False, index=True)
    receita_id = db.Column(db.Integer, db.ForeignKey('receita.id'), nullable=True)
    produto_id = db.Column(db.Integer, db.ForeignKey('produto.id'), nullable=True)
    quantidade = db.Column(db.Integer, nullable=False)
    preco_unitario = db.Column(db.Numeric(10, 2), nullable=False)
    desconto_percentual = db.Column(db.Float, default=0)  # %, nao R$

    receita = db.relationship('Receita')
    produto = db.relationship('Produto')

    @property
    def nome_item(self):
        if self.receita:
            return self.receita.nome
        if self.produto:
            return self.produto.nome
        return '?'

    @property
    def valor_total(self):
        # Decimal: preco_unitario eh Numeric (vem como Decimal do SQLA).
        # quantidade eh int, desconto_percentual eh float — convertemos.
        from decimal import ROUND_HALF_UP, Decimal
        qtd = Decimal(int(self.quantidade or 0))
        preco = Decimal(self.preco_unitario or 0)
        bruto = qtd * preco
        desc_pct = Decimal(str(self.desconto_percentual or 0))
        desc = bruto * desc_pct / Decimal('100')
        return (bruto - desc).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

class VendaB2BParcela(db.Model):
    """Cada parcela tem vencimento, valor previsto e valor recebido.

    Pagamento parcial e permitido: valor_pago < valor → fica em aberto pelo
    saldo. valor_pago == valor → quitada (campo pago_em preenchido).
    """
    __tablename__ = 'venda_b2b_parcela'

    id = db.Column(db.Integer, primary_key=True)
    venda_id = db.Column(db.Integer, db.ForeignKey('venda_b2b.id'), nullable=False, index=True)
    numero = db.Column(db.Integer, nullable=False)  # 1, 2, 3...
    vencimento = db.Column(db.Date, nullable=False, index=True)
    valor = db.Column(db.Numeric(10, 2), nullable=False)
    valor_pago = db.Column(db.Numeric(10, 2), default=0)
    pago_em = db.Column(db.DateTime, nullable=True)
    forma_pagamento = db.Column(db.String(30))  # pix, dinheiro, boleto, transferencia
    observacao = db.Column(db.String(200))

    @property
    def saldo(self):
        return (self.valor or 0) - (self.valor_pago or 0)

    @property
    def status(self):
        # Numeric(10, 2) garante precisao exata — comparacao direta.
        # (Tolerancia de 1 centavo nao eh mais necessaria desde a
        # migration 643bd66e89c3.)
        if self.valor_pago and self.valor_pago >= (self.valor or 0):
            return 'pago'
        if self.valor_pago and self.valor_pago > 0:
            return 'parcial'
        if self.vencimento < hoje():
            return 'atrasado'
        return 'aberto'


# ── Handshake QR Code (saida industria + entrega loja) ──

class VendaManualLoja(db.Model):
    """Vendas lancadas manualmente pra lojas sem API de PDV (ex: Anesio).

    NAO baixa estoque — eh so um registro pra alimentar previsao de
    demanda e sugestao de pedido. Vendas reais via Seru/VNDA continuam
    em MovEstoqueLoja. Loja com PDV API: usa essas movimentacoes
    automaticas. Loja sem PDV: admin lanca daqui periodicamente.
    """
    __tablename__ = 'venda_manual_loja'

    id = db.Column(db.Integer, primary_key=True)
    loja_id = db.Column(db.Integer, db.ForeignKey('loja.id'), nullable=False, index=True)
    data_venda = db.Column(db.Date, nullable=False, index=True)
    receita_id = db.Column(db.Integer, db.ForeignKey('receita.id'), nullable=True)
    produto_id = db.Column(db.Integer, db.ForeignKey('produto.id'), nullable=True)
    materia_prima_id = db.Column(db.Integer, db.ForeignKey('materia_prima.id'), nullable=True)
    quantidade = db.Column(db.Integer, nullable=False)
    valor_unitario = db.Column(db.Numeric(10, 2))  # opcional
    observacao = db.Column(db.String(200))
    criado_por_id = db.Column(db.Integer, db.ForeignKey('usuario.id'))
    criado_em = db.Column(db.DateTime, default=agora)

    loja = db.relationship('Loja')
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
            return self.materia_prima.nome
        return '?'
