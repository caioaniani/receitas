"""Modelos do dominio: projetos.

Faz parte de `app.models` (split em multiplos arquivos por dominio
em 2026-05-21). Importar via `from app.models import X` continua
funcionando porque `app/models/__init__.py` re-exporta tudo.
"""

from app.extensions import db
from app.utils import agora, hoje


class ProjetoArea(db.Model):
    __tablename__ = "projeto_area"

    id = db.Column(db.Integer, primary_key=True)
    nome = db.Column(db.String(100), nullable=False, unique=True)
    tipo = db.Column(db.String(20), nullable=False, default="empresa")  # empresa/igreja/vida
    cor = db.Column(db.String(20))  # ex: '#5b8def' — opcional, sobrescreve cor padrao do tipo
    ativa = db.Column(db.Boolean, default=True)
    ordem = db.Column(db.Integer, default=0)

    projetos = db.relationship("Projeto", backref="area",
                                cascade="all, delete-orphan",
                                order_by="Projeto.criado_em.desc()")

class Projeto(db.Model):
    __tablename__ = "projeto"

    id = db.Column(db.Integer, primary_key=True)
    area_id = db.Column(db.Integer, db.ForeignKey("projeto_area.id"), nullable=False)
    nome = db.Column(db.String(200), nullable=False)
    status = db.Column(db.String(20), default="planejado")
    prioridade = db.Column(db.String(10))
    foco_12s = db.Column(db.Boolean, default=False)
    responsavel_id = db.Column(db.Integer, db.ForeignKey("usuario.id"), nullable=True)
    observacao = db.Column(db.Text)
    criado_em = db.Column(db.DateTime, default=agora)
    atualizado_em = db.Column(db.DateTime, default=agora, onupdate=agora)

    responsavel = db.relationship("Usuario")
    tarefas = db.relationship("TarefaProjeto", backref="projeto",
                               cascade="all, delete-orphan",
                               order_by="TarefaProjeto.ordem, TarefaProjeto.id")

    @property
    def tarefas_ativas(self):
        return [t for t in self.tarefas if t.status not in ("feito", "cancelado")]

    @property
    def tem_atrasada(self):
        return any(t.atrasada for t in self.tarefas)

class TarefaProjeto(db.Model):
    __tablename__ = "tarefa_projeto"

    id = db.Column(db.Integer, primary_key=True)
    projeto_id = db.Column(db.Integer, db.ForeignKey("projeto.id"), nullable=False)
    nome = db.Column(db.String(300), nullable=False)
    status = db.Column(db.String(20), default="a_fazer")
    tipo = db.Column(db.String(20))
    esforco = db.Column(db.String(2))
    prazo = db.Column(db.Date, nullable=True)
    responsavel_id = db.Column(db.Integer, db.ForeignKey("usuario.id"), nullable=True)
    observacao = db.Column(db.Text)
    recorrencia = db.Column(db.String(20))  # diaria/semanal/quinzenal/mensal/trimestral
    ordem = db.Column(db.Integer, default=0)
    criado_em = db.Column(db.DateTime, default=agora)
    feito_em = db.Column(db.DateTime, nullable=True)

    responsavel = db.relationship("Usuario")

    @property
    def atrasada(self):
        return (self.prazo is not None
                and self.status not in ("feito", "cancelado")
                and self.prazo < hoje())

class WeeklyReview(db.Model):
    __tablename__ = "weekly_review"

    id = db.Column(db.Integer, primary_key=True)
    data = db.Column(db.Date, default=hoje, nullable=False)
    reflexao = db.Column(db.Text)
    fazendo_count = db.Column(db.Integer, default=0)
    a_fazer_count = db.Column(db.Integer, default=0)
    atrasadas_count = db.Column(db.Integer, default=0)
    foco_count = db.Column(db.Integer, default=0)
    criado_em = db.Column(db.DateTime, default=agora)
    criado_por = db.Column(db.Integer, db.ForeignKey("usuario.id"))

    autor = db.relationship("Usuario")


# ── Templates de Projeto ──

class ProjetoTemplate(db.Model):
    __tablename__ = "projeto_template"

    id = db.Column(db.Integer, primary_key=True)
    nome = db.Column(db.String(150), nullable=False)
    area_id_padrao = db.Column(db.Integer, db.ForeignKey("projeto_area.id"), nullable=True)
    descricao = db.Column(db.Text)
    criado_em = db.Column(db.DateTime, default=agora)

    area_padrao = db.relationship("ProjetoArea")
    tarefas = db.relationship("TarefaTemplate", backref="template",
                               cascade="all, delete-orphan",
                               order_by="TarefaTemplate.ordem")

class TarefaTemplate(db.Model):
    __tablename__ = "tarefa_template"

    id = db.Column(db.Integer, primary_key=True)
    template_id = db.Column(db.Integer, db.ForeignKey("projeto_template.id"), nullable=False)
    nome = db.Column(db.String(300), nullable=False)
    tipo = db.Column(db.String(20))
    esforco = db.Column(db.String(2))
    dias_prazo = db.Column(db.Integer)  # dias a partir da criacao do projeto
    ordem = db.Column(db.Integer, default=0)


# ── Integracao Seru (PDV): mapeamento de produtos/lojas + idempotencia ──
