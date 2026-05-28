"""Modelos do dominio: estoque.

Faz parte de `app.models` (split em multiplos arquivos por dominio
em 2026-05-21). Importar via `from app.models import X` continua
funcionando porque `app/models/__init__.py` re-exporta tudo.
"""

from app.extensions import db
from app.utils import agora


class MovimentacaoEstoque(db.Model):
    __tablename__ = 'movimentacao_estoque'

    id = db.Column(db.Integer, primary_key=True)
    materia_prima_id = db.Column(db.Integer, db.ForeignKey('materia_prima.id'), nullable=False)
    tipo = db.Column(db.String(10), nullable=False)  # 'entrada' ou 'saida'
    quantidade = db.Column(db.Float, nullable=False)
    preco_unitario = db.Column(db.Float)
    data = db.Column(db.DateTime, default=agora)
    referencia = db.Column(db.String(200))
    usuario_id = db.Column(db.Integer, db.ForeignKey('usuario.id'))
    # Fornecedor (opcional) — usado em entradas pra alimentar historico de preco
    fornecedor_id = db.Column(db.Integer, db.ForeignKey('fornecedor.id'))

    materia_prima = db.relationship('MateriaPrima', backref='movimentacoes')
    usuario = db.relationship('Usuario', backref='movimentacoes_estoque')
    fornecedor = db.relationship('Fornecedor')

    def __repr__(self):
        return f'<Movimentacao {self.tipo} {self.quantidade} MP={self.materia_prima_id}>'

class AlertaEstoque(db.Model):
    __tablename__ = 'alerta_estoque'

    id = db.Column(db.Integer, primary_key=True)
    materia_prima_id = db.Column(db.Integer, db.ForeignKey('materia_prima.id'), nullable=False, unique=True)
    estoque_minimo = db.Column(db.Float, nullable=False)

    materia_prima = db.relationship('MateriaPrima', backref='alerta_estoque', uselist=False)

    def __repr__(self):
        return f'<AlertaEstoque MP={self.materia_prima_id} min={self.estoque_minimo}>'

class EstoqueProducao(db.Model):
    __tablename__ = 'estoque_producao'

    id = db.Column(db.Integer, primary_key=True)
    receita_id = db.Column(db.Integer, db.ForeignKey('receita.id'), nullable=True)
    produto_id = db.Column(db.Integer, db.ForeignKey('produto.id'), nullable=True)
    quantidade = db.Column(db.Integer, default=0)
    # Nome digitado no balanco quando nao houve match com Receita/Produto.
    # Permite registrar a contagem fisica mesmo sem cadastro previo;
    # depois o admin vincula a uma receita/produto e isso volta a NULL.
    nome_pendente = db.Column(db.String(200), nullable=True)
    # Estado do item (apenas `backup` ou NULL=cru/padrao). Industria nao
    # mantem estoque assado (assa pra cumprir pedido e despacha).
    estado = db.Column(db.String(20), nullable=True)

    receita = db.relationship('Receita')
    produto = db.relationship('Produto')
    movimentacoes = db.relationship('MovEstoqueProducao', backref='estoque', cascade='all, delete-orphan')

    __table_args__ = (
        # Estoque da industria eh 1 linha por produto (estado vive so no pedido).
        # A trava de unicidade parcial eh criada na migracao
        # (app/migrations_legacy.py), apos consolidar duplicatas legadas.
        db.Index('ix_estoque_producao_receita', 'receita_id'),
        db.Index('ix_estoque_producao_produto', 'produto_id'),
    )

    @property
    def nome_item(self):
        if self.receita:
            return self.receita.nome
        if self.produto:
            return self.produto.nome
        if self.nome_pendente:
            return self.nome_pendente
        return '?'

    @property
    def nome_item_com_estado(self):
        from app.constants import render_item_com_estado
        return render_item_com_estado(self.nome_item, self.estado)

    @property
    def pendente(self):
        return self.receita_id is None and self.produto_id is None and bool(self.nome_pendente)

