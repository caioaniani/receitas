"""Modelos do dominio: entregas.

Faz parte de `app.models` (split em multiplos arquivos por dominio
em 2026-05-21). Importar via `from app.models import X` continua
funcionando porque `app/models/__init__.py` re-exporta tudo.
"""

from app.extensions import db
from app.utils import agora


class CartinhaEntrega(db.Model):
    __tablename__ = 'cartinha_entrega'

    id = db.Column(db.Integer, primary_key=True)
    pedido_code = db.Column(db.String(50), nullable=False, unique=True)
    texto = db.Column(db.Text)
    atualizado_em = db.Column(db.DateTime, default=agora)
    atualizado_por = db.Column(db.Integer, db.ForeignKey('usuario.id'))

    autor = db.relationship('Usuario', backref='cartinhas')

class OverrideEntrega(db.Model):
    """Sobrescreve a data de entrega de um pedido VNDA — local, nao sincroniza com o VNDA."""
    __tablename__ = 'override_entrega'

    id = db.Column(db.Integer, primary_key=True)
    pedido_code = db.Column(db.String(50), nullable=False, unique=True, index=True)
    data_entrega = db.Column(db.Date, nullable=False)
    motivo = db.Column(db.Text)
    atualizado_em = db.Column(db.DateTime, default=agora)
    atualizado_por = db.Column(db.Integer, db.ForeignKey('usuario.id'))

    autor = db.relationship('Usuario', backref='overrides_entrega')

class GeocodeCache(db.Model):
    """Cache de enderecos geocodificados (CEP -> lat/lng).
    Evita re-bater o Nominatim (rate limit 1 req/s)."""
    __tablename__ = 'geocode_cache'

    id = db.Column(db.Integer, primary_key=True)
    chave = db.Column(db.String(200), nullable=False, unique=True, index=True)
    lat = db.Column(db.Float)
    lng = db.Column(db.Float)
    fonte = db.Column(db.String(50))  # 'brasilapi', 'awesomeapi', 'nominatim', 'nominatim_cep_rejeitado', etc.
    criado_em = db.Column(db.DateTime, default=agora)

class Driver(db.Model):
    """Motorista/motoboy cadastrado. Pedidos sao atribuidos a um Driver."""
    __tablename__ = 'driver_entrega'

    id = db.Column(db.Integer, primary_key=True)
    nome = db.Column(db.String(80), nullable=False, unique=True)
    cor = db.Column(db.String(20))  # opcional: hex pra UI
    telefone = db.Column(db.String(30))
    ativo = db.Column(db.Boolean, default=True)
    criado_em = db.Column(db.DateTime, default=agora)
    # Capacidade maxima de pedidos por rodada de Auto-distribuir.
    # Usada pra moto (cap 2-3) vs carro (cap 12-15). Default alto = sem limite efetivo.
    capacidade = db.Column(db.Integer, default=999)

    # Acesso a pagina /driver/<token> + PIN 4 digitos pra dificultar acesso casual.
    token = db.Column(db.String(32), unique=True, index=True)
    pin = db.Column(db.String(8))  # 4 digitos, mas folga pra futuros 6

    atribuicoes = db.relationship('AtribuicaoEntrega', backref='driver', lazy='dynamic')


class DriverMagicToken(db.Model):
    """Magic link diario do motorista. Cron 05:00 BRT gera token novo
    pra cada Driver ativo + envia via WhatsApp. Velhos viram revogados.

    Acesso: /driver/<token> aceita tanto Driver.token legado quanto
    qualquer DriverMagicToken nao-revogado e nao-expirado.
    """
    __tablename__ = 'driver_magic_token'

    id = db.Column(db.Integer, primary_key=True)
    driver_id = db.Column(db.Integer, db.ForeignKey('driver_entrega.id'),
                           nullable=False, index=True)
    token = db.Column(db.String(64), nullable=False, unique=True, index=True)
    criado_em = db.Column(db.DateTime, default=agora, nullable=False)
    expira_em = db.Column(db.DateTime, nullable=False)
    revogado = db.Column(db.Boolean, default=False, nullable=False)
    enviado_em = db.Column(db.DateTime, nullable=True)
    enviado_ok = db.Column(db.Boolean, nullable=True)

    driver = db.relationship('Driver', backref='magic_tokens')

    @property
    def valido(self):
        return (not self.revogado) and (self.expira_em > agora())


class LoteSaida(db.Model):
    """Pacote nomeado de uma rodada de distribuicao.
    Cada vez que o usuario clica 'Distribuir' (ou cria manualmente), gera 1 lote.
    Status e inferido a partir das atribuicoes filhas."""
    __tablename__ = 'lote_saida'

    id = db.Column(db.Integer, primary_key=True)
    nome = db.Column(db.String(120), nullable=False)
    data_entrega = db.Column(db.Date, nullable=False, index=True)
    criado_em = db.Column(db.DateTime, default=agora)
    janelas_json = db.Column(db.Text)  # JSON array de strings, ex: ["07-08","08-09"]
    # aberto = nenhum saiu | em_rota = >=1 saiu, falta entregar | concluido = 100%
    status = db.Column(db.String(20), default='aberto', index=True)
    criado_por = db.Column(db.Integer, db.ForeignKey('usuario.id'))

class AtribuicaoEntrega(db.Model):
    """Vincula um pedido VNDA a um Driver. Pedido tem no maximo 1 driver por vez."""
    __tablename__ = 'atribuicao_entrega'

    id = db.Column(db.Integer, primary_key=True)
    pedido_code = db.Column(db.String(50), nullable=False, unique=True, index=True)
    driver_id = db.Column(db.Integer, db.ForeignKey('driver_entrega.id'))
    lote_id = db.Column(db.Integer, db.ForeignKey('lote_saida.id'), index=True)
    data_entrega = db.Column(db.Date, index=True)
    ordem = db.Column(db.Integer, default=0)  # ordem dentro da rota do driver
    atualizado_em = db.Column(db.DateTime, default=agora, onupdate=agora)
    atualizado_por = db.Column(db.Integer, db.ForeignKey('usuario.id'))

    # Status preenchido pela pagina do driver
    status = db.Column(db.String(20), default='pendente')  # pendente|entregue|nao_entregue
    entregue_em = db.Column(db.DateTime)
    nota = db.Column(db.String(500))
    motivo_falha = db.Column(db.String(50))  # ausente|recusou|endereco_errado|outro
    geo_lat = db.Column(db.Float)
    geo_lng = db.Column(db.Float)
    # Hash publico pra link compartilhavel com cliente
    proof_hash = db.Column(db.String(32), unique=True, index=True)

    autor = db.relationship('Usuario')
    fotos = db.relationship('EntregaFoto', backref='atribuicao', lazy='dynamic',
                            cascade='all, delete-orphan')

class EntregaFoto(db.Model):
    """Foto de comprovante de entrega tirada pelo driver."""
    __tablename__ = 'entrega_foto'

    id = db.Column(db.Integer, primary_key=True)
    atribuicao_id = db.Column(db.Integer, db.ForeignKey('atribuicao_entrega.id'), nullable=False, index=True)
    url = db.Column(db.String(500), nullable=False)  # URL publica (Dropbox shared link)
    storage_path = db.Column(db.String(500))  # caminho no storage pra deletar depois
    tirada_em = db.Column(db.DateTime, default=agora)
    tamanho_bytes = db.Column(db.Integer)
