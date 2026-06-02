"""Lista de compras semanal por loja.

Modelo de 3 tabelas:
- `ItemListaCompras`  — catálogo: itens que aparecem na lista de cada loja,
  agrupados por 'grupo' (string livre — cobre fornecedor 'AROMAR' OU categoria
  livre como 'FARMACIA'). MVP usa string pra simplificar; futuras versões
  podem vincular a `Fornecedor.id` se quiser.
- `ListaComprasSemana` — uma semana de lista pra uma loja. `data_semana_inicio`
  é sempre o domingo daquela semana. `status`: aberta → enviada → fechada.
- `ListaComprasItemQtd` — 3 números por item por semana:
    - `tenho`: preenchido pelo gerente da loja (inventário visual).
    - `pedido`: preenchido pelo gerente geral (quanto vai comprar).
    - `sobrou`: preenchido pelo gerente geral no fim da semana (histórico).

Faz parte de `app.models` (split em multiplos arquivos por dominio).
"""

from app.extensions import db
from app.utils import agora


class ItemListaCompras(db.Model):
    """Item do catálogo de uma loja — gerente vai marcar 'quanto tenho' deste."""
    __tablename__ = 'item_lista_compras'

    id = db.Column(db.Integer, primary_key=True)
    loja_id = db.Column(db.Integer, db.ForeignKey('loja.id'), nullable=False, index=True)
    grupo = db.Column(db.String(80), nullable=False, index=True)
    nome_item = db.Column(db.String(120), nullable=False)
    ordem = db.Column(db.Integer, default=0, nullable=False)
    ativo = db.Column(db.Boolean, default=True, nullable=False)
    criado_em = db.Column(db.DateTime, default=agora)

    loja = db.relationship('Loja')

    __table_args__ = (
        db.UniqueConstraint('loja_id', 'grupo', 'nome_item',
                            name='uq_item_lista_loja_grupo_nome'),
    )


class ListaComprasSemana(db.Model):
    """Uma semana de lista de compras pra uma loja."""
    __tablename__ = 'lista_compras_semana'

    id = db.Column(db.Integer, primary_key=True)
    loja_id = db.Column(db.Integer, db.ForeignKey('loja.id'), nullable=False, index=True)
    data_semana_inicio = db.Column(db.Date, nullable=False, index=True)
    status = db.Column(db.String(20), default='aberta', nullable=False)

    criado_por_id = db.Column(db.Integer, db.ForeignKey('usuario.id'))
    criado_em = db.Column(db.DateTime, default=agora)
    enviada_em = db.Column(db.DateTime)
    enviada_por_id = db.Column(db.Integer, db.ForeignKey('usuario.id'))
    fechada_em = db.Column(db.DateTime)
    fechada_por_id = db.Column(db.Integer, db.ForeignKey('usuario.id'))

    loja = db.relationship('Loja')
    criado_por = db.relationship('Usuario', foreign_keys=[criado_por_id])
    enviada_por = db.relationship('Usuario', foreign_keys=[enviada_por_id])
    fechada_por = db.relationship('Usuario', foreign_keys=[fechada_por_id])

    quantidades = db.relationship(
        'ListaComprasItemQtd',
        backref='semana',
        cascade='all, delete-orphan',
    )

    __table_args__ = (
        db.UniqueConstraint('loja_id', 'data_semana_inicio',
                            name='uq_lista_semana_loja_data'),
    )


class ListaComprasItemQtd(db.Model):
    """Os 3 números por item por semana."""
    __tablename__ = 'lista_compras_item_qtd'

    id = db.Column(db.Integer, primary_key=True)
    semana_id = db.Column(db.Integer, db.ForeignKey('lista_compras_semana.id'),
                          nullable=False, index=True)
    item_id = db.Column(db.Integer, db.ForeignKey('item_lista_compras.id'),
                        nullable=False, index=True)
    tenho = db.Column(db.Integer, default=0, nullable=False)
    pedido = db.Column(db.Integer, default=0, nullable=False)
    sobrou = db.Column(db.Integer, default=0, nullable=False)
    atualizado_em = db.Column(db.DateTime, default=agora, onupdate=agora)

    item = db.relationship('ItemListaCompras')

    __table_args__ = (
        db.UniqueConstraint('semana_id', 'item_id', name='uq_qtd_semana_item'),
    )
