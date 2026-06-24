"""Modelos do dominio: config.

Faz parte de `app.models` (split em multiplos arquivos por dominio
em 2026-05-21). Importar via `from app.models import X` continua
funcionando porque `app/models/__init__.py` re-exporta tudo.
"""

from app.extensions import db


class AppConfig(db.Model):
    """Key-value generico pra configuracoes runtime (sem precisar de
    redeploy/env var). Use AppConfig.get(k, default) e AppConfig.set(k, v)."""
    __tablename__ = 'app_config'

    key = db.Column(db.String(100), primary_key=True)
    value = db.Column(db.Text)

    @classmethod
    def get(cls, key, default=None):
        row = cls.query.filter_by(key=key).first()
        return row.value if row else default

    @classmethod
    def get_int(cls, key, default=None):
        v = cls.get(key)
        if v is None:
            return default
        try:
            return int(v)
        except (TypeError, ValueError):
            return default

    @classmethod
    def set(cls, key, value):
        row = cls.query.filter_by(key=key).first()
        v = str(value) if value is not None else None
        if row:
            row.value = v
        else:
            row = cls(key=key, value=v)
            db.session.add(row)
        return row


class PermissaoPapel(db.Model):
    """Override editavel de permissao por papel (web + copilot + Slack).

    O CODIGO define o padrao (app/services/permissoes.py::CAP_DEFAULT), que
    espelha o comportamento legado. Linhas aqui SOBREPOEM esse padrao; a
    ausencia de linha = usa o padrao. admin/owner ignoram tudo isso (sempre
    acesso total) — entao nao da pra se trancar fora do sistema.
    """
    __tablename__ = 'permissao_papel'

    id = db.Column(db.Integer, primary_key=True)
    papel = db.Column(db.String(20), nullable=False)
    capacidade = db.Column(db.String(50), nullable=False)
    permitido = db.Column(db.Boolean, nullable=False, default=True)

    __table_args__ = (
        db.UniqueConstraint('papel', 'capacidade', name='uq_permissao_papel_cap'),
    )
