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
    # Aniversário (portal Wi-Fi 11/07/2026): dia/mês pra campanha; ANO
    # opcional (LGPD — minimização). ALTER aplicado em prod (76b0a043)
    # ANTES deste modelo — procedimento de 2 commits.
    aniversario_dia = db.Column(db.Integer, nullable=True)
    aniversario_mes = db.Column(db.Integer, nullable=True)
    nascimento_ano = db.Column(db.Integer, nullable=True)
    # E-mail marketing (05/08/2026): regime OPT-OUT por decisão do dono — a
    # base inteira (site + Wi-Fi das lojas) recebe campanha, e quem clica em
    # "cancelar inscrição" no e-mail é marcado AQUI. NULL = recebe.
    # `app/services/marketing.py` puxa os descadastros do Listmonk ANTES de
    # cada envio: sem isso a sincronização re-inscreveria quem acabou de
    # sair. ALTER aplicado em prod (e777e0d0) e CONFIRMADO pela sonda
    # /api/claude/deploy?colunas= ANTES deste modelo — procedimento de 2
    # commits.
    marketing_descadastro_em = db.Column(db.DateTime, nullable=True)
    # De ONDE veio o cadastro: 'site' | 'wifi' | 'balcao' | NULL (antigo).
    # Existe porque derivar isso de outra tabela deu errado: o portal Wi-Fi
    # no modo RADIUS (13/07/2026) cria só o `Cliente`, sem `WifiPortalSessao`
    # — a lista de marketing do Wi-Fi enxergava 1 pessoa em vez de dezenas.
    # ALTER aplicado em prod (2768944d) e CONFIRMADO pela sonda
    # /api/claude/deploy?colunas= ANTES deste modelo — procedimento de 2
    # commits.
    origem = db.Column(db.String(20), nullable=True)
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


