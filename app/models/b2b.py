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
    # Endereco estruturado pra NF-e (06/07/2026): a SEFAZ exige logradouro/
    # numero/bairro/cidade/uf SEPARADOS — mesma licao do PedidoOnline. O
    # campo livre `endereco` acima segue como fallback humano (boleto/UI).
    endereco_logradouro = db.Column(db.String(200))
    endereco_numero = db.Column(db.String(20))
    endereco_complemento = db.Column(db.String(100))
    endereco_bairro = db.Column(db.String(100))
    endereco_cep = db.Column(db.String(9))
    endereco_cidade = db.Column(db.String(100))
    endereco_uf = db.Column(db.String(2))
    contato = db.Column(db.String(100))  # nome da pessoa que compra
    desconto_percentual = db.Column(db.Float, default=0)  # % sobre preco atacado
    # Fechamento MENSAL (07/07/2026): cliente compra o mes inteiro (vendas
    # sem parcela) e a conta fecha numa FaturaB2B — uma NF consolidada +
    # um boleto do total. False = cada venda cobra na hora (padrao).
    faturamento_mensal = db.Column(db.Boolean, default=False, nullable=False)
    observacao = db.Column(db.Text)
    ativo = db.Column(db.Boolean, default=True, nullable=False)
    criado_em = db.Column(db.DateTime, default=agora)

class PrecoClienteB2B(db.Model):
    """Tabela de preco POR CLIENTE do atacado (06/07/2026).

    O atacado cobra valores diferentes por cliente — um percentual unico
    (ClienteB2B.desconto_percentual) nao cobre isso. Preco especifico aqui
    VENCE o preco de atacado do cadastro (e o desconto percentual NAO se
    aplica em cima — o valor ja e final). Sem linha = cai no atacado padrao
    com o desconto do cliente (comportamento antigo, inalterado).

    Chave (cliente, kind, item_id) — mesmo padrao kind/item do
    TinyProdutoMap. Dinheiro: Numeric(10,2), sempre Decimal.
    """
    __tablename__ = 'preco_cliente_b2b'

    id = db.Column(db.Integer, primary_key=True)
    cliente_id = db.Column(db.Integer, db.ForeignKey('cliente_b2b.id'),
                           nullable=False, index=True)
    kind = db.Column(db.String(10), nullable=False)  # 'receita' | 'produto'
    item_id = db.Column(db.Integer, nullable=False)
    preco = db.Column(db.Numeric(10, 2), nullable=False)
    atualizado_em = db.Column(db.DateTime, default=agora, onupdate=agora)
    atualizado_por_id = db.Column(db.Integer, db.ForeignKey('usuario.id'),
                                  nullable=True)

    cliente = db.relationship('ClienteB2B',
                              backref=db.backref('precos_especificos',
                                                 cascade='all, delete-orphan'))

    __table_args__ = (
        db.UniqueConstraint('cliente_id', 'kind', 'item_id',
                            name='uq_preco_cliente_b2b_item'),
    )


