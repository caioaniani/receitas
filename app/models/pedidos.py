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
    status = db.Column(db.String(20), default='confirmado')
    observacao = db.Column(db.Text)
    criado_por = db.Column(db.Integer, db.ForeignKey('usuario.id'))
    criado_em = db.Column(db.DateTime, default=agora)
    # Motorista que pegou esse pedido na industria (handshake de saida).
    # Painel /driver/<token> filtra por isso pra cada motorista so ver os
    # pedidos que ele coletou.
    driver_id = db.Column(db.Integer, db.ForeignKey('driver_entrega.id'),
                           nullable=True, index=True)
    modificado_em = db.Column(db.DateTime, nullable=True)
    modificado_por_id = db.Column(db.Integer, db.ForeignKey('usuario.id'),
                                   nullable=True, index=True)

    loja = db.relationship('Loja', backref='pedidos')
    criador = db.relationship('Usuario', foreign_keys=[criado_por])
    driver = db.relationship('Driver', foreign_keys=[driver_id])
    modificado_por = db.relationship('Usuario', foreign_keys=[modificado_por_id])
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
    # Estado do item (assado/backup) ou NULL = estado padrao da familia
    # da receita (cru pra viennoiserie, congelado assado pra pao_sourdough,
    # assado fresco pra fornada_especial). Ver app/constants.py.
    estado = db.Column(db.String(20), nullable=True)

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

    @property
    def estado_efetivo(self):
        """`estado` explicito do item, ou fallback pra `receita.estado_padrao`.

        Permite a receita definir um padrao (ex: brioche='assado') sem
        precisar marcar item por item; o item ainda pode sobrescrever."""
        if self.estado:
            return self.estado
        if self.receita_id and self.receita and self.receita.estado_padrao:
            return self.receita.estado_padrao
        return None

    @property
    def nome_item_com_estado(self):
        """Nome do item + tag de estado, se houver (explicito ou via receita).
        Ex: 'Croissant Francês [BACKUP]' ou 'Sourdough' (sem tag)."""
        from app.constants import render_item_com_estado
        return render_item_com_estado(self.nome_item, self.estado_efetivo)


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


# ── Retirada de sobras (loja → industria) ──

class RetiradaSobra(db.Model):
    """Pedido de RETIRADA de sobras reaproveitaveis da loja pra industria
    (ex: croissants tradicionais que viram Croissant Almond).

    Nasce no lancamento de sobras pelo bot (o copilot pergunta "quantos voltam
    pra virar almond?", exige FOTO da sobra e cria a retirada pro dia
    seguinte). Modelo SEPARADO de PedidoLoja de proposito: retirada nao e
    demanda — jamais entra em previsao/comprometido/medias por construcao.

    Maquina de status (esteira espelhada da entrega, movida por QR):
      aguardando_coleta → [QR coleta, PIN driver → BAIXA EstoqueLoja]
      em_transporte     → [QR recebimento, PIN driver/producao → CREDITA
                           EstoqueProducao na receita de retorno] — ou
                          destrava admin em /pedidos/retiradas
                          (19/07/2026): recebimento manual (credita) ou
                          cancelar (ESTORNA a baixa da coleta)
      recebida          | cancelada (antes da coleta: sem mexer em estoque)
    Transicoes por CLAIM atomico (UPDATE condicional) — acao concorrente
    perde o claim e nao movimenta estoque 2x.

    Movimentos de estoque levam o token `ret-<id>` — mesma familia de tipos do
    fluxo manual (`devolucao_industria`/`retorno_loja`), entao Movimento do
    Dia/relatorios enxergam igual."""
    __tablename__ = 'retirada_sobra'

    id = db.Column(db.Integer, primary_key=True)
    loja_id = db.Column(db.Integer, db.ForeignKey('loja.id'), nullable=False,
                        index=True)
    status = db.Column(db.String(20), nullable=False,
                       default='aguardando_coleta', index=True)
    data_retirada = db.Column(db.Date, nullable=False, index=True)
    criado_em = db.Column(db.DateTime, default=agora, nullable=False)
    criado_por_id = db.Column(db.Integer, db.ForeignKey('usuario.id'))
    # Foto da sobra (obrigatoria na criacao) — comprovante da contagem.
    foto_url = db.Column(db.String(500), nullable=False)
    foto_storage_path = db.Column(db.String(500))
    observacao = db.Column(db.Text)
    driver_id = db.Column(db.Integer, db.ForeignKey('driver_entrega.id'),
                          nullable=True, index=True)
    coletada_em = db.Column(db.DateTime, nullable=True)
    recebida_em = db.Column(db.DateTime, nullable=True)
    cancelada_em = db.Column(db.DateTime, nullable=True)

    loja = db.relationship('Loja')
    criado_por = db.relationship('Usuario')
    driver = db.relationship('Driver')
    itens = db.relationship('RetiradaSobraItem', backref='retirada',
                            cascade='all, delete-orphan')
    qrcodes = db.relationship('RetiradaQRCode', back_populates='retirada',
                              cascade='all, delete-orphan')

    @property
    def token_mov(self):
        """Token que amarra os movimentos de estoque desta retirada."""
        return f'ret-{self.id}'