# Motivos de cancelamento (codigos persistidos em
# PedidoOnline.motivo_cancelamento) -> rotulo legivel pra UI.
MOTIVOS_CANCELAMENTO = {
    'pix_expirado': 'Pix não pago (reserva expirou)',
    'reembolso': 'Reembolsado pelo admin',
    'cancelado_admin': 'Cancelado manualmente (admin)',
}


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
    # Endereco ESTRUTURADO (snapshot) — a NF-e exige logradouro/numero/bairro/
    # cidade/uf SEPARADOS; mandar so a linha unica acima fazia a SEFAZ
    # rejeitar ("endereco/bairro/cidade em branco"). Preenchido no checkout
    # de entrega; NULL na retirada (que nao coleta endereco do cliente).
    endereco_logradouro = db.Column(db.String(200))
    endereco_numero = db.Column(db.String(20))
    endereco_complemento = db.Column(db.String(100))
    endereco_bairro = db.Column(db.String(100))
    endereco_cidade = db.Column(db.String(100))
    endereco_uf = db.Column(db.String(2))
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
    # Por que foi cancelado (25/06/2026): 'pix_expirado' | 'reembolso' |
    # 'cancelado_admin'. NULL = nunca cancelado OU cancelado antes desta coluna
    # existir — nesse caso `motivo_cancelamento_label` infere pelos timestamps.
    motivo_cancelamento = db.Column(db.String(40), nullable=True)
    # Reserva de estoque (21/06/2026): a partir do checkout,
    # `EstoqueLoja.quantidade_reservada` segura o saldo. `reserva_expira_em`
    # marca quando o cron deve liberar caso o cliente nunca pague (Pix vence
    # em 30min — a reserva fica 35min, margem de 5min pro webhook chegar).
    # NULL = pedido nunca reservou (legado pre-cutover ou pedido cancelado).
    reserva_expira_em = db.Column(db.DateTime, nullable=True, index=True)
    # client_id do GA4 (cookie `_ga`) capturado no POST do checkout — amarra
    # o purchase server-side (analytics_server.py) à sessão real do cliente
    # pro GA4 deduplicar com o evento do navegador. NULL = cliente sem GA
    # (recusou cookies / bloqueador). ALTER já em prod (13/07/2026).
    ga_client_id = db.Column(db.String(64), nullable=True)

    # Divulgacao (21/07/2026, pedido do dono): pedido "como do site" mas SEM
    # pagamento (brinde/PR). Aparece no painel de entregas com estrela ⭐ e
    # baixa estoque de VERDADE (o pao sai pela porta), porem com movimento
    # MARCADO (canal 'divulgacao' no baixa_venda) que fica FORA da previsao de
    # venda. `pago_em` continua NULL (nunca foi pago) — ja sai das somas de
    # faturamento do site (filtradas por pago_em). FALSE = pedido normal.
    divulgacao = db.Column(db.Boolean, nullable=False, default=False)

    # NF-e (Fase 5, via Tiny). Setados quando o admin clica "Emitir NF"
    # — o Tiny aplica NCM/CFOP/CST do cadastro do produto.
    tiny_pedido_id = db.Column(db.String(40), nullable=True)
    tiny_nota_fiscal_id = db.Column(db.String(40), nullable=True, index=True)
    nf_status = db.Column(db.String(40), nullable=True)
    nf_emitida_em = db.Column(db.DateTime, nullable=True)

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

    @property
    def motivo_cancelamento_label(self):
        """Rotulo legivel do motivo do cancelamento. Para pedidos cancelados
        ANTES desta coluna existir (motivo NULL), infere pelos timestamps — era
        o que se fazia na mao (Pix nao pago vs cancelado pos-pagamento)."""
        if self.status != 'cancelado':
            return None
        if self.motivo_cancelamento:
            return MOTIVOS_CANCELAMENTO.get(
                self.motivo_cancelamento, self.motivo_cancelamento)
        if self.pago_em is None:
            return 'Pix não pago (inferido)'
        return 'Cancelado após pagamento (inferido)'

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
    # "Fatiado?" — preferencia de corte escolhida pelo cliente, so em pao
    # sourdough (16/07/2026). NAO mexe em preco nem estoque (mesmo SKU, so
    # cortado). NULL/False = inteiro; True = fatiado. Fatiado e inteiro do
    # MESMO sourdough sao linhas separadas no carrinho/pedido.
    fatiado = db.Column(db.Boolean, nullable=True)

    receita = db.relationship('Receita', foreign_keys=[receita_id])
    produto = db.relationship('Produto', foreign_keys=[produto_id])

    componentes = db.relationship(
        'PedidoOnlineItemComponente', backref='item',
        cascade='all, delete-orphan', lazy='selectin')

    def __repr__(self):
        return f'<PedidoOnlineItem {self.nome} x{self.quantidade}>'


