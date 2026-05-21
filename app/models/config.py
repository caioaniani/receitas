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