class RetiradaSobraItem(db.Model):
    __tablename__ = 'retirada_sobra_item'

    id = db.Column(db.Integer, primary_key=True)
    retirada_id = db.Column(db.Integer, db.ForeignKey('retirada_sobra.id'),
                            nullable=False, index=True)
    receita_id = db.Column(db.Integer, db.ForeignKey('receita.id'),
                           nullable=True)
    produto_id = db.Column(db.Integer, db.ForeignKey('produto.id'),
                           nullable=True)
    quantidade = db.Column(db.Integer, nullable=False)
    # Conferencia do MOTORISTA na coleta (loja declarou 15, sairam 12): a
    # baixa da loja usa este valor e o recebimento parte dele. NULL =
    # coletou o declarado. ALTER em migrations_legacy (deployado ANTES do
    # modelo — procedimento de 2 commits, 03/07/2026).
    quantidade_coletada = db.Column(db.Integer, nullable=True)
    # Conferencia na industria (divergencia coletado x chegou). NULL = sem
    # divergencia registrada (recebeu o coletado/declarado).
    quantidade_recebida = db.Column(db.Integer, nullable=True)

    receita = db.relationship('Receita')
    produto = db.relationship('Produto')

    @property
    def nome_item(self):
        if self.receita:
            return self.receita.nome
        if self.produto:
            return self.produto.nome
        return '?'


class RetiradaQRCode(db.Model):
    """QR de handshake da retirada — mesmo desenho do PedidoQRCode.

    tipo='coleta': motorista escaneia NA LOJA + PIN do Driver →
      em_transporte + baixa EstoqueLoja.
    tipo='recebimento': escaneado NA INDUSTRIA + PIN de driver/producao →
      recebida + credita EstoqueProducao (receita de retorno)."""
    __tablename__ = 'retirada_qrcode'

    id = db.Column(db.Integer, primary_key=True)
    token = db.Column(db.String(40), unique=True, nullable=False, index=True)
    retirada_id = db.Column(db.Integer, db.ForeignKey('retirada_sobra.id'),
                            nullable=False, index=True)
    tipo = db.Column(db.String(12), nullable=False)  # 'coleta' | 'recebimento'
    criado_em = db.Column(db.DateTime, default=agora, nullable=False)
    criado_por_id = db.Column(db.Integer, db.ForeignKey('usuario.id'))
    expira_em = db.Column(db.DateTime, nullable=False)
    usado_em = db.Column(db.DateTime, nullable=True)
    usado_por_descricao = db.Column(db.String(100))

    retirada = db.relationship('RetiradaSobra', back_populates='qrcodes')
    criado_por = db.relationship('Usuario')

    @property
    def valido(self):
        return self.usado_em is None and self.expira_em > agora()