class PedidoOnlineItemComponente(db.Model):
    """Composição ESCOLHIDA pelo cliente num menu configurável (26/07/2026).

    Só existe pra item de menu (`Produto.menu_configuravel`) — item comum e
    cesta de composição fixa não têm linhas aqui e continuam sendo expandidos
    pelo cadastro (`cestas.componentes_de_cesta`).

    Por que persistir: o cadastro guarda a PRÉ-SELEÇÃO, não a escolha. Sem
    esta tabela, a baixa de estoque, a produção e a impressão do motorista
    leriam a cesta padrão e entregariam/debitariam a composição ERRADA
    (estoque tem peso especial — CLAUDE.md). Quem manda na baixa é esta
    linha; o cadastro vira só o menu de opções.

    `quantidade` é por UMA unidade do menu (2 menus x 7 minis = 14 minis) —
    a mesma semântica de `ProdutoItem.quantidade`. `preco_unitario` é
    snapshot do `ProdutoItem.preco_menu` no momento da compra (o admin pode
    reajustar depois; o pedido tem que refletir o que foi cobrado).
    """
    __tablename__ = 'pedido_online_item_componente'

    id = db.Column(db.Integer, primary_key=True)
    item_id = db.Column(
        db.Integer, db.ForeignKey('pedido_online_item.id'),
        nullable=False, index=True)
    # Slot de origem no cadastro do menu. Integer PURO, SEM ForeignKey de
    # propósito: `produtos.salvar_composicao` APAGA e RECRIA todos os
    # `ProdutoItem` da cesta a cada salvamento — uma FK real bloquearia o
    # admin de editar o menu assim que existisse um pedido (ou deixaria a
    # linha órfã). Aqui o campo é só rastreabilidade; quem manda na baixa de
    # estoque são as FKs de alvo abaixo, que são estáveis.
    produto_item_id = db.Column(db.Integer, nullable=True)
    # 'receita' | 'produto' | 'mp' — espelha ProdutoItem.tipo.
    tipo = db.Column(db.String(10), nullable=False)
    receita_id = db.Column(
        db.Integer, db.ForeignKey('receita.id'), nullable=True)
    produto_componente_id = db.Column(
        db.Integer, db.ForeignKey('produto.id'), nullable=True)
    materia_prima_id = db.Column(
        db.Integer, db.ForeignKey('materia_prima.id'), nullable=True)

    nome = db.Column(db.String(200), nullable=False)          # snapshot
    quantidade = db.Column(db.Integer, nullable=False, default=0)
    preco_unitario = db.Column(db.Numeric(10, 2), nullable=True)  # snapshot

    @property
    def coluna_estoque(self):
        """'receita_id' | 'produto_id' | 'materia_prima_id' — pronto pra
        filtrar EstoqueLoja, igual `cestas.componentes_de_cesta`."""
        if self.receita_id:
            return 'receita_id'
        if self.produto_componente_id:
            return 'produto_id'
        if self.materia_prima_id:
            return 'materia_prima_id'
        return None

    @property
    def alvo_id(self):
        return (self.receita_id or self.produto_componente_id
                or self.materia_prima_id)

    def __repr__(self):
        return f'<PedidoOnlineItemComponente {self.nome} x{self.quantidade}>'


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


class ClienteResetSenha(db.Model):
    """Token de recuperação de senha do cliente (Fase 6 — PR 3).

    Single-use + expira em 1h. Quando o cliente pede 'esqueci a senha', a
    gente cria um registro com token aleatório (`secrets.token_urlsafe`) e
    manda o link por e-mail. Cliente clica → valida → troca a senha → marca
    `usado_em`. Token usado/expirado não vale mais.

    NÃO confundir com `UsuarioResetSenha` do admin (se vier). Cliente final
    e staff são autenticações separadas; cada uma tem sua tabela de reset.
    """
    __tablename__ = 'cliente_reset_senha'

    id = db.Column(db.Integer, primary_key=True)
    cliente_id = db.Column(
        db.Integer, db.ForeignKey('cliente.id'),
        nullable=False, index=True)
    token = db.Column(db.String(80), unique=True, nullable=False, index=True)
    criado_em = db.Column(db.DateTime, default=agora, nullable=False)
    expira_em = db.Column(db.DateTime, nullable=False)
    usado_em = db.Column(db.DateTime, nullable=True)

    cliente = db.relationship('Cliente')

    def valido(self, agora_dt):
        return self.usado_em is None and self.expira_em > agora_dt


class ClienteVerificacaoEmail(db.Model):
    """Verificação de e-mail no cadastro — DISPARA SÓ quando o cadastro
    reivindicaria um pedido feito anteriormente como guest (decisão de
    19/06/2026: e-mail novo continua com cadastro instantâneo; e-mail que
    já tem `Cliente` guest exige confirmação por e-mail antes de virar conta
    completa, pra fechar o sequestro de pedido por email-typing).

    Pending data fica AQUI (nome_pending + senha_hash_pending) — não no
    Cliente — pra cadastro não-confirmado NÃO virar conta utilizável.
    Quando o cliente clica no link do e-mail, a gente promove os dados
    pendentes pro Cliente. Token expira em 1h, single-use."""
    __tablename__ = 'cliente_verificacao_email'

    id = db.Column(db.Integer, primary_key=True)
    cliente_id = db.Column(
        db.Integer, db.ForeignKey('cliente.id'),
        nullable=False, index=True)
    token = db.Column(db.String(80), unique=True, nullable=False, index=True)
    nome_pending = db.Column(db.String(120), nullable=True)
    telefone_pending = db.Column(db.String(30), nullable=True)
    senha_hash_pending = db.Column(db.String(255), nullable=False)
    criado_em = db.Column(db.DateTime, default=agora, nullable=False)
    expira_em = db.Column(db.DateTime, nullable=False)
    usado_em = db.Column(db.DateTime, nullable=True)

    cliente = db.relationship('Cliente')

    def valido(self, agora_dt):
        return self.usado_em is None and self.expira_em > agora_dt