class FaturaB2B(db.Model):
    """Fechamento MENSAL da conta de um cliente B2B (07/07/2026).

    Cliente com `ClienteB2B.faturamento_mensal` compra o mes inteiro: cada
    entrega e uma VendaB2B normal (baixa estoque na hora), SEM parcela. Na
    virada do mes a conta e FECHADA: as vendas do periodo entram nesta
    fatura, cada venda ganha UMA parcela com o vencimento da fatura (o
    contas a receber continua por parcela, nada muda nos relatorios) e a
    fatura emite UMA NF consolidada no Tiny + UM boleto Sicredi do total.

    Status: fechada -> paga (liquidacao do boleto quita as parcelas juntas)
    | cancelada (desfaz vinculos e apaga as parcelas criadas — so enquanto
    nada foi pago nem NF emitida).
    """
    __tablename__ = 'fatura_b2b'

    id = db.Column(db.Integer, primary_key=True)
    cliente_id = db.Column(db.Integer, db.ForeignKey('cliente_b2b.id'),
                           nullable=False, index=True)
    data_inicio = db.Column(db.Date, nullable=False)
    data_fim = db.Column(db.Date, nullable=False)
    vencimento = db.Column(db.Date, nullable=False, index=True)
    status = db.Column(db.String(15), nullable=False, default='fechada',
                       index=True)  # fechada | paga | cancelada
    valor_total = db.Column(db.Numeric(10, 2), nullable=False, default=0)
    pago_em = db.Column(db.DateTime, nullable=True)
    # NF-e via Tiny — mesmo trio da VendaB2B/PedidoOnline + numero humano.
    nf_numero = db.Column(db.String(50))
    tiny_nota_fiscal_id = db.Column(db.String(40))
    nf_status = db.Column(db.String(40))
    nf_emitida_em = db.Column(db.DateTime, nullable=True)
    criado_em = db.Column(db.DateTime, default=agora)
    criado_por_id = db.Column(db.Integer, db.ForeignKey('usuario.id'))
    cancelada_em = db.Column(db.DateTime, nullable=True)
    cancelada_por_id = db.Column(db.Integer, db.ForeignKey('usuario.id'),
                                 nullable=True)

    cliente = db.relationship('ClienteB2B')
    vendas = db.relationship('VendaB2B', backref='fatura',
                             foreign_keys='VendaB2B.fatura_id')
    criado_por = db.relationship('Usuario', foreign_keys=[criado_por_id])

    @property
    def codigo(self):
        """Referencia curta legivel (aparece no boleto como seu_numero)."""
        return f'FAT{self.id:05d}'

    @property
    def periodo_display(self):
        return (f'{self.data_inicio.strftime("%d/%m")} a '
                f'{self.data_fim.strftime("%d/%m/%Y")}')


class VendaB2B(db.Model):
    """Venda B2B: cabecalho. Itens vinculados via VendaB2BItem,
    pagamento parcelado via VendaB2BParcela.

    Estoque eh baixado do EstoqueProducao (industria) ao salvar.
    Cancelamento estorna automaticamente.
    """
    __tablename__ = 'venda_b2b'

    id = db.Column(db.Integer, primary_key=True)
    data_venda = db.Column(db.Date, nullable=False, default=lambda: agora().date(), index=True)
    # Data em que a industria precisa entregar/produzir (para a tela do padeiro).
    # NULL = venda imediata, nao entra na fila de producao do padeiro.
    data_entrega = db.Column(db.Date, nullable=True, index=True)
    cliente_id = db.Column(db.Integer, db.ForeignKey('cliente_b2b.id'), nullable=True, index=True)
    cliente_nome = db.Column(db.String(150))  # pra venda avulsa sem cadastro
    status = db.Column(db.String(20), default='ativa', nullable=False)  # ativa, cancelada
    # Status de ENTREGA/producao, separado do status FINANCEIRO acima. Espelha
    # o fluxo do pedido de loja na tela do padeiro: pendente -> separado ->
    # em_transporte -> entregue. Nao confundir com 'ativa'/'cancelada'.
    status_entrega = db.Column(db.String(20), nullable=False, default='pendente')
    # Numeric(10, 2): precisao exata em centavos. Float dava erro de
    # arredondamento em soma de parcelas (R$33,33 × 3 != R$100,00).
    valor_total = db.Column(db.Numeric(10, 2), nullable=False, default=0)
    # Frete da entrega COBRADO do cliente (20/07/2026, pedido do dono via
    # Bruno). SOMADO no valor_total (parcela/boleto/fatura herdam) e enviado
    # no campo valor_frete da NF do Tiny — mesmo padrao da NF do site.
    # 0 = sem frete (todas as vendas antigas).
    frete_valor = db.Column(db.Numeric(10, 2), nullable=False, default=0)
    observacao = db.Column(db.Text)
    nf_numero = db.Column(db.String(50))  # numero da NF se houver
    # Divulgação/cortesia autorizada: preserva valores, parcelas e estoque.
    # Snapshot de motivo, responsável e data; NULL mantém a cobrança normal.
    dispensa_cobranca = db.Column(db.JSON(none_as_null=True), nullable=True)
    # NF-e via Tiny (06/07/2026) — mesmo trio do PedidoOnline: id da NF no
    # Tiny, status da autorizacao SEFAZ e timestamp de emissao confirmada.
    tiny_nota_fiscal_id = db.Column(db.String(40))
    nf_status = db.Column(db.String(40))
    nf_emitida_em = db.Column(db.DateTime, nullable=True)
    # Fechamento mensal (07/07/2026): venda que entrou numa fatura. NULL =
    # venda avulsa/cobrada na hora (padrao).
    fatura_id = db.Column(db.Integer, db.ForeignKey('fatura_b2b.id'),
                          nullable=True, index=True)
    # Regime da baixa (07/07/2026, decisao do dono): o estoque da industria
    # so baixa quando o padeiro SEPARA o pedido no /padeiro. NULL = ainda
    # nao baixou (aguardando separacao); preenchido = ja baixou (na
    # separacao — ou na criacao, para venda IMEDIATA sem data_entrega, que
    # nunca entra na fila do padeiro). O estorno total limpa o marcador.
    # A quantidade continua vindo do ledger MovEstoqueProducao (por saldo);
    # esta coluna e o ESTADO do regime, nao a fonte das quantidades.
    estoque_baixado_em = db.Column(db.DateTime, nullable=True)
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
        if self.sem_cobranca:
            return Decimal('0')
        return Decimal(self.valor_total or 0) - self.valor_pago

    @property
    def sem_cobranca(self):
        return bool(self.dispensa_cobranca)

