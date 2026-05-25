"""Modelos do dominio: financeiro (contas a pagar a partir de NF/boleto).

Faz parte de `app.models` (split por dominio). Importar via
`from app.models import X` funciona porque `app/models/__init__.py`
re-exporta tudo.

Contas a pagar nascem de fotos de NF/boleto postadas no Slack (canais de
recebimento de mercadoria). Cada imagem/arquivo vira uma linha; a IA extrai
os dados e o usuario edita/confere na tela. O documento original fica no
Dropbox (imagem_url) pra conferencia.
"""

from app.extensions import db
from app.utils import agora, hoje


class ContaPagar(db.Model):
    """Documento de pagamento (NF ou boleto) recebido via Slack.

    Dinheiro: valores em Numeric(10,2) (precisao exata, regra B4). Campos
    editaveis — a IA so faz a primeira extracao, o humano corrige.
    """
    __tablename__ = 'conta_pagar'

    id = db.Column(db.Integer, primary_key=True)
    # 'nota_fiscal' | 'boleto' | 'desconhecido' (classificado pela IA)
    tipo_documento = db.Column(db.String(20), default='desconhecido')

    fornecedor_nome = db.Column(db.String(200))  # texto extraido
    fornecedor_id = db.Column(db.Integer, db.ForeignKey('fornecedor.id'),
                               nullable=True)

    valor_total = db.Column(db.Numeric(10, 2))
    vencimento = db.Column(db.Date, nullable=True, index=True)
    nf_numero = db.Column(db.String(60))
    codigo_barras = db.Column(db.String(100))    # boleto
    linha_digitavel = db.Column(db.String(100))  # boleto
    info_pagamento = db.Column(db.Text)          # pix / instrucoes livres
    itens_json = db.Column(db.Text)              # lista extraida (NF)

    # 'aberto' | 'pago' | 'ignorado'
    status = db.Column(db.String(20), default='aberto', nullable=False, index=True)
    valor_pago = db.Column(db.Numeric(10, 2), default=0)
    pago_em = db.Column(db.DateTime, nullable=True)
    forma_pagamento = db.Column(db.String(30))

    # Documento original no Dropbox
    imagem_url = db.Column(db.String(500))
    imagem_storage_path = db.Column(db.String(500))

    # Origem (Slack)
    origem_canal = db.Column(db.String(120))
    slack_file_id = db.Column(db.String(80), unique=True, index=True)  # idempotencia
    slack_ts = db.Column(db.String(40))
    enviado_por = db.Column(db.String(120))

    dados_ia_json = db.Column(db.Text)  # raw da extracao + modelo usado
    # Liga boleto <-> NF do mesmo recebimento (agrupamento manual na tela)
    relacionado_id = db.Column(db.Integer, db.ForeignKey('conta_pagar.id'),
                                nullable=True)

    criado_em = db.Column(db.DateTime, default=agora, index=True)
    editado_em = db.Column(db.DateTime, nullable=True)
    editado_por_id = db.Column(db.Integer, db.ForeignKey('usuario.id'),
                                nullable=True)

    fornecedor = db.relationship('Fornecedor')
    # Self-FK: 'relacionado' = doc pra onde aponta; 'relacionado_por' = docs
    # que apontam pra este. Juntos dao o par NF<->boleto nos dois sentidos.
    relacionado = db.relationship('ContaPagar', remote_side=[id],
                                  foreign_keys=[relacionado_id],
                                  backref='relacionado_por')

    @property
    def atrasado(self):
        return (self.status == 'aberto' and self.vencimento is not None
                and self.vencimento < hoje())

    @property
    def ligados(self):
        """Documentos ligados a este (bidirecional). Mostra o par mesmo que
        so um lado tenha setado o vinculo."""
        out = []
        if self.relacionado is not None:
            out.append(self.relacionado)
        for o in (self.relacionado_por or []):
            if o is not None and o.id != self.id and o not in out:
                out.append(o)
        return out


class ContaPagarItemMap(db.Model):
    """Mapeia 'nome de item de NF' (normalizado) -> MateriaPrima + regra de
    conversao de unidade. Confirmado uma vez, reusa em toda NF futura.

    Espelha SeruProdutoMap: estados mapeado/ignorado/pendente. A IA sugere
    unidade/fator (campos ia_*) mas NUNCA aplica sozinha — o humano confirma
    (confirmado_em) antes de qualquer entrada de estoque.
    """
    __tablename__ = 'conta_pagar_item_map'

    id = db.Column(db.Integer, primary_key=True)
    # Nome normalizado (ascii/lower) do item da NF — chave de reuso.
    item_nome_norm = db.Column(db.String(300), nullable=False, unique=True, index=True)
    item_nome_exemplo = db.Column(db.String(300))  # grafia original (humano)
    materia_prima_id = db.Column(db.Integer, db.ForeignKey('materia_prima.id'),
                                 nullable=True, index=True)
    ignorar = db.Column(db.Boolean, default=False, nullable=False)

    # Conversao confirmada: 1 unidade-de-compra-da-NF = fator_conversao
    # unidades-base-da-MP. Ex: 1 caixa de 300 batons -> fator 300.
    unidade_compra = db.Column(db.String(20))  # un/kg/g/ml/cx/fardo (informativo)
    fator_conversao = db.Column(db.Float, nullable=False, default=1.0)

    # Sugestoes cruas da IA (pre-preenchem o form; nunca aplicadas sem confirmar).
    ia_unidade_sugerida = db.Column(db.String(20))
    ia_fator_sugerido = db.Column(db.Float)

    primeira_visto_em = db.Column(db.DateTime, default=agora)
    confirmado_em = db.Column(db.DateTime, nullable=True)
    confirmado_por = db.Column(db.Integer, db.ForeignKey('usuario.id'), nullable=True)

    materia_prima = db.relationship('MateriaPrima')

    @property
    def estado(self):
        if self.ignorar:
            return 'ignorado'
        if self.materia_prima_id:
            return 'mapeado'
        return 'pendente'

    @property
    def alvo_nome(self):
        return self.materia_prima.nome if self.materia_prima else None

    @property
    def processavel(self):
        """So da entrada de estoque se mapeado + confirmado + nao-ignorado
        (salvaguarda igual a SeruLojaMap.confirmado_em no Seru)."""
        return bool(self.confirmado_em and self.materia_prima_id and not self.ignorar)