class CategoriaSite(db.Model):
    """Ordenação das categorias na vitrine (Fase 6.5 — 17/06/2026).

    `categoria` (texto livre em Produto/Receita) é só uma string — sem
    primary key própria. Pra ordenar na vitrine, mantemos uma tabela
    com o NOME da categoria + a posição.

    Categoria detectada no catálogo mas SEM linha aqui = vai pro fim
    (em ordem alfabética). Linha aqui sem categoria correspondente no
    catálogo é ignorada na vitrine (não causa erro)."""
    __tablename__ = 'categoria_site'

    id = db.Column(db.Integer, primary_key=True)
    # Único (case-sensitive) — combinamos com `Produto.categoria` /
    # `Receita.categoria` por texto exato.
    nome = db.Column(db.String(50), unique=True, nullable=False, index=True)
    ordem = db.Column(db.Integer, nullable=False, default=0)
    criado_em = db.Column(db.DateTime, default=agora)

    def __repr__(self):
        return f'<CategoriaSite {self.nome!r} ord={self.ordem}>'


class EstoqueSitePlano(db.Model):
    """Plano de estoque do site por DATA DE ENTREGA (22/06/2026, decisao do
    dono).

    Cada linha = quantos itens X esto disponiveis pra entregar no dia Y.
    Cliente que escolhe data Y no checkout so consegue comprar produtos com
    saldo nessa data (qtd_planejada - qtd_reservada > 0).

    Diferente de `EstoqueLoja`:
    - EstoqueLoja = estoque FISICO da loja (foi produzido / esta na prateleira).
      Continua sendo a fonte de verdade pra movimentacoes fisicas (separacao,
      entrega).
    - EstoqueSitePlano = quantos VOU TER pra entregar em cada dia. Pode planejar
      no futuro (sexta vou ter 20 foccacia) sem precisar produzir agora. So
      controla DISPONIBILIDADE no site; baixa/historico fisico segue em
      EstoqueLoja.

    Reserva acontece quando o pedido eh PAGO (igual EstoqueLoja). Se a gente
    reservasse no checkout (status aguardando_pagamento), 30min de pix nao
    paga seguraria o saldo de outros clientes — entao usa o webhook do Pagar.me
    pra reservar so o que de fato vendeu.
    """
    __tablename__ = 'estoque_site_plano'

    id = db.Column(db.Integer, primary_key=True)
    # 'receita' | 'produto' — bate com PedidoOnlineItem.kind.
    kind = db.Column(db.String(10), nullable=False)
    item_id = db.Column(db.Integer, nullable=False)
    data = db.Column(db.Date, nullable=False, index=True)

    qtd_planejada = db.Column(db.Integer, nullable=False, default=0,
                               server_default='0')
    qtd_reservada = db.Column(db.Integer, nullable=False, default=0,
                               server_default='0')

    criado_em = db.Column(db.DateTime, default=agora)
    atualizado_em = db.Column(db.DateTime, default=agora, onupdate=agora)

    __table_args__ = (
        # (kind, item_id, data) eh unico — uma linha por item por dia.
        db.UniqueConstraint('kind', 'item_id', 'data',
                            name='uq_estoque_site_plano_item_data'),
        # Indice extra pra consulta "qual saldo pra esse dia" (vitrine).
        db.Index('ix_estoque_site_plano_data_kind_item',
                 'data', 'kind', 'item_id'),
    )

    @property
    def saldo(self):
        """Disponivel = planejado - reservado (nunca negativo aqui no display;
        race condition de over-reserva eh travada no service `reservar`)."""
        return max(0, (self.qtd_planejada or 0) - (self.qtd_reservada or 0))

    def __repr__(self):
        return (f'<EstoqueSitePlano {self.kind}:{self.item_id} '
                f'{self.data.isoformat() if self.data else "?"} '
                f'plan={self.qtd_planejada} res={self.qtd_reservada}>')


