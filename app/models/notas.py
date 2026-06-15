"""Memoria persistente do agente (15/06/2026).

Notas em markdown que o copilot/bot consultam ANTES de responder regras de
negocio, e que o copilot REGISTRA quando o dono ensina algo novo. Substitui
a "memoria efemera" que evaporava a cada sessao (o dono dizia "agora
cookies cortam em 5", o copilot esquecia no proximo deploy).

Decisao do dono 15/06/2026 sobre Obsidian: nao precisa ferramenta externa,
o conhecimento mora no proprio sistema (paginia /notas). Mas o formato eh
markdown puro, entao da pra exportar pra Obsidian/Notion/qualquer coisa
quando quiser.

Tabela nova: criada pelo `db.create_all` no startup, sem ALTER.
"""
from app.extensions import db
from app.utils import agora

__all__ = ['Nota']


# Origem de quem criou a nota — pra auditoria + pra UI mostrar "voce
# disse isso pelo Slack em X dia". Mantemos uma lista fechada pra evitar
# 'string drift'.
ORIGENS = (
    'admin',           # criado/editado manualmente em /notas
    'copilot_slack',   # copilot via DM/@mention no Slack
    'copilot_wpp',     # copilot via WhatsApp do dono (zapi_bot)
    'bot_padeiro',     # bot de atendimento (Chatwoot) — futuro, hoje so read
)


class Nota(db.Model):
    __tablename__ = 'nota'

    id = db.Column(db.Integer, primary_key=True)
    titulo = db.Column(db.String(200), nullable=False)
    conteudo = db.Column(db.Text, nullable=False)
    # Tags em CSV pra busca simples. Ex: "loja-anesio,cookie,corte".
    # Lowercase, sem acentos (normalizado no service).
    tags = db.Column(db.String(500), default='', nullable=False)
    origem = db.Column(db.String(30), default='admin', nullable=False)
    criada_em = db.Column(db.DateTime, default=agora, nullable=False)
    criada_por_id = db.Column(db.Integer, db.ForeignKey('usuario.id'),
                              nullable=True)
    atualizada_em = db.Column(db.DateTime)
    # Soft delete: o copilot pode arquivar sem perder historico, e o admin
    # restaura na UI. Nota arquivada NAO aparece nas buscas dos agentes.
    arquivada_em = db.Column(db.DateTime)

    criada_por = db.relationship('Usuario', foreign_keys=[criada_por_id])

    @property
    def ativa(self):
        return self.arquivada_em is None

    @property
    def lista_tags(self):
        return [t.strip() for t in (self.tags or '').split(',') if t.strip()]
