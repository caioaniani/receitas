"""Notificacoes WhatsApp (Z-API): log de envios + automacoes configuraveis.

Tabelas criadas por db.create_all() no startup (mesmo caminho de ContaPagar).
"""
from app.extensions import db
from app.utils import agora


class NotificacaoWhatsapp(db.Model):
    """Registro de cada mensagem enviada pelo Z-API — alimenta o historico da
    aba (texto, numero, quando, ok/erro, origem). Apenas log."""
    __tablename__ = 'notificacao_whatsapp'

    id = db.Column(db.Integer, primary_key=True)
    numero = db.Column(db.String(20))
    mensagem = db.Column(db.Text)
    # origem: 'digest_tarefas' | 'digest_anomalias' | 'automacao:<id>' | 'manual'
    origem = db.Column(db.String(60), index=True)
    ok = db.Column(db.Boolean, default=False, nullable=False)
    # Identificador devolvido pela Z-API. NULL significa que o envio nunca foi
    # confirmado — HTTP 200 sozinho nao basta.
    zaap_id = db.Column(db.String(120), nullable=True, index=True)
    erro = db.Column(db.String(300))
    criado_em = db.Column(db.DateTime, default=agora, index=True)


class AutomacaoWhatsapp(db.Model):
    """Mensagem de WhatsApp agendada, criada pelo admin na tela. O job mestre
    (seru_cron) dispara as ativas no horario, 1x por dia (por dia permitido)."""
    __tablename__ = 'automacao_whatsapp'

    id = db.Column(db.Integer, primary_key=True)
    nome = db.Column(db.String(120), nullable=False)
    ativo = db.Column(db.Boolean, default=True, nullable=False)
    horario = db.Column(db.String(5), nullable=False)         # 'HH:MM' em BRT
    # CSV de dias da semana (0=seg .. 6=dom); vazio/'todos' = todo dia.
    dias_semana = db.Column(db.String(20))
    mensagem = db.Column(db.Text, nullable=False)
    destino = db.Column(db.String(20))                        # numero; vazio = ZAPI_NUMERO_DESTINO
    ultimo_disparo_em = db.Column(db.DateTime, nullable=True)
    criado_em = db.Column(db.DateTime, default=agora)
    criado_por_id = db.Column(db.Integer, db.ForeignKey('usuario.id'))

    @property
    def dias_set(self):
        """Set de dias permitidos (0=seg..6=dom). Vazio/'todos' = todos."""
        raw = (self.dias_semana or '').strip()
        if not raw or raw == 'todos':
            return set(range(7))
        out = {int(p) for p in raw.split(',') if p.strip().isdigit()}
        return out or set(range(7))