class EstoqueSiteRegraSemanal(db.Model):
    """Regra recorrente de venda no site para um item.

    ``dias_mask`` usa os bits 0..6 para segunda..domingo. ``qtd_limite``
    nulo significa sem limite nos dias marcados; zero nunca e necessario
    aqui, porque um dia desmarcado ja significa indisponivel.

    A regra semanal substitui os planos diarios legados daquele item. As
    reservas continuam em :class:`EstoqueSitePlano`, preservando as vendas
    ja realizadas. Uma excecao explicita por data tem precedencia sobre ela.
    """
    __tablename__ = 'estoque_site_regra_semanal'

    id = db.Column(db.Integer, primary_key=True)
    kind = db.Column(db.String(10), nullable=False)
    item_id = db.Column(db.Integer, nullable=False)
    dias_mask = db.Column(db.Integer, nullable=False, default=127,
                          server_default='127')
    qtd_limite = db.Column(db.Integer, nullable=True)
    criado_em = db.Column(db.DateTime, default=agora)
    atualizado_em = db.Column(db.DateTime, default=agora, onupdate=agora)

    __table_args__ = (
        db.UniqueConstraint('kind', 'item_id',
                            name='uq_estoque_site_regra_semanal_item'),
        db.Index('ix_estoque_site_regra_semanal_item', 'kind', 'item_id'),
    )

    def permite(self, data):
        return bool((self.dias_mask or 0) & (1 << data.weekday()))


class EstoqueSiteExcecao(db.Model):
    """Excecao pontual que prevalece sobre a regra semanal do item.

    Linha com ``qtd_limite`` nulo libera sem limite; zero bloqueia; valor
    positivo limita a quantidade vendavel naquela data.
    """
    __tablename__ = 'estoque_site_excecao'

    id = db.Column(db.Integer, primary_key=True)
    kind = db.Column(db.String(10), nullable=False)
    item_id = db.Column(db.Integer, nullable=False)
    data = db.Column(db.Date, nullable=False, index=True)
    qtd_limite = db.Column(db.Integer, nullable=True)
    criado_em = db.Column(db.DateTime, default=agora)
    atualizado_em = db.Column(db.DateTime, default=agora, onupdate=agora)

    __table_args__ = (
        db.UniqueConstraint('kind', 'item_id', 'data',
                            name='uq_estoque_site_excecao_item_data'),
        db.Index('ix_estoque_site_excecao_data_kind_item',
                 'data', 'kind', 'item_id'),
    )


