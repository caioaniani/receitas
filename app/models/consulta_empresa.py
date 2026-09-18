"""Cache cadastral público e orçamento compartilhado de consultas externas."""
from app.extensions import db


class ConsultaEmpresaCache(db.Model):
    __tablename__ = 'consulta_empresa_cache'

    documento = db.Column(db.String(14), primary_key=True)
    resultado = db.Column(db.JSON, nullable=False)
    tem_ie = db.Column(db.Boolean, nullable=False)
    consultado_em = db.Column(db.DateTime, nullable=False)
    expira_em = db.Column(db.DateTime, nullable=False, index=True)


class ConsultaEmpresaLimite(db.Model):
    __tablename__ = 'consulta_empresa_limite'

    provedor = db.Column(db.String(40), primary_key=True)
    proxima_em = db.Column(db.DateTime, nullable=False)