class VendaB2BItem(db.Model):
    __tablename__ = 'venda_b2b_item'

    id = db.Column(db.Integer, primary_key=True)
    venda_id = db.Column(db.Integer, db.ForeignKey('venda_b2b.id'), nullable=False, index=True)
    receita_id = db.Column(db.Integer, db.ForeignKey('receita.id'), nullable=True)
    produto_id = db.Column(db.Integer, db.ForeignKey('produto.id'), nullable=True)
    quantidade = db.Column(db.Integer, nullable=False)
    preco_unitario = db.Column(db.Numeric(10, 2), nullable=False)
    desconto_percentual = db.Column(db.Float, default=0)  # %, nao R$
    # Estado do item (cru/backup/assado) p/ producao — mesma regra do PedidoItem.
    estado = db.Column(db.String(20), nullable=True)
    # Observacao por item (ex: "fatiado", "sem acucar") — aparece pro padeiro.
    observacao = db.Column(db.String(200))

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
    def estado_efetivo(self):
        """Estado explicito do item ou fallback pra `receita.estado_padrao`."""
        if self.estado:
            return self.estado
        if self.receita_id and self.receita and self.receita.estado_padrao:
            return self.receita.estado_padrao
        return None

    @property
    def nome_item_com_estado(self):
        from app.constants import render_item_com_estado
        return render_item_com_estado(self.nome_item, self.estado_efetivo)

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
    # Parcela CRIADA por um fechamento mensal (FaturaB2B): a liquidacao do
    # boleto da fatura quita todas as parcelas com este vinculo de uma vez,
    # e o cancelamento da fatura as apaga (so as dela). NULL = parcela
    # normal da venda.
    fatura_id = db.Column(db.Integer, db.ForeignKey('fatura_b2b.id'),
                          nullable=True, index=True)
    vencimento = db.Column(db.Date, nullable=False, index=True)
    valor = db.Column(db.Numeric(10, 2), nullable=False)
    valor_pago = db.Column(db.Numeric(10, 2), default=0)
    pago_em = db.Column(db.DateTime, nullable=True)
    forma_pagamento = db.Column(db.String(30))  # pix, dinheiro, boleto, transferencia
    observacao = db.Column(db.String(200))

    fatura = db.relationship('FaturaB2B', backref='parcelas',
                             foreign_keys=[fatura_id])

    @property
    def saldo(self):
        if self.venda and self.venda.sem_cobranca:
            return 0
        return (self.valor or 0) - (self.valor_pago or 0)

    @property
    def status(self):
        if self.venda and self.venda.sem_cobranca:
            return 'sem_cobranca'
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


# ── Orcamento B2B (encomendas corporativas, eventos, cestas em volume) ──
#
# Pre-pedido: atendente monta lista pra mandar pro cliente em PDF.
# Cliente aceita -> pode virar VendaB2B (Fase 2). Por enquanto o que
# importa e o ORCAMENTO em si (PDF + historico).
#
# Diferenca chave pra VendaB2B: orcamento NAO baixa estoque e NAO gera
# parcela. So vira "real" quando aprovado e convertido em venda.