class WifiPortalSessao(db.Model):
    """Sessão do PORTAL WI-FI da loja (11/07/2026, Ribeiro do Vale).

    Cada cadastro no Wi-Fi de clientes vira uma linha: dados do formulário
    (nome/e-mail/WhatsApp/senha já HASHEADA/aniversário/aceite LGPD) + os
    parâmetros que o Omada manda no redirect do portal externo (MAC do
    aparelho, MAC do AP, SSID). O cliente valida a posse do WhatsApp
    mandando o código `WIFI-XXXXXX` pro número da padaria; o webhook do
    Chatwoot reconhece o código, resolve a CONTA do site (4 regras em
    `wifi_portal._resolver_conta` — decisão do dono 11/07) e devolve o link
    de login one-time. Senha NUNCA em claro (hash scrypt na entrada).

    Tabela nova via db.create_all (sem ALTER). Poda: sessões velhas são
    varridas em `wifi_portal.criar_sessao` (>30 dias — PII, LGPD)."""
    __tablename__ = 'wifi_portal_sessao'

    id = db.Column(db.Integer, primary_key=True)
    # Token da sessão (URL de status) + código curto que o cliente manda
    # no WhatsApp + token de login one-time (só depois de validado).
    token = db.Column(db.String(80), unique=True, nullable=False, index=True)
    codigo = db.Column(db.String(12), nullable=False, index=True)
    login_token = db.Column(db.String(80), nullable=True, index=True)
    login_usado_em = db.Column(db.DateTime, nullable=True)

    # Params do portal externo do Omada (redirect). Podem vir vazios no
    # teste por link/QR (antes do enforcement no controlador).
    client_mac = db.Column(db.String(20), nullable=True)
    ap_mac = db.Column(db.String(20), nullable=True)
    ssid = db.Column(db.String(50), nullable=True)
    site_omada = db.Column(db.String(50), nullable=True)
    redirect_url = db.Column(db.String(300), nullable=True)

    # Dados do formulário.
    nome = db.Column(db.String(150), nullable=False)
    email = db.Column(db.String(200), nullable=False)
    telefone = db.Column(db.String(30), nullable=False)     # o DIGITADO
    telefone_validado = db.Column(db.String(30), nullable=True)  # o que ENVIOU
    senha_hash = db.Column(db.String(256), nullable=False)
    aniversario_dia = db.Column(db.Integer, nullable=True)
    aniversario_mes = db.Column(db.Integer, nullable=True)
    nascimento_ano = db.Column(db.Integer, nullable=True)
    aceite_lgpd_em = db.Column(db.DateTime, nullable=False)

    # Resolução da conta (preenchidos na validação do WhatsApp).
    validado_em = db.Column(db.DateTime, nullable=True)
    cliente_id = db.Column(db.Integer, db.ForeignKey('cliente.id'),
                           nullable=True)
    # 'conta_criada' | 'login_direto' | 'login_conta_telefone' |
    # 'magic_link_email' — ver wifi_portal._resolver_conta.
    resultado = db.Column(db.String(30), nullable=True)

    # Autorização do aparelho no controlador Omada (best-effort; fica
    # pendente enquanto a Open API não estiver configurada).
    wifi_autorizado_em = db.Column(db.DateTime, nullable=True)
    wifi_erro = db.Column(db.String(200), nullable=True)

    criado_em = db.Column(db.DateTime, default=agora, nullable=False)
    expira_em = db.Column(db.DateTime, nullable=False)

    cliente = db.relationship('Cliente')

    def pendente(self, agora_dt):
        return self.validado_em is None and self.expira_em > agora_dt

    def __repr__(self):
        return (f'<WifiPortalSessao {self.codigo} {self.email} '
                f'res={self.resultado}>')


class WifiVoucher(db.Model):
    """Estoque de vouchers do portal Wi-Fi (12/07/2026).

    Trava dura SEM API no OC200: o portal do controlador fica no modo
    Voucher; o dono gera o lote no Hotspot Manager do Omada, exporta e sobe
    em /admin/wifi-vouchers. Cada cadastro VALIDADO no WhatsApp consome UM
    voucher (claim atômico em `wifi_portal.alocar_voucher`) e o código vai
    na resposta — sem cadastro, sem internet. Estoque baixo alerta o dono
    (WhatsApp, dedup 24h). Tabela nova via db.create_all (sem ALTER)."""
    __tablename__ = 'wifi_voucher'

    id = db.Column(db.Integer, primary_key=True)
    codigo = db.Column(db.String(20), unique=True, nullable=False,
                       index=True)
    lote = db.Column(db.String(60), nullable=True)      # nome do arquivo/lote
    criado_em = db.Column(db.DateTime, default=agora, nullable=False)
    usado_em = db.Column(db.DateTime, nullable=True, index=True)
    sessao_id = db.Column(db.Integer,
                          db.ForeignKey('wifi_portal_sessao.id'),
                          nullable=True)

    sessao = db.relationship('WifiPortalSessao')

    def __repr__(self):
        return (f'<WifiVoucher {self.codigo} '
                f'{"usado" if self.usado_em else "livre"}>')


