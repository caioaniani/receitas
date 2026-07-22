"""Módulo de treinamento gamificado (24/07/2026, pedido do dono).

O dono grava vídeos, monta um quiz com pontuação, e cada funcionário loga
(pela conta ligada ao RH — ver `Funcionario.usuario`), assiste e responde.
Quem ASSISTE TUDO e passa no quiz (nota mínima) fica ELEGÍVEL a sorteio/bônus;
o sorteio/bônus é gesto manual do dono (o sistema não mexe em folha sozinho).

Todas as tabelas aqui são NOVAS -> criadas pelo `db.create_all` no startup,
sem migration (o único ALTER do módulo é `funcionario.usuario_id`, já aplicado
pelo procedimento de 2 commits em migrations_legacy).

Fonte do vídeo é SWAPPABLE de propósito (`video_tipo`):
- 'stream'  -> Cloudflare Stream (PADRÃO em prod, decisão do dono 24/07/2026);
  `video_ref` = UID do vídeo no Cloudflare. O upload vai DIRETO do navegador
  pro Cloudflare e o player é um iframe embutido na nossa página.
- 'arquivo' -> hospedado no nosso servidor (volume Railway em /data);
  `video_ref` = nome do arquivo, servido por rota própria com HTTP Range.
  (Fica como fallback; esbarra em permissão de volume no Railway.)
- 'embed'   -> URL de player externo (fallback/escape hatch); `video_ref` = URL.
Trocar de fonte é só mudar `video_tipo` — o resto do módulo não muda.
"""
from app.extensions import db
from app.utils import agora

__all__ = [
    'Treinamento',
    'TreinamentoPergunta',
    'TreinamentoOpcao',
    'TreinamentoTentativa',
    'TreinamentoConclusao',
]

VIDEO_TIPOS = ('arquivo', 'embed')


class Treinamento(db.Model):
    __tablename__ = 'treinamento'

    id = db.Column(db.Integer, primary_key=True)
    titulo = db.Column(db.String(200), nullable=False)
    descricao = db.Column(db.Text)
    # Vídeo (fonte swappable — ver docstring do módulo).
    video_tipo = db.Column(db.String(10), default='arquivo', nullable=False)
    video_ref = db.Column(db.String(500))     # nome do arquivo OU URL de embed
    ordem = db.Column(db.Integer, default=0, nullable=False, index=True)
    ativo = db.Column(db.Boolean, default=True, nullable=False)
    # Nota mínima pra "passar" (percentual de acerto). Default 70%.
    nota_minima = db.Column(db.Integer, default=70, nullable=False)
    criado_em = db.Column(db.DateTime, default=agora, nullable=False, index=True)
    criado_por_id = db.Column(db.Integer, db.ForeignKey('usuario.id'))
    # Soft-delete: treinamento com histórico (tentativas) nunca é excluído.
    apagado_em = db.Column(db.DateTime, nullable=True, index=True)

    criado_por = db.relationship('Usuario', foreign_keys=[criado_por_id])
    perguntas = db.relationship(
        'TreinamentoPergunta', backref='treinamento',
        order_by='TreinamentoPergunta.ordem',
        cascade='all, delete-orphan')

    @property
    def total_perguntas(self):
        return len(self.perguntas)


class TreinamentoPergunta(db.Model):
    __tablename__ = 'treinamento_pergunta'

    id = db.Column(db.Integer, primary_key=True)
    treinamento_id = db.Column(
        db.Integer, db.ForeignKey('treinamento.id'), nullable=False, index=True)
    enunciado = db.Column(db.Text, nullable=False)
    ordem = db.Column(db.Integer, default=0, nullable=False)

    opcoes = db.relationship(
        'TreinamentoOpcao', backref='pergunta',
        order_by='TreinamentoOpcao.ordem',
        cascade='all, delete-orphan')


class TreinamentoOpcao(db.Model):
    __tablename__ = 'treinamento_opcao'

    id = db.Column(db.Integer, primary_key=True)
    pergunta_id = db.Column(
        db.Integer, db.ForeignKey('treinamento_pergunta.id'),
        nullable=False, index=True)
    texto = db.Column(db.String(500), nullable=False)
    correta = db.Column(db.Boolean, default=False, nullable=False)
    ordem = db.Column(db.Integer, default=0, nullable=False)


class TreinamentoTentativa(db.Model):
    """Log de CADA tentativa de quiz de um funcionário. A elegibilidade e o
    progresso vêm de `TreinamentoConclusao` (rollup); aqui fica o histórico."""
    __tablename__ = 'treinamento_tentativa'

    id = db.Column(db.Integer, primary_key=True)
    treinamento_id = db.Column(
        db.Integer, db.ForeignKey('treinamento.id'), nullable=False, index=True)
    usuario_id = db.Column(
        db.Integer, db.ForeignKey('usuario.id'), nullable=False, index=True)
    acertos = db.Column(db.Integer, default=0, nullable=False)
    total = db.Column(db.Integer, default=0, nullable=False)
    pontos = db.Column(db.Integer, default=0, nullable=False)
    aprovado = db.Column(db.Boolean, default=False, nullable=False)
    criado_em = db.Column(db.DateTime, default=agora, nullable=False, index=True)

    treinamento = db.relationship('Treinamento')
    usuario = db.relationship('Usuario')

    @property
    def percentual(self):
        return round(100 * self.acertos / self.total) if self.total else 0


class TreinamentoConclusao(db.Model):
    """Rollup por (usuário, treinamento): assistiu? passou? melhor pontuação?
    UMA linha por par. Completar = assistido E aprovado. Elegível ao
    sorteio/bônus = concluiu TODOS os treinamentos ativos."""
    __tablename__ = 'treinamento_conclusao'
    __table_args__ = (
        db.UniqueConstraint('usuario_id', 'treinamento_id',
                            name='uq_treino_conclusao_usuario_treino'),
    )

    id = db.Column(db.Integer, primary_key=True)
    usuario_id = db.Column(
        db.Integer, db.ForeignKey('usuario.id'), nullable=False, index=True)
    treinamento_id = db.Column(
        db.Integer, db.ForeignKey('treinamento.id'), nullable=False, index=True)
    assistido_em = db.Column(db.DateTime, nullable=True)
    aprovado_em = db.Column(db.DateTime, nullable=True)
    melhor_pontos = db.Column(db.Integer, default=0, nullable=False)
    atualizado_em = db.Column(db.DateTime, default=agora, nullable=False)

    usuario = db.relationship('Usuario')
    treinamento = db.relationship('Treinamento')

    @property
    def completo(self):
        return self.assistido_em is not None and self.aprovado_em is not None