class MovEstoqueProducao(db.Model):
    __tablename__ = 'mov_estoque_producao'

    id = db.Column(db.Integer, primary_key=True)
    estoque_producao_id = db.Column(db.Integer, db.ForeignKey('estoque_producao.id'), nullable=False, index=True)
    tipo = db.Column(db.String(20), nullable=False)
    quantidade = db.Column(db.Integer, nullable=False)
    data = db.Column(db.DateTime, default=agora, index=True)
    referencia = db.Column(db.String(200))
    usuario_id = db.Column(db.Integer, db.ForeignKey('usuario.id'))


# ── Pedidos de Loja ──

class EstoqueLoja(db.Model):
    __tablename__ = 'estoque_loja'

    id = db.Column(db.Integer, primary_key=True)
    loja_id = db.Column(db.Integer, db.ForeignKey('loja.id'), nullable=False)
    receita_id = db.Column(db.Integer, db.ForeignKey('receita.id'), nullable=True)
    produto_id = db.Column(db.Integer, db.ForeignKey('produto.id'), nullable=True)
    materia_prima_id = db.Column(db.Integer, db.ForeignKey('materia_prima.id'), nullable=True)
    quantidade = db.Column(db.Integer, default=0)
    # Nome digitado em entrada-em-lote quando nao houve match com nenhum
    # cadastro. Mesma logica do EstoqueProducao.nome_pendente.
    nome_pendente = db.Column(db.String(200), nullable=True)
    # Estado do item (assado/backup/NULL=padrao). Loja pode ter 3 estados
    # ao mesmo tempo pra mesma receita: assado (vitrine) + backup (freezer
    # pra emergencia) + NULL (cru, raro).
    estado = db.Column(db.String(20), nullable=True)

    loja = db.relationship('Loja')
    receita = db.relationship('Receita')
    produto = db.relationship('Produto')
    materia_prima = db.relationship('MateriaPrima')
    movimentacoes = db.relationship('MovEstoqueLoja', backref='estoque', cascade='all, delete-orphan')

    __table_args__ = (
        # Uma linha por (loja, receita, estado) — permite multiplos
        # estados simultaneos.
        db.Index('ix_estoque_loja_loja_receita_estado',
                  'loja_id', 'receita_id', 'estado'),
        db.Index('ix_estoque_loja_loja_produto_estado',
                  'loja_id', 'produto_id', 'estado'),
    )

    @property
    def nome_item(self):
        if self.receita:
            return self.receita.nome
        if self.produto:
            return self.produto.nome
        if self.materia_prima:
            return self.materia_prima.nome + ' (MP)'
        if self.nome_pendente:
            return self.nome_pendente
        return '?'

    @property
    def nome_item_com_estado(self):
        from app.constants import render_item_com_estado
        return render_item_com_estado(self.nome_item, self.estado)

    @property
    def pendente(self):
        return (self.receita_id is None and self.produto_id is None
                and self.materia_prima_id is None and bool(self.nome_pendente))

class FotoRecebimento(db.Model):
    __tablename__ = 'foto_recebimento'

    id = db.Column(db.Integer, primary_key=True)
    pedido_id = db.Column(db.Integer, db.ForeignKey('pedido_loja.id'), nullable=False)
    # Storage: ver PedidoItemFoto pro padrao M6 (Dropbox preferido, BLOB legado).
    imagem = db.Column(db.LargeBinary, nullable=True)  # legado, nullable apos M6
    imagem_url = db.Column(db.String(500))  # shared link Dropbox
    imagem_storage_path = db.Column(db.String(500))
    mimetype = db.Column(db.String(100))
    enviada_em = db.Column(db.DateTime, default=agora)
    enviada_por = db.Column(db.Integer, db.ForeignKey('usuario.id'))

    pedido = db.relationship('PedidoLoja', backref=db.backref('fotos', cascade='all, delete-orphan'))


