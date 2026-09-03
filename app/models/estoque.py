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
    # 50 pra caber 'venda_b2b_sem_estoque' (21) e futuros tipos. Migrado em
    # _migrate_postgres() — mesma logica do mov_estoque_loja.tipo.
    tipo = db.Column(db.String(50), nullable=False)
    quantidade = db.Column(db.Integer, nullable=False)
    data = db.Column(db.DateTime, default=agora, index=True)
    referencia = db.Column(db.String(200))
    usuario_id = db.Column(db.Integer, db.ForeignKey('usuario.id'))


class ConsumoSubFracao(db.Model):
    """Acumulador de FRACAO no consumo de sub-receita da industria.

    O estoque do congelado e INTEIRO (bolas de massa, unidades), mas o
    consumo por lote e fracionario — a batida de 50 croissants consome
    1,26 bola de massa para folhar (90g/un, bola de 3.580g). Arredondar
    por lote desandava o saldo (~meia bola somia/sobrava por dia); aqui a
    fracao ACUMULA por sub-receita e baixa 1 inteiro quando fecha — mesmo
    padrao do SeruDebito nas vendas fracionadas do PDV. Decisao do dono
    03/07/2026 (caso Massa para folhar: padeiro conta em bolas inteiras).
    1 linha por sub-receita; tabela nova criada por db.create_all.
    """
    __tablename__ = 'consumo_sub_fracao'

    id = db.Column(db.Integer, primary_key=True)
    receita_id = db.Column(db.Integer, db.ForeignKey('receita.id'),
                           nullable=False, unique=True, index=True)
    fracao_pendente = db.Column(db.Float, nullable=False, default=0.0)
    atualizado_em = db.Column(db.DateTime, default=agora, onupdate=agora)

    receita = db.relationship('Receita')


# ── Pedidos de Loja ──

