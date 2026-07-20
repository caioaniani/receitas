"""Patrimônio — inventário de móveis e equipamentos (20/07/2026).

Pedido do dono: "preciso fazer o inventário da indústria e das lojas,
pensei em colar aqueles códigos de barras ou QR Code". Cada ativo (forno,
amassadeira, freezer, mesa, TV…) ganha uma etiqueta QR que aponta pra
página de conferência: escaneou → confirma "está aqui, neste estado".
O inventário é o conjunto das conferências — o relatório mostra o que
ninguém achou desde uma data.

Separado do estoque de PROPÓSITO: patrimônio não tem quantidade nem baixa
de venda — é presença + estado de itens únicos. Tabelas novas via
`db.create_all` (sem ALTER).
"""
from app.extensions import db
from app.utils import agora


class Ativo(db.Model):
    """Um móvel/equipamento com identidade própria (1 linha = 1 etiqueta).

    `loja_id` NULL = indústria (mesma convenção do resto do sistema).
    `situacao`: em_uso | manutencao | baixado (baixado sai das etiquetas e
    do inventário, mas fica no histórico — nunca se apaga patrimônio).
    """
    __tablename__ = 'ativo'

    id = db.Column(db.Integer, primary_key=True)
    nome = db.Column(db.String(200), nullable=False)
    categoria = db.Column(db.String(60))          # Forno, Refrigeração, Mobiliário…
    loja_id = db.Column(db.Integer, db.ForeignKey('loja.id'), index=True)
    local_detalhe = db.Column(db.String(120))     # "cozinha", "salão", "balcão"
    numero_serie = db.Column(db.String(120))
    # Dinheiro em Numeric (regra da casa, B4) — opcional, pra seguro/contábil.
    valor_aquisicao = db.Column(db.Numeric(10, 2))
    adquirido_em = db.Column(db.Date)
    situacao = db.Column(db.String(20), nullable=False, default='em_uso',
                         index=True)
    baixado_em = db.Column(db.DateTime)
    observacao = db.Column(db.Text)
    criado_em = db.Column(db.DateTime, default=agora, nullable=False)
    criado_por_id = db.Column(db.Integer, db.ForeignKey('usuario.id'))

    loja = db.relationship('Loja')
    conferencias = db.relationship(
        'AtivoConferencia', backref='ativo', lazy=True,
        cascade='all, delete-orphan',
        order_by='AtivoConferencia.momento.desc()')

    @property
    def codigo(self):
        """Código humano da etiqueta (A-0001)."""
        return f'A-{self.id:04d}'

    @property
    def local_nome(self):
        return self.loja.nome if self.loja else 'Indústria'


class AtivoConferencia(db.Model):
    """Um "vi este ativo" — o átomo do inventário.

    Registra quem conferiu, quando, ONDE viu (pode divergir do cadastro —
    a lista avisa; mover o cadastro é gesto do admin) e o estado
    (ok | problema, com observação livre)."""
    __tablename__ = 'ativo_conferencia'

    id = db.Column(db.Integer, primary_key=True)
    ativo_id = db.Column(db.Integer, db.ForeignKey('ativo.id'),
                         nullable=False, index=True)
    momento = db.Column(db.DateTime, default=agora, nullable=False, index=True)
    usuario_id = db.Column(db.Integer, db.ForeignKey('usuario.id'))
    loja_id_visto = db.Column(db.Integer, db.ForeignKey('loja.id'))
    estado = db.Column(db.String(20), nullable=False, default='ok')
    observacao = db.Column(db.String(500))

    usuario = db.relationship('Usuario')
    loja_vista = db.relationship('Loja')
