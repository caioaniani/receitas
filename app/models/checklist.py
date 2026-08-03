"""Checklist de loja (03/08/2026) — abertura, troca de turno e fechamento.

Pedido do dono: o gerente/atendente chefe responsável do turno preenche um
checklist na tela do celular e tira FOTO comprovando os pontos que o dono
marcou como "exige foto". Decisões dele (AskUserQuestion 03/08/2026): tela
no celular (não Slack), itens CADASTRÁVEIS em tela (sem deploy pra mudar),
foto por item selecionado, e cobrança como pendência na home.

Tabelas NOVAS via `db.create_all` — sem ALTER, sem procedimento de 2 commits
(que vale pra COLUNA nova em tabela existente, não pra tabela nova).
"""
from app.extensions import db
from app.utils import agora


class ChecklistItemModelo(db.Model):
    """Um ponto do checklist, cadastrado pelo admin em /checklist/config.

    `loja_id` NULL = vale pra TODAS as lojas (o comum); preenchido = item
    específico daquela loja (soma aos globais, não substitui).
    Item nunca é apagado se já tem resposta gravada — vira `ativo=False`
    (a resposta histórica guarda snapshot do texto, mas a FK fica viva).
    """
    __tablename__ = 'checklist_item_modelo'

    id = db.Column(db.Integer, primary_key=True)
    tipo = db.Column(db.String(20), nullable=False, index=True)
    texto = db.Column(db.String(300), nullable=False)
    exige_foto = db.Column(db.Boolean, nullable=False, default=False)
    ordem = db.Column(db.Integer, nullable=False, default=0)
    ativo = db.Column(db.Boolean, nullable=False, default=True)
    loja_id = db.Column(db.Integer, db.ForeignKey('loja.id'), nullable=True)
    criado_em = db.Column(db.DateTime, default=agora)

    loja = db.relationship('Loja')

    def __repr__(self):
        return f'<ChecklistItemModelo {self.tipo} {self.texto!r}>'


class ChecklistPreenchimento(db.Model):
    """Um checklist RESPONDIDO: quem, qual loja, qual turno, quando.

    Sem unique em (loja, tipo, data) DE PROPÓSITO: troca de turno pode
    acontecer mais de uma vez no dia, e um segundo preenchimento de
    abertura é informação (a tela avisa que já havia um), não erro.
    """
    __tablename__ = 'checklist_preenchimento'

    id = db.Column(db.Integer, primary_key=True)
    loja_id = db.Column(db.Integer, db.ForeignKey('loja.id'),
                        nullable=False, index=True)
    tipo = db.Column(db.String(20), nullable=False)
    data = db.Column(db.Date, nullable=False, index=True)
    criado_em = db.Column(db.DateTime, default=agora)
    usuario_id = db.Column(db.Integer, db.ForeignKey('usuario.id'),
                           nullable=False)
    observacao = db.Column(db.String(500), nullable=True)

    loja = db.relationship('Loja')
    usuario = db.relationship('Usuario')
    respostas = db.relationship('ChecklistResposta', backref='preenchimento',
                                cascade='all, delete-orphan',
                                order_by='ChecklistResposta.id')

    @property
    def n_problemas(self):
        return sum(1 for r in self.respostas if not r.ok)

    def __repr__(self):
        return (f'<ChecklistPreenchimento loja={self.loja_id} '
                f'{self.tipo} {self.data}>')


class ChecklistResposta(db.Model):
    """Resposta de UM item: OK ou problema, com a foto quando exigida.

    `item_texto`/`exigia_foto` são SNAPSHOT do item no momento do
    preenchimento — o dono edita o cadastro depois sem reescrever a
    história do que foi cobrado naquele turno.
    Foto vive no Dropbox (`foto_url` raw) — nunca BLOB (regra M6).
    """
    __tablename__ = 'checklist_resposta'

    id = db.Column(db.Integer, primary_key=True)
    preenchimento_id = db.Column(
        db.Integer, db.ForeignKey('checklist_preenchimento.id'),
        nullable=False, index=True)
    item_id = db.Column(db.Integer,
                        db.ForeignKey('checklist_item_modelo.id'),
                        nullable=True)
    item_texto = db.Column(db.String(300), nullable=False)
    exigia_foto = db.Column(db.Boolean, nullable=False, default=False)
    ok = db.Column(db.Boolean, nullable=False, default=True)
    observacao = db.Column(db.String(500), nullable=True)
    foto_url = db.Column(db.String(500), nullable=True)
    foto_storage_path = db.Column(db.String(500), nullable=True)

    def __repr__(self):
        return (f'<ChecklistResposta {self.item_texto!r} '
                f'{"ok" if self.ok else "problema"}>')