class Orcamento(db.Model):
    __tablename__ = 'orcamento'

    id = db.Column(db.Integer, primary_key=True)
    # Codigo curto, legivel pelo telefone (ex: 'ORC-2026-0042'). Gerado
    # no service ao criar (ano + sequencial reseta anualmente).
    codigo = db.Column(db.String(20), unique=True, index=True, nullable=False)
    data = db.Column(db.Date, nullable=False, default=hoje, index=True)
    # Validade da proposta (default 7 dias). Vencido = aviso na lista,
    # nao bloqueia aprovar manual.
    valido_ate = db.Column(db.Date, nullable=False,
                           default=lambda: hoje() + __import__('datetime').timedelta(days=7))
    # Data prevista de entrega do que esta sendo orcado. NULL = "a combinar".
    # Vai no PDF e na tela de detalhe. Diferente de VendaB2B.data_entrega:
    # orcamento NAO entra na fila do padeiro (so quando virar venda).
    data_entrega = db.Column(db.Date, nullable=True)

    # Cliente: pode ser ClienteB2B cadastrado OU avulso (so nome+contato).
    cliente_id = db.Column(db.Integer, db.ForeignKey('cliente_b2b.id'),
                           nullable=True, index=True)
    cliente_nome = db.Column(db.String(150))   # avulso
    cliente_documento = db.Column(db.String(30))  # CNPJ/CPF do avulso
    cliente_email = db.Column(db.String(120))
    cliente_telefone = db.Column(db.String(30))
    cliente_endereco = db.Column(db.String(250))

    # Status do orcamento (rascunho -> enviado -> aprovado/recusado/expirado).
    # Strings (nao Enum) pelo mesmo motivo dos outros dominios.
    status = db.Column(db.String(20), nullable=False, default='rascunho', index=True)

    # Dinheiro: Numeric(10,2) + Decimal sempre (CLAUDE.md, peso especial).
    # `subtotal` = soma dos itens; `desconto_valor` = desconto absoluto;
    # `valor_total` = subtotal - desconto.
    subtotal = db.Column(db.Numeric(10, 2), nullable=False, default=0)
    desconto_valor = db.Column(db.Numeric(10, 2), nullable=False, default=0)
    # Frete: valor SOMADO ao total (subtotal - desconto + frete). Default 0
    # = retirada / sem frete. Dinheiro -> Numeric(10,2) + Decimal sempre.
    frete_valor = db.Column(db.Numeric(10, 2), nullable=False, default=0)
    valor_total = db.Column(db.Numeric(10, 2), nullable=False, default=0)

    observacao = db.Column(db.Text)  # condicoes, prazo de entrega, etc

    criado_por_id = db.Column(db.Integer, db.ForeignKey('usuario.id'))
    criado_em = db.Column(db.DateTime, default=agora)
    atualizado_em = db.Column(db.DateTime, default=agora, onupdate=agora)
    enviado_em = db.Column(db.DateTime, nullable=True)
    aprovado_em = db.Column(db.DateTime, nullable=True)
    recusado_em = db.Column(db.DateTime, nullable=True)
    # Aprovar VIRA venda (07/07/2026): a venda criada na aprovacao fica
    # vinculada aqui — evita converter o mesmo orcamento 2x (duplicaria a
    # fila do padeiro e a baixa na separacao). NULL = ainda nao converteu
    # (rascunho/enviado/recusado, ou aprovado do regime antigo).
    venda_id = db.Column(db.Integer, db.ForeignKey('venda_b2b.id'),
                         nullable=True)
    # Rascunho arquivavel (08/07/2026, pedido do dono): rascunho que nao
    # foi pra frente sai de Pendentes SEM virar 'recusado' (recusado =
    # cliente disse nao). Mesmo idioma do Receita.arquivada_em; NULL =
    # ativo. Arquivado nao transiciona de status ate desarquivar.
    arquivado_em = db.Column(db.DateTime, nullable=True)

    cliente = db.relationship('ClienteB2B')
    itens = db.relationship('OrcamentoItem', backref='orcamento',
                            cascade='all, delete-orphan', order_by='OrcamentoItem.id')
    criado_por = db.relationship('Usuario', foreign_keys=[criado_por_id])
    venda = db.relationship('VendaB2B', foreign_keys=[venda_id])

    @property
    def cliente_display(self):
        if self.cliente:
            return self.cliente.nome
        return self.cliente_nome or '(avulso)'

    @property
    def venceu(self):
        return self.status not in ('aprovado', 'recusado') and hoje() > self.valido_ate

    def recalcular_total(self):
        """Soma itens -> subtotal; (subtotal - desconto) + frete -> total.
        Desconto nao deixa os produtos negativos (max 0); frete soma em
        cima. Tudo em Decimal pra precisao exata (centavos)."""
        from decimal import Decimal
        sub = sum((Decimal(str(i.subtotal or 0)) for i in self.itens),
                  Decimal('0'))
        self.subtotal = sub
        desc = Decimal(str(self.desconto_valor or 0))
        frete = Decimal(str(self.frete_valor or 0))
        self.valor_total = max(Decimal('0'), sub - desc) + frete
        return self.valor_total


