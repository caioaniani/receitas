"""Modelos do dominio: loja.

Faz parte de `app.models` (split em multiplos arquivos por dominio
em 2026-05-21). Importar via `from app.models import X` continua
funcionando porque `app/models/__init__.py` re-exporta tudo.
"""

from app.extensions import db


class Loja(db.Model):
    __tablename__ = 'loja'

    id = db.Column(db.Integer, primary_key=True)
    nome = db.Column(db.String(150), nullable=False, unique=True)
    endereco = db.Column(db.String(300))
    telefone = db.Column(db.String(30))
    ativa = db.Column(db.Boolean, default=True)
    planta_imagem = db.Column(db.LargeBinary)
    planta_mimetype = db.Column(db.String(100))
    # PIN 4-6 digitos usado pelo funcionario da loja pra confirmar
    # recebimento via QR Code (handshake driver → loja).
    pin = db.Column(db.String(8))
    # Dados FISCAIS (20/07/2026): a loja e DESTINATARIA da NF-e de
    # transferencia industria→loja (filial com CNPJ proprio). SEFAZ exige
    # documento + endereco estruturado — mesma regua do ClienteB2B. O campo
    # livre `endereco` acima segue como fallback humano (RH/escala).
    cnpj = db.Column(db.String(20))
    inscricao_estadual = db.Column(db.String(20))
    endereco_logradouro = db.Column(db.String(200))
    endereco_numero = db.Column(db.String(20))
    endereco_complemento = db.Column(db.String(100))
    endereco_bairro = db.Column(db.String(100))
    endereco_cep = db.Column(db.String(9))
    endereco_cidade = db.Column(db.String(100))
    endereco_uf = db.Column(db.String(2))
    # Razao social LEGAL da filial (20/07/2026, pedido do dono: "a razao
    # social e diferente de como eu chamo ela no sistema"). `nome` segue
    # sendo o apelido interno de toda a operacao; a NF de transferencia usa
    # a razao social quando preenchida (fallback: nome). ALTER ja em prod.
    razao_social = db.Column(db.String(200))
    # Dispensa de NF de transferencia POR LOJA (20/07/2026, dono): loja
    # marcada nunca emite NF no scan do QR. Decisao do ADMIN no /rh/lojas
    # — motorista/padeiro nao veem a opcao.
    nf_dispensada = db.Column(db.Boolean, nullable=False, default=False)

    @property
    def fiscal_completo(self):
        """MESMA régua da emissão da NF de transferência
        (tiny_nf_transf._payload_cliente_loja): CNPJ com 14 dígitos +
        endereço estruturado completo. O badge do RH usa isto — badge
        verde com emissão recusando era exatamente o que ele existia
        pra prevenir (achado A5 da revisão 20/07/2026)."""
        doc = ''.join(c for c in (self.cnpj or '') if c.isdigit())
        if len(doc) != 14:
            return False
        return all((v or '').strip() for v in (
            self.endereco_logradouro, self.endereco_numero,
            self.endereco_bairro, self.endereco_cep,
            self.endereco_cidade, self.endereco_uf))

    def __repr__(self):
        return f'<Loja {self.nome}>'


class PrecoLojaReceita(db.Model):
    __tablename__ = 'preco_loja_receita'

    id = db.Column(db.Integer, primary_key=True)
    loja_id = db.Column(db.Integer, db.ForeignKey('loja.id'), nullable=False)
    receita_id = db.Column(db.Integer, db.ForeignKey('receita.id'), nullable=False)
    preco = db.Column(db.Float, nullable=False)

    __table_args__ = (db.UniqueConstraint('loja_id', 'receita_id', name='uq_preco_loja_receita'),)