class EstoqueLoja(db.Model):
    __tablename__ = 'estoque_loja'

    id = db.Column(db.Integer, primary_key=True)
    loja_id = db.Column(db.Integer, db.ForeignKey('loja.id'), nullable=False)
    receita_id = db.Column(db.Integer, db.ForeignKey('receita.id'), nullable=True)
    produto_id = db.Column(db.Integer, db.ForeignKey('produto.id'), nullable=True)
    materia_prima_id = db.Column(db.Integer, db.ForeignKey('materia_prima.id'), nullable=True)
    quantidade = db.Column(db.Integer, default=0)
    # Reservado pra pedido online em `aguardando_pagamento` — segura o
    # estoque entre o checkout e a confirmacao do pagamento (Pix 30min).
    # O catalogo expoe `quantidade - quantidade_reservada` como
    # disponivel. NOT NULL DEFAULT 0 (decisao 21/06/2026 — cutover loja
    # propria, ver `app/services/loja_estoque_reserva.py`).
    quantidade_reservada = db.Column(db.Integer, nullable=False,
                                     default=0, server_default='0')
    # Nome digitado em entrada-em-lote quando nao houve match com nenhum
    # cadastro. Mesma logica do EstoqueProducao.nome_pendente.
    nome_pendente = db.Column(db.String(200), nullable=True)
    # Estado do item (assado/backup/NULL=padrao). Loja pode ter 3 estados
    # ao mesmo tempo pra mesma receita: assado (vitrine) + backup (freezer
    # pra emergencia) + NULL (cru, raro).
    estado = db.Column(db.String(20), nullable=True)
    # Estoque MINIMO desta loja pra este item: piso da sugestao de pedido
    # loja->industria (motor venda+estoque em previsao_producao). O alvo do
    # dia nunca cai abaixo dele — a loja mantem um colchao do item. Vazio =
    # sem piso. ALTER em migrations_legacy (commit 1, 16/07/2026).
    estoque_minimo = db.Column(db.Integer, nullable=True)
    # Pedido minimo DIARIO desta loja pra este item (dono 17/08/2026,
    # danishes assadas: "as lojas devem receber 2 danishes desses por dia
    # IMPRETERIVELMENTE"): piso INCONDICIONAL do pedido de cada dia — NAO
    # desconta o estoque que sobrou (diferente do colchao acima). A media
    # de venda manda quando passa do piso. Vazio = sem piso. ALTER em
    # migrations_legacy (commit 1, 17/08/2026).
    pedido_minimo_diario = db.Column(db.Integer, nullable=True)
    # Reposição FRESCA por venda do dia: modo excepcional por loja+item.
    # Ignora saldo acumulado, merma e caixa/minimo globais; cada entrega usa
    # somente a média de venda daquele dia da semana. Ex.: Croissant
    # Tradicional fresco da Nebraska (dono 03/09/2026).
    reposicao_por_venda_diaria = db.Column(
        db.Boolean, nullable=False, default=False, server_default='false')

    loja = db.relationship('Loja')
    receita = db.relationship('Receita')
    produto = db.relationship('Produto')
    materia_prima = db.relationship('MateriaPrima')
    movimentacoes = db.relationship('MovEstoqueLoja', backref='estoque', cascade='all, delete-orphan')

    __table_args__ = (
        # Estoque eh 1 linha por produto (estado vive so no pedido). Indices de
        # consulta por (loja, item); a trava de unicidade parcial eh criada na
        # migracao (app/migrations_legacy.py), apos consolidar duplicatas legadas.
        db.Index('ix_estoque_loja_loja_receita', 'loja_id', 'receita_id'),
        db.Index('ix_estoque_loja_loja_produto', 'loja_id', 'produto_id'),
        db.Index('ix_estoque_loja_loja_mp', 'loja_id', 'materia_prima_id'),
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

    @property
    def disponivel(self):
        """Quantidade que pode ser vendida AGORA = quantidade fisica
        menos o que esta reservado pra pedidos aguardando pagamento."""
        return max(0, (self.quantidade or 0) - (self.quantidade_reservada or 0))

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
    # Vinculo com o Desperdicio que causou este movimento (tipos
    # 'desperdicio'/'desperdicio_sem_estoque'). Permite excluir um registro
    # de desperdicio estornando EXATAMENTE o que ele baixou — sem o vinculo,
    # estornar `quantidade` as cegas cria estoque fantasma (reaproveitavel
    # nunca baixou, parcial baixou menos, cesta baixou nos componentes).
    # NULL = movimento de outra origem OU anterior a coluna (ALTER em
    # migrations_legacy, deployado 02/07/2026 antes deste modelo).
    desperdicio_id = db.Column(
        db.Integer, db.ForeignKey('desperdicio.id', ondelete='SET NULL'),
        nullable=True, index=True)


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


class PerdaProducao(db.Model):
    """Perda de PRODUÇÃO lançada pelo padeiro (queimou, caiu, erro de ponto —
    13/08/2026, pedido do dono). Tabela nova via db.create_all.

    Registro estruturado; quem mexe em estoque é o service
    `perda_producao.registrar`:
    - perda de item PRONTO debita EstoqueProducao (mov 'perda_producao',
      ligado pela referência 'Perda #<id> — ...');
    - `fornada=True` = fornada que queimou ANTES de lançar a produção —
      consome MP + sub-receitas da ficha (motor do produzir) SEM creditar
      estoque (o produto nunca existiu).
    """
    __tablename__ = 'perda_producao'

    id = db.Column(db.Integer, primary_key=True)
    receita_id = db.Column(db.Integer, db.ForeignKey('receita.id'),
                           nullable=False, index=True)
    quantidade = db.Column(db.Integer, nullable=False)
    motivo = db.Column(db.String(20), nullable=False)
    observacao = db.Column(db.Text)
    fornada = db.Column(db.Boolean, nullable=False, default=False)
    # RESPONSÁVEL pela perda = funcionário do quadro do RH (dono 13/08/2026:
    # "escolher o responsável — padeiro, ajudante de padeiro etc"). A TV do
    # padeiro roda em conta compartilhada — criado_por_id diz quem LANÇOU,
    # não quem era o responsável. ALTER em migrations_legacy (2 commits,
    # sonda ?colunas= confirmada). Nullable: perdas pré-feature.
    funcionario_id = db.Column(db.Integer, db.ForeignKey('funcionario.id'),
                               nullable=True)
    criado_em = db.Column(db.DateTime, default=agora, index=True)
    criado_por_id = db.Column(db.Integer, db.ForeignKey('usuario.id'))

    receita = db.relationship('Receita')
    funcionario = db.relationship('Funcionario')
    criado_por = db.relationship('Usuario')


# ── Slack bot (copilot via DM/@mention) ───────────────────────────────