class OrcamentoItem(db.Model):
    __tablename__ = 'orcamento_item'

    id = db.Column(db.Integer, primary_key=True)
    orcamento_id = db.Column(db.Integer, db.ForeignKey('orcamento.id'),
                             nullable=False, index=True)

    # Vinculo opcional ao catalogo: linha livre (sem FK) e suportada pra
    # itens fora do catalogo ("Servico de buffet", "Decoracao", etc).
    receita_id = db.Column(db.Integer, db.ForeignKey('receita.id'), nullable=True)
    produto_id = db.Column(db.Integer, db.ForeignKey('produto.id'), nullable=True)

    # Snapshot do nome (pra item livre OU pra preservar o que foi enviado
    # mesmo se o catalogo mudar). Editavel pelo atendente.
    nome = db.Column(db.String(200), nullable=False)
    quantidade = db.Column(db.Numeric(10, 3), nullable=False, default=1)
    unidade = db.Column(db.String(20))  # un, kg, cx, dz, ...
    preco_unitario = db.Column(db.Numeric(10, 2), nullable=False, default=0)
    subtotal = db.Column(db.Numeric(10, 2), nullable=False, default=0)
    observacao = db.Column(db.String(200))

    receita = db.relationship('Receita')
    produto = db.relationship('Produto')

    def recalcular_subtotal(self):
        from decimal import Decimal
        self.subtotal = (Decimal(str(self.quantidade or 0))
                         * Decimal(str(self.preco_unitario or 0)))
        return self.subtotal


class LeadB2B(db.Model):
    """Lead de ATACADO/B2B capturado pelo bot de atendimento (16/07/2026,
    pedido do dono: "treinar o bot para atender os clientes que vem querer o
    cardapio B2B — capturar e-mail e telefone whatsapp para eu entrar em
    contato"). Tabela NOVA — criada por db.create_all no startup, sem ALTER
    legado (mesmo padrao do UsoIA). Vive em tabela propria de proposito: o
    historico da conversa (ChatbotConversa) e apagado pela retencao em 180d;
    o lead e dado comercial e fica.
    """
    __tablename__ = 'lead_b2b'

    id = db.Column(db.Integer, primary_key=True)
    nome = db.Column(db.String(150), nullable=False)
    empresa = db.Column(db.String(200))
    email = db.Column(db.String(200), nullable=False, index=True)
    # So digitos, com DDD (com ou sem o 55) — normalizado na captura.
    telefone = db.Column(db.String(20), nullable=False, index=True)
    # O que o cliente disse que quer (resumo do bot) — contexto pro contato.
    interesse = db.Column(db.Text)
    origem = db.Column(db.String(30), nullable=False, default='chatbot')
    # Conversa do Chatwoot onde o lead nasceu (link direto na tela admin).
    conversa_id = db.Column(db.Integer)
    catalogo_enviado = db.Column(db.Boolean, nullable=False, default=False)
    # NULL = ainda nao contatado (badge "novo" na tela; o dono marca).
    contatado_em = db.Column(db.DateTime, nullable=True)
    contatado_por_id = db.Column(db.Integer, db.ForeignKey('usuario.id'),
                                 nullable=True)
    criado_em = db.Column(db.DateTime, default=agora, index=True)

    def __repr__(self):
        return f'<LeadB2B {self.email}>'
