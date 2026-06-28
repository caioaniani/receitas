"""Modelos do dominio: producao.

Faz parte de `app.models` (split em multiplos arquivos por dominio
em 2026-05-21). Importar via `from app.models import X` continua
funcionando porque `app/models/__init__.py` re-exporta tudo.
"""

from app.extensions import db
from app.utils import agora


class PlanejamentoProducao(db.Model):
    __tablename__ = 'planejamento_producao'

    id = db.Column(db.Integer, primary_key=True)
    data = db.Column(db.Date, nullable=False)
    nome = db.Column(db.String(100))
    criado_em = db.Column(db.DateTime, default=agora)
    criado_por = db.Column(db.Integer, db.ForeignKey('usuario.id'))
    status = db.Column(db.String(20), default='rascunho')
    # 'cronograma' = plano aprovado do cronograma diario (desce pro padeiro);
    # NULL/'manual' = plano avulso/deficit. So pra distinguir na UI.
    origem = db.Column(db.String(20))
    # Fluxo de 2 passos: APROVAR cria a ordem (rascunho, enviado_ao_padeiro=
    # False) -> ENVIAR libera pro padeiro (True). O padeiro só vê o que foi
    # enviado. Ordens antigas nascem True (coluna DEFAULT TRUE na migração).
    enviado_ao_padeiro = db.Column(db.Boolean, default=True)

    itens = db.relationship('PlanejamentoItem', backref='planejamento',
                            cascade='all, delete-orphan', lazy=True)
    autor = db.relationship('Usuario', backref='planejamentos')

    def __repr__(self):
        return f'<Planejamento {self.nome} em {self.data}>'

class PlanejamentoItem(db.Model):
    __tablename__ = 'planejamento_item'

    id = db.Column(db.Integer, primary_key=True)
    planejamento_id = db.Column(db.Integer, db.ForeignKey('planejamento_producao.id'), nullable=False)
    receita_id = db.Column(db.Integer, db.ForeignKey('receita.id'), nullable=False)
    multiplicador = db.Column(db.Integer, default=1)
    # Unidades-alvo (do cronograma) e quanto ja foi produzido. produzido_qtd
    # avanca quando o padeiro marca producao (credita estoque + baixa MP).
    qtd_alvo = db.Column(db.Integer)
    produzido_qtd = db.Column(db.Integer, nullable=False, default=0,
                              server_default='0')

    receita = db.relationship('Receita')

    def __repr__(self):
        return f'<PlanejamentoItem receita={self.receita_id} x{self.multiplicador}>'


class PrevisaoSnapshot(db.Model):
    """Instrumentacao de acuracia do forecast (28/06/2026): congela o
    `previsto` do pedido semanal por (data de entrega, loja, receita) no
    momento em que foi gerado, e depois casa com o `realizado` (entregue)
    pra medir vies e erro. Sem isso nao havia como saber se a previsao
    acerta — qualquer 'melhoria' era no escuro.

    Uma linha por (data_alvo, loja, receita): grava-se a PRIMEIRA previsao
    vista pra aquela data-alvo (tipicamente ~7 dias antes), pra medir sempre
    no mesmo lead. `realizado` fica NULL ate a data passar e o cron casar.
    """
    __tablename__ = 'previsao_snapshot'

    id = db.Column(db.Integer, primary_key=True)
    data_alvo = db.Column(db.Date, nullable=False, index=True)
    loja_id = db.Column(db.Integer, db.ForeignKey('loja.id'), nullable=False)
    receita_id = db.Column(db.Integer, db.ForeignKey('receita.id'),
                           nullable=False)
    previsto = db.Column(db.Integer, nullable=False, default=0)
    # NULL ate a data_alvo passar; preenchido pelo cron com o entregue real.
    realizado = db.Column(db.Integer, nullable=True)
    casado_em = db.Column(db.DateTime, nullable=True)
    criado_em = db.Column(db.DateTime, default=agora, index=True)

    loja = db.relationship('Loja')
    receita = db.relationship('Receita')

    __table_args__ = (
        db.UniqueConstraint('data_alvo', 'loja_id', 'receita_id',
                            name='uq_previsao_snapshot_alvo'),
    )

    def __repr__(self):
        return (f'<PrevisaoSnapshot {self.data_alvo} loja={self.loja_id} '
                f'rec={self.receita_id} prev={self.previsto} '
                f'real={self.realizado}>')
