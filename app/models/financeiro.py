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
    # Conferencia humana (Fase 2, 2026-06-10): NULL = ninguem conferiu —
    # os dados podem ser so o chute da IA. Editar a conta confere
    # automaticamente; o botao "Conferida" na tela tambem. Colunas criadas
    # por ALTER em migrations_legacy (deploy bb9f1cf) ANTES deste modelo.
    revisada_em = db.Column(db.DateTime, nullable=True)
    revisada_por_id = db.Column(db.Integer, db.ForeignKey('usuario.id'),
                                 nullable=True)

    fornecedor = db.relationship('Fornecedor')
    revisada_por = db.relationship('Usuario', foreign_keys=[revisada_por_id])
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
    def revisada(self):
        return self.revisada_em is not None

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
    # Alvo alternativo: PRODUTO de revenda (agua/chiclete/iogurte comprado
    # pronto) — mutuamente exclusivo com materia_prima_id. Espelha
    # SeruProdutoMap. Coluna criada via migrations_legacy (ALTER deployado
    # e confirmado em 10/06/2026, antes deste modelo).
    produto_id = db.Column(db.Integer, db.ForeignKey('produto.id'),
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
    produto = db.relationship('Produto')

    @property
    def estado(self):
        if self.ignorar:
            return 'ignorado'
        if self.materia_prima_id or self.produto_id:
            return 'mapeado'
        return 'pendente'

    @property
    def alvo_nome(self):
        if self.materia_prima:
            return self.materia_prima.nome
        if self.produto:
            return f'{self.produto.nome} (produto)'
        return None

    @property
    def processavel(self):
        """So da entrada de estoque se mapeado + confirmado + nao-ignorado
        (salvaguarda igual a SeruLojaMap.confirmado_em no Seru)."""
        return bool(self.confirmado_em and not self.ignorar
                    and (self.materia_prima_id or self.produto_id))


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


# ── Cobranças (contas a RECEBER — boleto híbrido Sicredi, 04/07/2026) ──────
#
# Fluxo: parcela de VendaB2B vira Cobranca -> entra num arquivo de REMESSA
# CNAB400 (CobrancaRemessa) enviado ao Sicredi -> o RETORNO do banco confirma
# o registro (ocorrência 02), traz o QR Pix do boleto híbrido (registro tipo
# 8) e dá baixa nas liquidações (06/15/17), que também quitam a parcela.
# Layouts: manuais Sicredi (CNAB400 + boleto híbrido) — ver
# app/services/sicredi_cnab.py.

class CobrancaRemessa(db.Model):
    """Um arquivo .rem gerado (sequencial obrigatório pelo banco)."""
    __tablename__ = 'cobranca_remessa'

    id = db.Column(db.Integer, primary_key=True)
    numero = db.Column(db.Integer, nullable=False, unique=True)  # 1, 2, 3...
    gerado_em = db.Column(db.DateTime, default=agora, nullable=False)
    gerado_por_id = db.Column(db.Integer, db.ForeignKey('usuario.id'))
    n_titulos = db.Column(db.Integer, nullable=False, default=0)
    conteudo = db.Column(db.Text, nullable=False)   # pra re-baixar o arquivo

    gerado_por = db.relationship('Usuario')

    @property
    def nome_arquivo(self):
        """Nomenclatura EXIGIDA pelo Sicredi (e-mail da homologação,
        07/07/2026): CCCCCmdd.CRM — código do cedente (5) + mês (1-9,
        O=out, N=nov, D=dez) + dia (2). Vários arquivos no MESMO dia:
        1º .CRM, 2º .RM2, 3º .RM3...

        (O formato antigo REMnnnnn.CRM era só da homologação por e-mail;
        no Sicredi Internet o nome errado é recusado.)"""
        from app.services.sicredi_cnab import _cfg
        cedente = _cfg()['beneficiario']
        d = self.gerado_em
        mes = '123456789OND'[d.month - 1]
        # Ordinal do arquivo NO DIA (1º, 2º...) pela ordem do sequencial.
        ordem = (CobrancaRemessa.query
                 .filter(db.func.date(CobrancaRemessa.gerado_em) == d.date(),
                         CobrancaRemessa.numero <= self.numero)
                 .count())
        ext = 'CRM' if ordem <= 1 else f'RM{ordem}'
        return f'{cedente}{mes}{d.day:02d}.{ext}'


class Cobranca(db.Model):
    """Um título de cobrança (boleto). Snapshot do pagador na criação —
    editar o cliente depois não muda boleto já emitido."""
    __tablename__ = 'cobranca'

    id = db.Column(db.Integer, primary_key=True)
    parcela_id = db.Column(db.Integer, db.ForeignKey('venda_b2b_parcela.id'),
                           nullable=True, unique=True, index=True)
    # Boleto de FATURA mensal (07/07/2026): cobre TODAS as parcelas do
    # fechamento de uma vez (a liquidacao quita a fatura + parcelas).
    # Mutuamente exclusivo com parcela_id na pratica (um boleto por fatura).
    fatura_id = db.Column(db.Integer, db.ForeignKey('fatura_b2b.id'),
                          nullable=True, unique=True, index=True)
    # Pagador (snapshot)
    pagador_nome = db.Column(db.String(100), nullable=False)
    pagador_cnpj_cpf = db.Column(db.String(20), nullable=False)
    pagador_endereco = db.Column(db.String(250))
    pagador_cep = db.Column(db.String(9))

    valor = db.Column(db.Numeric(10, 2), nullable=False)
    vencimento = db.Column(db.Date, nullable=False, index=True)
    emissao = db.Column(db.Date, nullable=False)
    seu_numero = db.Column(db.String(10), nullable=False)  # ref interna (NF/venda)
    # Nosso número completo com DV, 9 dígitos: AA B NNNNN D (ex: 252000041).
    nosso_numero = db.Column(db.String(9), unique=True, index=True)

    # pendente -> remessa (no arquivo) -> registrada (ocorr. 02) ->
    # paga (06/15/17) | baixada (09/10) | rejeitada (03)
    status = db.Column(db.String(15), nullable=False, default='pendente',
                       index=True)
    remessa_id = db.Column(db.Integer, db.ForeignKey('cobranca_remessa.id'),
                           index=True)
    valor_pago = db.Column(db.Numeric(10, 2))
    pago_em = db.Column(db.Date)
    motivo_retorno = db.Column(db.String(60))   # motivo da rejeição/ocorrência

    # QR Pix do boleto híbrido (chega no RETORNO, registro tipo 8)
    pix_txid = db.Column(db.String(35))
    pix_url = db.Column(db.String(120))
    pix_copia_cola = db.Column(db.Text)

    criado_em = db.Column(db.DateTime, default=agora, nullable=False)
    criado_por_id = db.Column(db.Integer, db.ForeignKey('usuario.id'))

    parcela = db.relationship('VendaB2BParcela', backref='cobranca')
    fatura = db.relationship('FaturaB2B', backref='cobrancas')
    remessa = db.relationship('CobrancaRemessa', backref='cobrancas')
    criado_por = db.relationship('Usuario')

    @property
    def nosso_numero_fmt(self):
        """25/200004-1 (formato humano do manual)."""
        n = self.nosso_numero or ''
        if len(n) != 9:
            return n
        return f'{n[:2]}/{n[2:8]}-{n[8]}'

    @property
    def vencida(self):
        from app.utils import hoje
        return (self.status in ('pendente', 'remessa', 'registrada')
                and self.vencimento < hoje())


class EnvioCobranca(db.Model):
    """Auditoria de envio, não de entrega/leitura nem de pagamento.

    Referências são snapshots sem FK: exclusões permitidas pelo fluxo antigo
    não apagam o comprovante. Nenhum PDF, token do provedor ou dado bancário
    completo é persistido aqui. Tabela nova criada por _setup_schema.
    """
    __tablename__ = 'envio_cobranca'

    id = db.Column(db.Integer, primary_key=True)
    chave = db.Column(db.String(36), unique=True, nullable=False)
    fatura_id = db.Column(db.Integer, index=True)
    venda_id = db.Column(db.Integer, index=True)
    cobranca_ids = db.Column(db.JSON, nullable=False, default=list)
    referencia = db.Column(db.String(120), nullable=False)
    destinatario = db.Column(db.String(254), nullable=False)
    documentos = db.Column(db.String(20), nullable=False)  # nf | boleto | nf_boleto
    nf_id = db.Column(db.String(40))
    anexos = db.Column(db.JSON, nullable=False, default=list)
    status = db.Column(db.String(20), nullable=False)  # preparando | aceito | falha | incerto
    provedor_id = db.Column(db.String(150))
    erro = db.Column(db.String(500))
    criado_em = db.Column(db.DateTime, default=agora, nullable=False, index=True)
    concluido_em = db.Column(db.DateTime)
    usuario_id = db.Column(db.Integer)
    usuario_nome = db.Column(db.String(100))