class PedidoItemFoto(db.Model):
    """Foto de conferencia por SKU de um pedido, em uma etapa do fluxo.

    Obrigatoria pra:
    - **saida** (industria → motorista): industria tira foto de cada item
      antes do QR de saida ser gerado. Sem foto de todos, nao gera QR.
    - **entrega** (motorista → loja): motorista tira foto de cada item
      antes do QR de entrega ser gerado. Sem foto de todos, loja nao recebe.

    Foto eh por SKU, nao por unidade — 1 foto cobre as 90 unidades de
    croissant tradicional, etc. Re-anexar substitui (delete + insert
    via UI; backend so insere/atualiza por API).
    """
    __tablename__ = 'pedido_item_foto'

    id = db.Column(db.Integer, primary_key=True)
    pedido_item_id = db.Column(db.Integer, db.ForeignKey('pedido_item.id'),
                                nullable=False, index=True)
    etapa = db.Column(db.String(10), nullable=False)  # 'saida' | 'entrega'
    # Storage da imagem — duas opcoes:
    # 1. Dropbox (preferido, novos uploads): imagem_url + imagem_storage_path.
    # 2. BLOB legado (fotos pre-migracao M6): imagem.
    # Serve route prioriza URL Dropbox quando preenchido.
    imagem = db.Column(db.LargeBinary, nullable=True)  # legado, nullable apos M6
    imagem_url = db.Column(db.String(500))  # shared link Dropbox
    imagem_storage_path = db.Column(db.String(500))  # path no Dropbox pra deletar
    mimetype = db.Column(db.String(100))
    criado_em = db.Column(db.DateTime, default=agora, nullable=False)
    criado_por_id = db.Column(db.Integer, db.ForeignKey('usuario.id'),
                               nullable=True)
    # Pra quando o motorista (sem Usuario.id no sistema) tirar foto:
    # capturamos o Driver.id em criado_por_driver_id.
    criado_por_driver_id = db.Column(db.Integer, db.ForeignKey('driver_entrega.id'),
                                       nullable=True)

    pedido_item = db.relationship('PedidoItem',
                                    backref=db.backref('fotos_conferencia',
                                                       cascade='all, delete-orphan'))

    __table_args__ = (
        # 1 foto unica por (item, etapa) — re-upload substitui.
        db.UniqueConstraint('pedido_item_id', 'etapa',
                             name='uq_pedidoitemfoto_item_etapa'),
    )

class MovEstoqueLoja(db.Model):
    __tablename__ = 'mov_estoque_loja'

    id = db.Column(db.Integer, primary_key=True)
    estoque_loja_id = db.Column(db.Integer, db.ForeignKey('estoque_loja.id'), nullable=False, index=True)
    # 50 pra caber 'venda_seru_sem_estoque' (22) e futuros tipos.
    tipo = db.Column(db.String(50), nullable=False)
    quantidade = db.Column(db.Integer, nullable=False)
    data = db.Column(db.DateTime, default=agora, index=True)
    referencia = db.Column(db.String(200))
    usuario_id = db.Column(db.Integer, db.ForeignKey('usuario.id'))


# ── Cartinha de Entrega (Vnda) ──

class Desperdicio(db.Model):
    """Registro de sobra do dia descartada na loja (vencida).

    Item identificado por receita/produto/MP (exclusivo). Cada registro gera
    um MovEstoqueLoja(tipo='desperdicio') que de fato baixa o estoque.
    """
    __tablename__ = 'desperdicio'

    id = db.Column(db.Integer, primary_key=True)
    loja_id = db.Column(db.Integer, db.ForeignKey('loja.id'), nullable=False, index=True)
    receita_id = db.Column(db.Integer, db.ForeignKey('receita.id'), nullable=True)
    produto_id = db.Column(db.Integer, db.ForeignKey('produto.id'), nullable=True)
    materia_prima_id = db.Column(db.Integer, db.ForeignKey('materia_prima.id'), nullable=True)
    quantidade = db.Column(db.Integer, nullable=False)
    data = db.Column(db.Date, nullable=False, default=lambda: agora().date(), index=True)
    motivo = db.Column(db.String(30), nullable=False, default='vencido')
    observacao = db.Column(db.Text)
    criado_em = db.Column(db.DateTime, default=agora)
    criado_por_id = db.Column(db.Integer, db.ForeignKey('usuario.id'))

    loja = db.relationship('Loja')
    receita = db.relationship('Receita')
    produto = db.relationship('Produto')
    materia_prima = db.relationship('MateriaPrima')
    criado_por = db.relationship('Usuario')

    @property
    def nome_item(self):
        if self.receita:
            return self.receita.nome
        if self.produto:
            return self.produto.nome
        if self.materia_prima:
            return self.materia_prima.nome + ' (MP)'
        return '?'


# ── Slack bot (copilot via DM/@mention) ───────────────────────────────
