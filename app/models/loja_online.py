"""Modelos do dominio: loja online (e-commerce proprio).

Criado na Fase 3 do projeto "loja propria" (substituir o VNDA). Faz parte
de `app.models` (split por dominio); importar via `from app.models import X`
continua funcionando porque `app/models/__init__.py` re-exporta tudo.

NAO confundir com:
- `Loja` / `PedidoLoja` (app/models/loja.py, pedidos.py): loja FISICA e
  pedido B2B/reposicao interna.
- `PedidoSite` (cache read-only do VNDA): sera aposentado no cutover.

Aqui vivem os modelos do checkout NATIVO do site:
- `Cliente`: consumidor final (PII — LGPD). Guest checkout reusa por email.
- `EnderecoCliente`: enderecos salvos do cliente logado (Fase 6).
- `PedidoOnline` + `PedidoOnlineItem`: pedido nascido no nosso site.

Dinheiro SEMPRE em `Numeric(10, 2)` + `Decimal` (peso especial, CLAUDE.md).
Tabelas novas sao criadas por `db.create_all()` no startup (mesmo padrao do
`ContaPagar`); nao precisa de DDL no `migrations_legacy.py`.
"""
import secrets

from werkzeug.security import check_password_hash, generate_password_hash

from app.extensions import db
from app.utils import agora


def _gerar_codigo_pedido():
    """Codigo curto, legivel e unico-na-pratica pro pedido (ex: 'A7K3F9D2').

    8 chars hex maiusculo. Espelha o formato dos codigos do VNDA (ex:
    'D9065562E2') pra nao estranhar na virada. Colisao e improvavel
    (16^8 = 4 bi); a coluna tem unique constraint como rede de seguranca.
    """
    return secrets.token_hex(4).upper()


class Cliente(db.Model):
    """Consumidor final do site (PII — LGPD).

    Guest checkout: criado/reusado por email mesmo sem senha (senha_hash
    NULL). Conta de verdade (login self-serve) vem na Fase 6 — ai o cliente
    define senha e `senha_hash` deixa de ser NULL.
    """
    __tablename__ = 'cliente'

    id = db.Column(db.Integer, primary_key=True)
    nome = db.Column(db.String(150), nullable=False)
    # Email = identidade do cliente. Unique pra guest reusar a mesma linha
    # (CRM + "meus pedidos" no futuro). Indexado pra lookup no checkout.
    email = db.Column(db.String(200), nullable=False, unique=True, index=True)
    telefone = db.Column(db.String(30))
    # CPF: opcional agora, necessario pra NF (Fase 5). So digitos.
    cpf = db.Column(db.String(14))
    # NULL ate o cliente criar conta (Fase 6). Guest nunca tem senha.
    senha_hash = db.Column(db.String(256), nullable=True)
    # Consentimento LGPD: timestamp do aceite no checkout. NULL = sem aceite
    # registrado (pedidos antigos/migrados).
    aceite_lgpd_em = db.Column(db.DateTime, nullable=True)
    ativo = db.Column(db.Boolean, default=True, nullable=False)
    criado_em = db.Column(db.DateTime, default=agora)

    enderecos = db.relationship(
        'EnderecoCliente', backref='cliente',
        cascade='all, delete-orphan', lazy='dynamic')
    pedidos = db.relationship(
        'PedidoOnline', backref='cliente', lazy='dynamic')

    def set_senha(self, senha):
        self.senha_hash = generate_password_hash(senha, method='scrypt')

    def check_senha(self, senha):
        if not self.senha_hash:
            return False
        return check_password_hash(self.senha_hash, senha)

    @property
    def tem_conta(self):
        """True se o cliente ja virou conta de verdade (tem senha)."""
        return bool(self.senha_hash)

    def __repr__(self):
        return f'<Cliente {self.email}>'


class EnderecoCliente(db.Model):
    """Endereco salvo de um cliente logado (Fase 6).

    Guest checkout NAO usa esta tabela — grava o endereco denormalizado
    direto no `PedidoOnline` (snapshot do que foi entregue). Esta tabela e
    pra cliente recorrente reusar enderecos sem redigitar.
    """
    __tablename__ = 'endereco_cliente'

    id = db.Column(db.Integer, primary_key=True)
    cliente_id = db.Column(
        db.Integer, db.ForeignKey('cliente.id'), nullable=False, index=True)
    apelido = db.Column(db.String(50))  # "Casa", "Trabalho"
    cep = db.Column(db.String(9))
    logradouro = db.Column(db.String(200))
    numero = db.Column(db.String(20))
    complemento = db.Column(db.String(100))
    bairro = db.Column(db.String(100))
    cidade = db.Column(db.String(100))
    uf = db.Column(db.String(2))
    # Cache do geocode (frete.py) pra nao re-geocodificar a cada checkout.
    lat = db.Column(db.Float)
    lng = db.Column(db.Float)
    principal = db.Column(db.Boolean, default=False, nullable=False)
    criado_em = db.Column(db.DateTime, default=agora)

    def linha_unica(self):
        """Endereco em uma linha pra exibir/snapshot no pedido."""
        partes = [self.logradouro, self.numero, self.complemento,
                  self.bairro, self.cidade, self.uf]
        return ', '.join(p for p in partes if p)

    def __repr__(self):
        return f'<EnderecoCliente {self.id} cli={self.cliente_id}>'


