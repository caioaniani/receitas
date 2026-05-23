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