class LojaDataEspecial(db.Model):
    """Data com horario de entrega DIFERENTE do normal (27/07/2026).

    Pedido do dono: no Dia dos Pais (09/08/2026) o site so pode oferecer UMA
    janela de entrega, das 06:00 as 10:00 — bem fora do 08:00-18:00 de todo
    dia. Em vez de cravar essa data no codigo, a data vira CADASTRO: o dono
    resolve Natal, Dia das Maes e qualquer outra sozinho, sem deploy (escolha
    dele em 27/07/2026, via AskUserQuestion).

    Contrato de cada campo:
    - `janelas`: as janelas daquele dia, UMA POR LINHA, no formato
      'HH:MM–HH:MM' (EN-DASH, igual `loja_checkout.JANELAS_HORARIAS`).
      SUBSTITUEM a lista normal — nao somam. Vale pra entrega agendada E pra
      retirada (decisao do dono: a restricao e das duas pontas).
    - VAZIO = **fechado**: o dia some do calendario do site. E um estado
      legitimo (Natal), nao um cadastro pela metade — por isso nao ha
      fallback pras janelas normais quando a lista esta vazia; cair no
      normal transformaria "fechado" em "aberto o dia inteiro".
    - `express_bloqueado`: tira a entrega imediata do ar NAQUELE dia. Sem
      isso, "so uma janela" seria mentira — o cliente pediria express as 15h
      e a padaria teria que sair pra rua fora da leva unica.
    - `rotulo`: pra que serve a data ("Dia dos Pais"), so pra tela/manual.

    Tabela NOVA via `db.create_all` — nao precisa de ALTER nem do
    procedimento de 2 commits (isso vale pra COLUNA nova, nao pra tabela).
    """
    __tablename__ = 'loja_data_especial'

    id = db.Column(db.Integer, primary_key=True)
    data = db.Column(db.Date, unique=True, nullable=False, index=True)
    rotulo = db.Column(db.String(80), nullable=True)
    # Uma janela por linha. Vazio/NULL = fechado (ver docstring).
    janelas = db.Column(db.Text, nullable=True)
    express_bloqueado = db.Column(db.Boolean, default=True, nullable=False)
    # Itens que NAO podem ser vendidos pra ENTREGA neste dia (07/08/2026,
    # caso "Caixa de Mini vendida pro Dia dos Pais" — dono: "os clientes nao
    # poderiam comprar os minis para o dia 9"). Uma REGRA por linha: nome de
    # CATEGORIA (ex.: 'Mini Pães') ou nome de ITEM do catálogo (ex.: 'Caixa
    # de Mini'); comparação sem acento/caixa no serviço. NULL/vazio = sem
    # restrição (todo o catálogo vale, comportamento de sempre). ALTER em
    # migrations_legacy (procedimento de 2 commits, coluna confirmada em
    # prod pela sonda /api/claude/deploy?colunas= antes deste modelo).
    bloquear_itens = db.Column(db.Text, nullable=True)
    criado_em = db.Column(db.DateTime, default=agora, nullable=False)
    criado_por_id = db.Column(db.Integer, db.ForeignKey('usuario.id'),
                              nullable=True)

    criado_por = db.relationship('Usuario')

    def lista_janelas(self):
        """As janelas como lista, sem linhas vazias. [] = dia fechado."""
        return [ln.strip() for ln in (self.janelas or '').splitlines()
                if ln.strip()]

    def lista_bloqueios(self):
        """As regras de bloqueio como lista (texto cru, sem linhas vazias).
        [] = sem restrição de itens."""
        return [ln.strip() for ln in (self.bloquear_itens or '').splitlines()
                if ln.strip()]

    @property
    def fechado(self):
        return not self.lista_janelas()

    def __repr__(self):
        return (f'<LojaDataEspecial {self.data} '
                f'{"FECHADO" if self.fechado else self.lista_janelas()}>')