# Modos de entrega aceitos no checkout. String no banco (nao Enum) pra
# evitar migration de tipo; validado na camada de servico/rota.
MODOS_ENTREGA = ('agendada', 'retirada', 'express')

# Status do pedido online. Comeca em 'aguardando_pagamento' (Fase 3 ainda
# nao cobra; Fase 4 liga o Pagar.me). 'pago' dispara baixa de estoque +
# NF. 'cancelado' dispara estorno. Strings, nao Enum (mesmo motivo acima).
STATUS_PEDIDO_ONLINE = (
    'aguardando_pagamento', 'pago', 'em_preparo', 'a_caminho',
    'entregue', 'cancelado',
)


class PedidoOnline(db.Model):
    """Pedido nascido no nosso site (checkout nativo).

    Fase 3: criado com status 'aguardando_pagamento'. NAO baixa estoque
    aqui — a baixa (`MovEstoqueLoja('venda_site')`) so acontece quando o
    webhook do Pagar.me confirmar 'pago' (Fase 4), nunca no retorno do
    checkout (CLAUDE.md: dinheiro tem peso especial).

    Dados do cliente sao DENORMALIZADOS (nome/email/telefone/endereco) pra o
    pedido virar um snapshot fiel do que foi pedido, independente de edicao
    posterior no cadastro do cliente.
    """
    __tablename__ = 'pedido_online'

    id = db.Column(db.Integer, primary_key=True)
    codigo = db.Column(
        db.String(12), unique=True, index=True, default=_gerar_codigo_pedido)

    # Cliente: FK opcional (guest tem Cliente criado por email, mas pode ser
    # NULL em cenarios de import). Os campos denormalizados abaixo sao a
    # fonte de verdade do pedido.
    cliente_id = db.Column(
        db.Integer, db.ForeignKey('cliente.id'), nullable=True, index=True)
    nome_cliente = db.Column(db.String(150), nullable=False)
    email_cliente = db.Column(db.String(200), nullable=False)
    telefone_cliente = db.Column(db.String(30))

    # ── Destinatario (quando difere do pagador — presente) ──────────
    # NULL = entrega/retirada vai pro proprio cliente. Quando preenchidos,
    # estes prevalecem na entrega; o pagador (acima) continua sendo quem
    # paga e recebe contato comercial.
    nome_destinatario = db.Column(db.String(150))
    telefone_destinatario = db.Column(db.String(30))

    # ── Entrega ──────────────────────────────────────────────────────
    modo_entrega = db.Column(db.String(20), nullable=False)  # MODOS_ENTREGA
    # Retirada: loja escolhida pelo cliente. NULL nos modos de entrega.
    loja_retirada_id = db.Column(
        db.Integer, db.ForeignKey('loja.id'), nullable=True)
    # Entrega (agendada/express): endereco denormalizado (snapshot).
    endereco_entrega = db.Column(db.Text)
    endereco_cep = db.Column(db.String(9))
    # Distancia calculada pelo frete.py (km). Auditoria do valor cobrado.
    distancia_km = db.Column(db.Float)
    # Data + janela escolhidas. Retirada/express tambem usam (hora marcada).
    data_entrega = db.Column(db.Date, index=True)
    janela_entrega = db.Column(db.String(40))  # "8h-12h", "imediato", etc.

    # ── Dinheiro (Numeric(10,2) + Decimal sempre) ────────────────────
    subtotal = db.Column(db.Numeric(10, 2), nullable=False, default=0)
    frete_valor = db.Column(db.Numeric(10, 2), nullable=False, default=0)
    valor_total = db.Column(db.Numeric(10, 2), nullable=False, default=0)

    cartinha = db.Column(db.Text)  # recado/presente
    status = db.Column(
        db.String(30), nullable=False, default='aguardando_pagamento',
        index=True)

    criado_em = db.Column(db.DateTime, default=agora, index=True)
    atualizado_em = db.Column(db.DateTime, default=agora, onupdate=agora)
    pago_em = db.Column(db.DateTime, nullable=True)
    cancelado_em = db.Column(db.DateTime, nullable=True)

    itens = db.relationship(
        'PedidoOnlineItem', backref='pedido',
        cascade='all, delete-orphan', lazy='select')
    loja_retirada = db.relationship('Loja', foreign_keys=[loja_retirada_id])

    def recalcular_total(self):
        """Soma itens -> subtotal; subtotal + frete -> valor_total.

        Tudo em Decimal (precisao exata em centavos). Chamar apos
        adicionar/remover itens ou setar o frete.
        """
        from decimal import Decimal
        sub = sum((Decimal(str(i.subtotal or 0)) for i in self.itens),
                  Decimal('0'))
        self.subtotal = sub
        self.valor_total = sub + Decimal(str(self.frete_valor or 0))
        return self.valor_total

    def __repr__(self):
        return f'<PedidoOnline {self.codigo} {self.status}>'