class ContaPagarItemProcessado(db.Model):
    """Idempotencia: cada (conta, indice do item) processa estoque/preco UMA
    vez. Reprocessar a mesma NF NAO duplica entrada nem historico.

    Espelha SeruPedidoProcessado. Guarda o que foi aplicado (auditoria —
    dinheiro/estoque tem peso especial). A entrada vai pro estoque global
    (industria, via movimentacao_id) OU pro EstoqueLoja (lojas, via
    mov_estoque_loja_id) conforme a empresa do canal.
    """
    __tablename__ = 'conta_pagar_item_processado'

    conta_pagar_id = db.Column(db.Integer, db.ForeignKey('conta_pagar.id'),
                               primary_key=True)
    item_indice = db.Column(db.Integer, primary_key=True)  # indice no itens_json
    materia_prima_id = db.Column(db.Integer, db.ForeignKey('materia_prima.id'),
                                 index=True)
    loja_id = db.Column(db.Integer, db.ForeignKey('loja.id'), nullable=True)
    processado_em = db.Column(db.DateTime, default=agora)
    custo_aplicado = db.Column(db.Float)   # custo_base calculado (por unidade-base)
    qtd_estoque = db.Column(db.Float)      # qtd somada na unidade-base da MP
    # Rastro da movimentacao gerada (uma ou outra, conforme o destino):
    movimentacao_id = db.Column(db.Integer,
                                db.ForeignKey('movimentacao_estoque.id'), nullable=True)
    mov_estoque_loja_id = db.Column(db.Integer,
                                    db.ForeignKey('mov_estoque_loja.id'), nullable=True)
    historico_id = db.Column(db.Integer,
                             db.ForeignKey('historico_preco_mp.id'), nullable=True)


class VariacaoPrecoMP(db.Model):
    """Aviso de variacao de preco de MP detectada ao processar uma NF.

    Gerado quando o custo novo difere do anterior (mais caro OU mais barato).
    O preco JA foi aplicado automaticamente; isto e so um aviso pra revisao
    humana. Aprovar/ignorar fecha o alerta, nao mexe no custo. A tela lista
    status='novo' ordenando pelas maiores variacoes (abs(variacao_pct)).
    """
    __tablename__ = 'variacao_preco_mp'

    id = db.Column(db.Integer, primary_key=True)
    materia_prima_id = db.Column(db.Integer, db.ForeignKey('materia_prima.id'),
                                 nullable=False, index=True)
    conta_pagar_id = db.Column(db.Integer, db.ForeignKey('conta_pagar.id'),
                               nullable=True)
    item_indice = db.Column(db.Integer)
    custo_anterior = db.Column(db.Float)
    custo_novo = db.Column(db.Float)
    variacao_pct = db.Column(db.Float)  # (novo-anterior)/anterior*100
    fornecedor_id = db.Column(db.Integer, db.ForeignKey('fornecedor.id'),
                              nullable=True)
    criado_em = db.Column(db.DateTime, default=agora, index=True)
    # 'novo' | 'aprovado' | 'ignorado'
    status = db.Column(db.String(20), default='novo', nullable=False, index=True)
    revisado_em = db.Column(db.DateTime)
    revisado_por_id = db.Column(db.Integer, db.ForeignKey('usuario.id'))

    materia_prima = db.relationship('MateriaPrima')
    conta_pagar = db.relationship('ContaPagar')
    fornecedor = db.relationship('Fornecedor')


class SlackCanalLojaMap(db.Model):
    """Mapeia o canal Slack de recebimento de NF -> Loja (empresa). Cada canal
    = 1 empresa = 1 estoque. Sem confirmacao, NFs daquele canal nao dao entrada.

    Espelha SeruLojaMap: auto-fuzzy na primeira aparicao (pelo nome de
    SLACK_CANAIS_NF_NOMES), admin confirma/corrige na tela. O canal da
    industria roteia pro estoque global de MP (nao pra EstoqueLoja) — ver
    conta_pagar_estoque.
    """
    __tablename__ = 'slack_canal_loja_map'

    id = db.Column(db.Integer, primary_key=True)
    canal_id = db.Column(db.String(40), nullable=False, unique=True, index=True)
    loja_id = db.Column(db.Integer, db.ForeignKey('loja.id'), nullable=True)
    ignorar = db.Column(db.Boolean, default=False, nullable=False)
    auto_match = db.Column(db.Boolean, default=False)  # True se setado via fuzzy
    # True = esta loja e a Industria (entrada vai pro estoque global, nao EstoqueLoja)
    eh_industria = db.Column(db.Boolean, default=False, nullable=False)
    confirmado_em = db.Column(db.DateTime, nullable=True)
    confirmado_por = db.Column(db.Integer, db.ForeignKey('usuario.id'), nullable=True)

    loja = db.relationship('Loja')

    @property
    def estado(self):
        if self.ignorar:
            return 'ignorado'
        if self.loja_id:
            return 'mapeado'
        return 'pendente'
