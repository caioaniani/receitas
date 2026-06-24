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

    receita = db.relationship('Receita')

    def __repr__(self):
        return f'<PlanejamentoItem receita={self.receita_id} x{self.multiplicador}>'