class PedidoOnlineItem(db.Model):
    """Item de um PedidoOnline.

    Nome e preco sao SNAPSHOT no momento do pedido (preco_site pode mudar
    depois; o pedido tem que refletir o que foi cobrado). FK pra
    receita/produto mantida pra a baixa de estoque (Fase 4) saber o que
    debitar do EstoqueLoja.
    """
    __tablename__ = 'pedido_online_item'

    id = db.Column(db.Integer, primary_key=True)
    pedido_id = db.Column(
        db.Integer, db.ForeignKey('pedido_online.id'),
        nullable=False, index=True)
    # 'receita' | 'produto' — qual catalogo o item veio.
    kind = db.Column(db.String(10), nullable=False)
    receita_id = db.Column(
        db.Integer, db.ForeignKey('receita.id'), nullable=True)
    produto_id = db.Column(
        db.Integer, db.ForeignKey('produto.id'), nullable=True)

    nome = db.Column(db.String(200), nullable=False)  # snapshot
    preco_unitario = db.Column(db.Numeric(10, 2), nullable=False)  # snapshot
    quantidade = db.Column(db.Integer, nullable=False, default=1)
    subtotal = db.Column(db.Numeric(10, 2), nullable=False, default=0)

    receita = db.relationship('Receita', foreign_keys=[receita_id])
    produto = db.relationship('Produto', foreign_keys=[produto_id])

    def __repr__(self):
        return f'<PedidoOnlineItem {self.nome} x{self.quantidade}>'


# Status do pagamento (espelha o ciclo do Pagar.me, simplificado).
STATUS_PAGAMENTO = ('pendente', 'pago', 'falhou', 'estornado')


class PagamentoOnline(db.Model):
    """Tentativa de pagamento de um PedidoOnline via Pagar.me (Fase 4).

    Dinheiro em Numeric(10,2). Um pedido pode ter várias tentativas (Pix que
    expirou + cartão, etc.) — por isso é 1 pedido : N pagamentos. A
    confirmação de 'pago' SEMPRE vem do webhook (nunca do retorno do
    checkout) — CLAUDE.md, dinheiro tem peso especial.
    """
    __tablename__ = 'pagamento_online'

    id = db.Column(db.Integer, primary_key=True)
    pedido_id = db.Column(
        db.Integer, db.ForeignKey('pedido_online.id'),
        nullable=False, index=True)
    metodo = db.Column(db.String(10), nullable=False)  # 'pix' | 'cartao'
    valor = db.Column(db.Numeric(10, 2), nullable=False)
    status = db.Column(
        db.String(20), nullable=False, default='pendente', index=True)

    # Identificadores do Pagar.me (auditoria + reconciliação no webhook).
    pagarme_order_id = db.Column(db.String(60), index=True)
    pagarme_charge_id = db.Column(db.String(60), index=True)

    # Pix: payload pra exibir/copiar. Cartão não guarda NADA do cartão (PCI:
    # a tokenização é no front com a public key; o servidor nunca vê o número).
    pix_qr_code = db.Column(db.Text)        # EMV copia-e-cola
    pix_qr_code_url = db.Column(db.Text)    # imagem do QR (se vier)
    pix_expira_em = db.Column(db.DateTime)

    erro = db.Column(db.Text)  # última mensagem de erro (falha)
    criado_em = db.Column(db.DateTime, default=agora, index=True)
    atualizado_em = db.Column(db.DateTime, default=agora, onupdate=agora)
    pago_em = db.Column(db.DateTime)

    pedido = db.relationship('PedidoOnline', backref='pagamentos')

    def __repr__(self):
        return f'<PagamentoOnline {self.metodo} {self.status} ped={self.pedido_id}>'


class PagarmeEvento(db.Model):
    """Idempotência do webhook do Pagar.me.

    Cada evento do webhook tem um id único; gravamos antes de processar pra
    o MESMO evento (reentrega do Pagar.me) NÃO baixar estoque/marcar pago
    duas vezes. Mesmo padrão de SeruPedidoProcessado / SlackEventoProcessado.
    """
    __tablename__ = 'pagarme_evento'

    id = db.Column(db.Integer, primary_key=True)
    evento_id = db.Column(db.String(80), unique=True, nullable=False, index=True)
    tipo = db.Column(db.String(60))  # 'order.paid', 'charge.paid', ...
    recebido_em = db.Column(db.DateTime, default=agora)

    def __repr__(self):
        return f'<PagarmeEvento {self.tipo} {self.evento_id}>'
