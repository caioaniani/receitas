"""Modelos do dominio: integracoes.

Faz parte de `app.models` (split em multiplos arquivos por dominio
em 2026-05-21). Importar via `from app.models import X` continua
funcionando porque `app/models/__init__.py` re-exporta tudo.
"""

from app.extensions import db
from app.utils import agora


class NFLog(db.Model):
    """Audit log de cada solicitacao de NF pelo bot. Obrigatorio pela LGPD —
    em caso de questionamento, a gente precisa provar quem pediu o que e quando.

    NUNCA grava CPF inteiro: so os 4 ultimos digitos pra ajudar na auditoria
    sem armazenar dado sensivel a mais."""
    __tablename__ = 'nf_log'

    id = db.Column(db.Integer, primary_key=True)
    criado_em = db.Column(db.DateTime, default=agora, index=True)
    conv_id = db.Column(db.String(50), index=True)
    canal = db.Column(db.String(20))  # 'whatsapp' | 'instagram' | 'site' | ?
    cpf_4ultimos = db.Column(db.String(4))   # '0099' pra '...000-00'
    numero_pedido = db.Column(db.String(50), index=True)
    resultado = db.Column(db.String(30))   # 'enviada', 'nao_encontrado', 'sem_nf', 'erro', 'handoff'
    detalhe = db.Column(db.Text)


class VigiaVeredito(db.Model):
    """Cada avaliacao do vigia do chatbot (1 por mensagem do cliente).

    Persiste pra o auditor diario (`chatbot_auditor`) achar PADROES — ex:
    'bot empurrou conteudo de cesta pro humano 5x', 'cliente X surtou no
    horario de pico'. Sem isso, o historico vive em memoria e some no deploy."""
    __tablename__ = 'vigia_veredito'

    id = db.Column(db.Integer, primary_key=True)
    criado_em = db.Column(db.DateTime, default=agora, index=True)
    conv_id = db.Column(db.String(50), index=True)  # str pra cobrir 'teste-X'
    cliente = db.Column(db.String(200))
    mensagem_cliente = db.Column(db.Text)
    bot_acao = db.Column(db.String(30))  # 'responder' ou 'handoff'
    bot_motivo = db.Column(db.Text)      # quando handoff: motivo do bot
    alerta = db.Column(db.Boolean, default=False)
    gravidade = db.Column(db.String(10), index=True)  # 'alta' | 'media' | None
    motivo_vigia = db.Column(db.Text)
    enviado_whatsapp = db.Column(db.Boolean, default=False)
    # JSON list ['consultar_produtos', ...] das tools chamadas pelo bot
    # ANTES de produzir o veredito (responder ou handoff). Persistir aqui
    # permite o auditor distinguir handoff "preguicoso" (lista vazia ou
    # so transferir_para_humano) de handoff legitimo, e calcular contencao
    # real. Nullable: registros velhos / detectores deterministicos
    # (followup, abandono, espera_humano) gravam NULL.
    tools_usadas = db.Column(db.Text)
    # Reconhecimento do alerta no PAINEL (banner + som). Quando alguem clica
    # no banner pra "ler", marcamos aqui — o som para e o alerta sai dos
    # pendentes (mas continua no historico). Server-side: silencia em todos
    # os aparelhos. (15/06/2026 — alertas do vigia no /entregas/painel)
    reconhecido_em = db.Column(db.DateTime)
    reconhecido_por_id = db.Column(db.Integer, db.ForeignKey('usuario.id'))


class CopilotConversa(db.Model):
    """Audit trail das interacoes com o copilot.
    Cada prompt do usuario vira 1 registro. Guarda a interpretacao da
    LLM, status (pendente/aprovado/cancelado/executado/falhou) e link
    pro registro resultante (ex: pedido criado)."""
    __tablename__ = 'copilot_conversa'

    id = db.Column(db.Integer, primary_key=True)
    usuario_id = db.Column(db.Integer, db.ForeignKey('usuario.id'), nullable=False, index=True)
    criado_em = db.Column(db.DateTime, default=agora, index=True)
    prompt = db.Column(db.Text, nullable=False)
    # JSON com {tipo, params, explicacao, ambiguidades?}
    interpretacao_json = db.Column(db.Text)
    tipo_acao = db.Column(db.String(40), index=True)
    status = db.Column(db.String(20), default='pendente', index=True)
    executado_em = db.Column(db.DateTime)
    # Link pro registro criado (ex: pedido_loja.id se criou um pedido)
    registro_tipo = db.Column(db.String(40))
    registro_id = db.Column(db.Integer)
    erro = db.Column(db.Text)

    usuario = db.relationship('Usuario')


# ── Gestao de Projetos (PARA + 12 Week Year) ──

class AuditLog(db.Model):
    """Trilha de auditoria de mutacoes em modelos sensiveis.
    Populado automaticamente via SQLAlchemy event listener (depois_flush)
    pros modelos registrados em audit_models.py.

    Guarda snapshot 'antes' e 'depois' em JSON pra reconstrucao."""
    __tablename__ = 'audit_log'

    id = db.Column(db.Integer, primary_key=True)
    usuario_id = db.Column(db.Integer, db.ForeignKey('usuario.id'), index=True)
    criado_em = db.Column(db.DateTime, default=agora, index=True)
    tabela = db.Column(db.String(60), nullable=False, index=True)
    registro_id = db.Column(db.Integer, index=True)
    acao = db.Column(db.String(10), nullable=False)  # insert | update | delete
    antes = db.Column(db.Text)  # JSON: snapshot pré-mudança (null em insert)
    depois = db.Column(db.Text)  # JSON: snapshot pós-mudança (null em delete)
    ip = db.Column(db.String(45))
    user_agent = db.Column(db.String(300))

    usuario = db.relationship('Usuario')

class SeruProdutoMap(db.Model):
    """Mapeia 'nome do produto' como vem da Seru pra um item do nosso catalogo.

    Estados (mutuamente exclusivos):
    - MAPEADO: receita_id ou produto_id setado → auto-baixa estoque na venda
    - IGNORADO: ignorar=True → nunca processa (cafe, agua, etc)
    - PENDENTE: tudo NULL/False → fica na fila de revisao, vendas nao baixam

    Composicao: fator_quantidade indica quanto 1 venda Seru desconta do alvo.
    Ex: 'NOZES COM MANTEIGA' = 2 fatias de 1 Sourdough que rende 10 fatias →
    fator_quantidade = 0.2. Default 1.0 (1 venda = 1 unidade do alvo).
    """
    __tablename__ = 'seru_produto_map'

    id = db.Column(db.Integer, primary_key=True)
    seru_nome = db.Column(db.String(300), nullable=False, unique=True, index=True)
    seru_sku = db.Column(db.String(100), nullable=True)
    receita_id = db.Column(db.Integer, db.ForeignKey('receita.id'), nullable=True)
    produto_id = db.Column(db.Integer, db.ForeignKey('produto.id'), nullable=True)
    ignorar = db.Column(db.Boolean, default=False, nullable=False)
    fator_quantidade = db.Column(db.Float, nullable=False, default=1.0)

    primeira_visto_em = db.Column(db.DateTime, default=agora)
    confirmado_em = db.Column(db.DateTime, nullable=True)
    confirmado_por = db.Column(db.Integer, db.ForeignKey('usuario.id'), nullable=True)

    receita = db.relationship('Receita')
    produto = db.relationship('Produto')

    @property
    def estado(self):
        if self.ignorar:
            return 'ignorado'
        if self.receita_id or self.produto_id:
            return 'mapeado'
        return 'pendente'

    @property
    def alvo_nome(self):
        if self.receita:
            return self.receita.nome
        if self.produto:
            return self.produto.nome
        return None

class TinyProdutoMap(db.Model):
    """Liga um item NOSSO (Receita/Produto) ao SKU dele no Tiny, pra emissao
    de NF (Fase 5). Direcao inversa do SeruProdutoMap: aqui NOS mandamos pro
    Tiny, entao a chave eh o nosso item e o valor eh o SKU.

    `canal` (06/07/2026): no Tiny o B2B eh OUTRO cadastro/lista de preco —
    o mesmo item nosso pode apontar pra SKUs DIFERENTES por canal
    ('site' | 'b2b'). Cada canal tem a propria tela de mapeamento
    (/admin/loja-online/tiny-skus e /b2b/tiny-skus).

    O fiscal (NCM/CFOP/CST) NAO mora aqui — fica no cadastro do produto no
    Tiny; a emissao so referencia o SKU e o Tiny aplica os impostos."""
    __tablename__ = 'tiny_produto_map'

    id = db.Column(db.Integer, primary_key=True)
    canal = db.Column(db.String(10), nullable=False, default='site',
                      server_default='site')  # 'site' | 'b2b'
    kind = db.Column(db.String(10), nullable=False)   # 'receita' | 'produto'
    item_id = db.Column(db.Integer, nullable=False)
    tiny_sku = db.Column(db.String(100), nullable=True)
    tiny_nome = db.Column(db.String(300), nullable=True)  # snapshot p/ exibir
    auto_match = db.Column(db.Boolean, default=False)     # setado por fuzzy
    confirmado_em = db.Column(db.DateTime, nullable=True)
    confirmado_por = db.Column(db.Integer, db.ForeignKey('usuario.id'),
                               nullable=True)
    criado_em = db.Column(db.DateTime, default=agora)
    atualizado_em = db.Column(db.DateTime, default=agora, onupdate=agora)

    __table_args__ = (
        db.UniqueConstraint('canal', 'kind', 'item_id',
                            name='uq_tiny_map_canal_item'),
    )

    @property
    def estado(self):
        return 'mapeado' if (self.tiny_sku or '').strip() else 'pendente'


class SeruLojaMap(db.Model):
    """Mapeia 'company.name' da Seru pra nossa Loja. Auto-fuzzy na primeira
    aparicao; admin pode confirmar/corrigir/ignorar via /pdv/config-lojas."""
    __tablename__ = 'seru_loja_map'

    id = db.Column(db.Integer, primary_key=True)
    seru_company_name = db.Column(db.String(300), nullable=False, unique=True, index=True)
    # Ancora ESTAVEL do vinculo: UUID da company na API do Seru. O nome vira
    # so rotulo — renome no Seru atualiza `seru_company_name` sozinho no sync
    # (incidente 06-07/07/2026: renomearam as lojas e o vinculo por nome
    # quebrou em silencio). Backfill na primeira venda; ALTER em
    # migrations_legacy (procedimento de 2 commits).
    seru_company_id = db.Column(db.String(64), nullable=True, index=True)
    # CNPJ da company (pedido do dono 07/07/2026): e por ele que o humano
    # reconhece a loja (matriz x filial) na hora de vincular. Backfill na
    # primeira venda, junto com o id.
    seru_company_document = db.Column(db.String(20), nullable=True)
    loja_id = db.Column(db.Integer, db.ForeignKey('loja.id'), nullable=True)
    ignorar = db.Column(db.Boolean, default=False, nullable=False)
    auto_match = db.Column(db.Boolean, default=False)  # True se foi setado via fuzzy
    confirmado_em = db.Column(db.DateTime, nullable=True)
    confirmado_por = db.Column(db.Integer, db.ForeignKey('usuario.id'), nullable=True)

    loja = db.relationship('Loja')

    @property
    def cnpj_fmt(self):
        d = ''.join(c for c in (self.seru_company_document or '')
                    if c.isdigit())
        if len(d) != 14:
            return self.seru_company_document or None
        return f'{d[:2]}.{d[2:5]}.{d[5:8]}/{d[8:12]}-{d[12:]}'

    @property
    def estado(self):
        if self.ignorar:
            return 'ignorado'
        if self.loja_id:
            return 'mapeado'
        return 'pendente'

class SeruPedidoProcessado(db.Model):
    """Garante idempotencia: cada pedido Seru e processado UMA vez.
    Se a venda for cancelada na Seru depois, marcamos cancelado_em e
    o proximo sync gera estornos."""
    __tablename__ = 'seru_pedido_processado'

    seru_pedido_id = db.Column(db.String(100), primary_key=True)
    processado_em = db.Column(db.DateTime, default=agora)
    loja_id = db.Column(db.Integer, db.ForeignKey('loja.id'), nullable=True)
    n_itens_total = db.Column(db.Integer, default=0)
    n_itens_baixados = db.Column(db.Integer, default=0)
    cancelado_em = db.Column(db.DateTime, nullable=True)
    estornado_em = db.Column(db.DateTime, nullable=True)

class TinyPedidoProcessado(db.Model):
    """Idempotencia do PDV do TINY (27/07/2026): cada pedido do Tiny e
    processado UMA vez, mesmo com o sync rodando a cada N minutos.

    Espelho do `SeruPedidoProcessado`. Tabela NOVA — criada por
    `db.create_all` no startup, sem ALTER (o procedimento de 2 commits vale
    pra COLUNA nova, nao pra tabela).

    IMPORTANTE: no Tiny, o NOSSO sistema so cria NOTA (`tiny_nf`), nunca
    pedido — `tiny.incluir_pedido` nao tem chamador. Logo todo `pedido` que
    a API devolve nasceu no PDV, e importar por ali NAO colide com a baixa
    que o site/B2B ja fazem por conta propria.
    """
    __tablename__ = 'tiny_pedido_processado'

    tiny_pedido_id = db.Column(db.String(100), primary_key=True)
    numero = db.Column(db.String(40), nullable=True)
    processado_em = db.Column(db.DateTime, default=agora)
    loja_id = db.Column(db.Integer, db.ForeignKey('loja.id'), nullable=True)
    data_pedido = db.Column(db.Date, nullable=True, index=True)
    valor = db.Column(db.Numeric(10, 2), nullable=True)
    n_itens_total = db.Column(db.Integer, default=0)
    n_itens_baixados = db.Column(db.Integer, default=0)
    # Situacao do Tiny no momento do processamento (ex: 'Faturado'). Venda
    # que vira 'Cancelado' depois gera estorno no proximo sync.
    situacao = db.Column(db.String(40), nullable=True)
    cancelado_em = db.Column(db.DateTime, nullable=True)
    estornado_em = db.Column(db.DateTime, nullable=True)

    def __repr__(self):
        return f'<TinyPedidoProcessado {self.tiny_pedido_id} {self.situacao}>'


class SeruDebito(db.Model):
    """Acumulador de baixas fracionadas por (loja, produto Seru).

    Quando um produto Seru tem fator_quantidade < 1 (ex: 0.2), vender 1 nao
    baixa estoque inteiro. A fracao fica aqui ate atingir >= 1 inteiro, dai
    baixa N inteiros do EstoqueLoja e fracao_pendente fica com o resto.
    """
    __tablename__ = 'seru_debito'

    loja_id = db.Column(db.Integer, db.ForeignKey('loja.id'), primary_key=True)
    seru_produto_map_id = db.Column(db.Integer,
                                     db.ForeignKey('seru_produto_map.id', ondelete='CASCADE'),
                                     primary_key=True)
    fracao_pendente = db.Column(db.Float, nullable=False, default=0.0)
    atualizado_em = db.Column(db.DateTime, default=agora,
                               onupdate=agora)


class SeruDebitoMov(db.Model):
    """Rastreio de cada contribuicao individual ao SeruDebito.fracao_pendente.

    Necessario pra estornar fracoes quando um pedido com fator < 1 eh
    cancelado antes de virar inteiro. Sem isso, a fracao contribuida pelo
    pedido fica "presa" no acumulador (bug B9 da auditoria de 2026-05-21).
    """
    __tablename__ = 'seru_debito_mov'

    id = db.Column(db.Integer, primary_key=True)
    loja_id = db.Column(db.Integer, db.ForeignKey('loja.id'), nullable=False)
    seru_produto_map_id = db.Column(db.Integer,
                                     db.ForeignKey('seru_produto_map.id'),
                                     nullable=False)
    seru_pedido_id = db.Column(db.String(64), nullable=False, index=True)
    # Contribuicao bruta desse pedido pra acumulador (= qtd * fator).
    # Pode ser fracionaria (0.2) ou misturada (1.4 = 1 inteiro + 0.4 fracao).
    fracao = db.Column(db.Float, nullable=False)
    criado_em = db.Column(db.DateTime, default=agora, nullable=False)
    estornado_em = db.Column(db.DateTime, nullable=True)

    __table_args__ = (
        db.Index('ix_seru_debito_mov_pedido_status',
                  'seru_pedido_id', 'estornado_em'),
    )


class VendaSeruDiaria(db.Model):
    """Snapshot persistente das vendas do Seru por DIA + loja + produto.

    Gravado pelo capturador (a partir da API) pra que relatorio/analises leiam do
    NOSSO banco em vez de re-consultar a API a cada request — com ~600 pedidos/dia
    a consulta ao vivo estoura em ranges largos (o "erro de rede" da tela). NAO
    substitui o MovEstoqueLoja (baixa de estoque); e a fonte do RELATORIO de itens
    vendidos e do faturamento por loja.

    Idempotente por (data, loja_seru, seru_nome): recapturar o dia sobrescreve os
    numeros daquele dia. Dinheiro em Numeric (regra do projeto). `loja_seru` e o
    company.name do Seru (sempre preenchido, chave de agrupamento do relatorio);
    `loja_id` e o vinculo resolvido (SeruLojaMap) quando existir."""
    __tablename__ = 'venda_seru_diaria'

    id = db.Column(db.Integer, primary_key=True)
    data = db.Column(db.Date, nullable=False, index=True)
    loja_seru = db.Column(db.String(200), nullable=False, index=True)
    loja_id = db.Column(db.Integer, db.ForeignKey('loja.id'), nullable=True)
    seru_nome = db.Column(db.String(300), nullable=False)
    sku = db.Column(db.String(100), nullable=True)
    qtd = db.Column(db.Numeric(12, 3), nullable=False, default=0)
    faturamento = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    n_pedidos = db.Column(db.Integer, nullable=False, default=0)
    atualizado_em = db.Column(db.DateTime, default=agora, onupdate=agora)

    loja = db.relationship('Loja')

    __table_args__ = (
        db.UniqueConstraint('data', 'loja_seru', 'seru_nome',
                            name='uq_venda_seru_diaria'),
        db.Index('ix_venda_seru_diaria_periodo', 'data', 'loja_seru'),
    )


class VendaSeruDiaLoja(db.Model):
    """Totais de venda do Seru por DIA + loja (companheira de VendaSeruDiaria).

    Existe pra dar a contagem de PEDIDOS DISTINTOS e o faturamento certos por
    dia/loja: somar n_pedidos das linhas por PRODUTO inflaria (1 pedido com 3
    itens contaria 3x). Como cada pedido tem UM dia e UMA loja, somar por
    (data, loja) da o total exato de pedidos no intervalo. Gravada junto pelo
    mesmo capturador, idempotente por (data, loja_seru)."""
    __tablename__ = 'venda_seru_dia_loja'

    id = db.Column(db.Integer, primary_key=True)
    data = db.Column(db.Date, nullable=False, index=True)
    loja_seru = db.Column(db.String(200), nullable=False, index=True)
    loja_id = db.Column(db.Integer, db.ForeignKey('loja.id'), nullable=True)
    n_pedidos = db.Column(db.Integer, nullable=False, default=0)
    # `faturamento` = soma dos SUBTOTAIS dos itens (base do relatorio por produto;
    # subconta kit/box, cujos itens vem com preco 0). `faturamento_pedidos` = soma
    # do TOTAL do pedido (inclui kit/box) — base do faturamento do bot, pra o
    # dinheiro bater com o que a Seru cobrou. Os dois convivem de proposito.
    faturamento = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    faturamento_pedidos = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    atualizado_em = db.Column(db.DateTime, default=agora, onupdate=agora)

    loja = db.relationship('Loja')

    __table_args__ = (
        db.UniqueConstraint('data', 'loja_seru', name='uq_venda_seru_dia_loja'),
    )


class VendaSeruDiaBreakdown(db.Model):
    """Breakdowns extras da venda Seru por (dia, loja): metodo de PAGAMENTO,
    CANAL de venda e contagem de CANCELADOS — os eixos da tela 'Vendas PDV'.

    Separado de VendaSeruDiaLoja porque sao 1:N (varios metodos/canais por
    dia/loja) e pra nao inchar a tabela de totais. `dimensao`: 'pagamento' |
    'canal' | 'marketplace' | 'cancelados' | 'sem_itens' (18/07/2026 — total
    das cobrancas so-valor do dia, rodape do card Por loja). Pra 'cancelados',
    `valor` e a CONTAGEM (nao dinheiro) e `chave`=''. Pra 'marketplace',
    `valor` tambem e contagem e `chave` identifica ifood/99food/rappi.
    Idempotente por (data, loja_seru, dimensao, chave)."""
    __tablename__ = 'venda_seru_dia_breakdown'

    id = db.Column(db.Integer, primary_key=True)
    data = db.Column(db.Date, nullable=False, index=True)
    loja_seru = db.Column(db.String(200), nullable=False, index=True)
    dimensao = db.Column(db.String(20), nullable=False)
    chave = db.Column(db.String(120), nullable=False, default='')
    valor = db.Column(db.Numeric(14, 2), nullable=False, default=0)
    atualizado_em = db.Column(db.DateTime, default=agora, onupdate=agora)

    __table_args__ = (
        db.UniqueConstraint('data', 'loja_seru', 'dimensao', 'chave',
                            name='uq_venda_seru_dia_breakdown'),
        db.Index('ix_venda_seru_dia_breakdown_periodo', 'data', 'dimensao'),
    )


# ── Motor unico de baixa de venda (Seru + site + saida-lote) ──
# Substitui o trio paralelo SeruProdutoMap/LojaProdutoMap (+VndaProdutoMap morto)
# e os acumuladores SeruDebito/LojaDebito (+VndaDebito). Ver app/services/
# baixa_venda.py. Estrategia (decisao do dono 2026-06-30): composto multi-item
# (cesta/sanduiche) mora na composicao do PRODUTO (ProdutoItem); fator escalar
# de 1 item (cafe -> 0.2 cookie) fica como multiplicador no mapa.

class VendaMapa(db.Model):
    """Mapa unificado: 'nome externo' de um CANAL de venda -> item do catalogo.

    canal: 'seru' (PDV Colibri) | 'lote' (saida-em-lote manual). O SITE nao usa
    mapa — o PedidoOnlineItem ja referencia receita/produto por FK.

    Estados (mutuamente exclusivos):
    - MAPEADO: receita_id/produto_id/materia_prima_id setado -> baixa na venda
    - IGNORADO: ignorar=True -> nunca processa (cafe sem desconto, agua, etc)
    - PENDENTE: tudo NULL/False -> fila de revisao, vendas nao baixam

    fator_quantidade: multiplicador pra item SIMPLES de 1 alvo (ex: 'CAFE' ->
    0.2 de 'Cookie'). Composto multi-item (cesta) NAO usa fator aqui — a
    composicao mora no Produto (ProdutoItem), aplicada pelo motor por cima.
    """
    __tablename__ = 'venda_mapa'

    id = db.Column(db.Integer, primary_key=True)
    canal = db.Column(db.String(20), nullable=False, index=True)
    nome_externo = db.Column(db.String(300), nullable=False, index=True)
    sku = db.Column(db.String(100), nullable=True)
    receita_id = db.Column(db.Integer, db.ForeignKey('receita.id'), nullable=True)
    produto_id = db.Column(db.Integer, db.ForeignKey('produto.id'), nullable=True)
    materia_prima_id = db.Column(db.Integer, db.ForeignKey('materia_prima.id'),
                                 nullable=True)
    ignorar = db.Column(db.Boolean, default=False, nullable=False)
    fator_quantidade = db.Column(db.Float, nullable=False, default=1.0)

    primeira_visto_em = db.Column(db.DateTime, default=agora)
    confirmado_em = db.Column(db.DateTime, nullable=True)
    confirmado_por = db.Column(db.Integer, db.ForeignKey('usuario.id'),
                               nullable=True)

    receita = db.relationship('Receita')
    produto = db.relationship('Produto')
    materia_prima = db.relationship('MateriaPrima')

    __table_args__ = (
        db.UniqueConstraint('canal', 'nome_externo', name='uq_venda_mapa_canal_nome'),
    )

    @property
    def estado(self):
        if self.ignorar:
            return 'ignorado'
        if self.receita_id or self.produto_id or self.materia_prima_id:
            return 'mapeado'
        return 'pendente'

    @property
    def alvo_tipo(self):
        if self.receita_id:
            return 'receita'
        if self.produto_id:
            return 'produto'
        if self.materia_prima_id:
            return 'mp'
        return None

    @property
    def alvo_nome(self):
        if self.receita:
            return self.receita.nome
        if self.produto:
            return self.produto.nome
        if self.materia_prima:
            return self.materia_prima.nome
        return None


class DebitoEstoque(db.Model):
    """Acumulador de fracao por (loja, ITEM FISICO de estoque).

    Quando uma venda consome fracao de um item (cafe -> 0.2 cookie; sanduiche ->
    0.2 sourdough), a fracao fica aqui ate somar >= 1 inteiro, dai baixa N
    inteiros do EstoqueLoja. Chave por ITEM (nao por mapa, como os Seru/LojaDebito
    antigos): assim fracoes de produtos/canais DIFERENTES que consomem o MESMO
    item somam juntas, em vez de ficar presas separadas. Mantem EstoqueLoja
    inteiro.
    """
    __tablename__ = 'debito_estoque'

    id = db.Column(db.Integer, primary_key=True)
    loja_id = db.Column(db.Integer, db.ForeignKey('loja.id'), nullable=False)
    receita_id = db.Column(db.Integer, db.ForeignKey('receita.id'), nullable=True)
    produto_id = db.Column(db.Integer, db.ForeignKey('produto.id'), nullable=True)
    materia_prima_id = db.Column(db.Integer, db.ForeignKey('materia_prima.id'),
                                 nullable=True)
    fracao_pendente = db.Column(db.Float, nullable=False, default=0.0)
    atualizado_em = db.Column(db.DateTime, default=agora, onupdate=agora)

    __table_args__ = (
        db.UniqueConstraint('loja_id', 'receita_id', 'produto_id',
                            'materia_prima_id', name='uq_debito_estoque_item'),
    )


class DebitoEstoqueMov(db.Model):
    """Rastreio de cada contribuicao FRACIONARIA ao DebitoEstoque, por pedido.

    Necessario pra estornar a fracao quando um pedido com consumo fracionario eh
    cancelado antes de virar inteiro (senao a fracao fica presa no acumulador).
    So registra quando a contribuicao tem parte fracionaria — venda inteira eh
    revertida pela referencia do MovEstoqueLoja.
    """
    __tablename__ = 'debito_estoque_mov'

    id = db.Column(db.Integer, primary_key=True)
    loja_id = db.Column(db.Integer, db.ForeignKey('loja.id'), nullable=False)
    receita_id = db.Column(db.Integer, db.ForeignKey('receita.id'), nullable=True)
    produto_id = db.Column(db.Integer, db.ForeignKey('produto.id'), nullable=True)
    materia_prima_id = db.Column(db.Integer, db.ForeignKey('materia_prima.id'),
                                 nullable=True)
    canal = db.Column(db.String(20), nullable=False)
    # Chave do pedido por canal (ex: 'seru:12345', 'site:ABC123'). Liga a
    # contribuicao ao pedido pro estorno.
    pedido_ref = db.Column(db.String(120), nullable=False)
    # Contribuicao bruta deste pedido pra este item (qtd * fator * qtd_componente).
    fracao = db.Column(db.Float, nullable=False)
    criado_em = db.Column(db.DateTime, default=agora, nullable=False)
    estornado_em = db.Column(db.DateTime, nullable=True)

    __table_args__ = (
        db.Index('ix_debito_estoque_mov_pedido_status',
                 'pedido_ref', 'estornado_em'),
    )


class VendaMapaUso(db.Model):
    """Marcador (canal, mapa, loja): qual LOJA ja usou um VendaMapa.

    Substitui o papel que o LojaDebito acumulava de lado (alem de fracao):
    saber "quais lojas baixaram por este mapa" pra montar a coluna 'lojas'
    da tela de mapeamentos de lote. A fracao em si vive no DebitoEstoque
    (por item fisico); este aqui eh so o vinculo de uso pra UI/auditoria.
    Idempotente por (venda_mapa_id, loja_id).
    """
    __tablename__ = 'venda_mapa_uso'

    venda_mapa_id = db.Column(db.Integer,
                              db.ForeignKey('venda_mapa.id', ondelete='CASCADE'),
                              primary_key=True)
    loja_id = db.Column(db.Integer, db.ForeignKey('loja.id'), primary_key=True)
    primeiro_uso_em = db.Column(db.DateTime, default=agora)
    ultimo_uso_em = db.Column(db.DateTime, default=agora, onupdate=agora)


# ── VNDA APOSENTADO (24/06/2026) ──
# Os mapeamentos/idempotencia/acumulador de baixa do VNDA foram removidos
# (VndaProdutoMap / VndaPedidoProcessado / VndaDebito): a baixa de estoque do
# site agora roda pelo motor unico (baixa_venda, canal='site') a partir do
# PedidoOnline (loja propria). As TABELAS antigas (vnda_*) ficam no Postgres
# por enquanto pra preservar historico — db.create_all nao as recria nem dropa.
# So `PedidoSite` sobrevive: e cache do CRM (card de cliente no Chatwoot),
# alimentado por app.services.vnda_card a partir da API VNDA de pedidos.

class PedidoSite(db.Model):
    """Cache local de pedidos do site (VNDA) indexado por telefone, para o
    card de cliente do CRM (Chatwoot).

    Por que existe: a API VNDA filtra pedidos por data de criacao (nao por
    telefone), e o telefone so aparece apos enriquecer cada pedido
    (shipping/cliente). Consultar isso on-demand quando o atendente abre a
    conversa seria lento e bateria demais na API (429). Entao pre-populamos
    esta tabela, indexada por `telefone_chave` (mesma normalizacao BR de
    `app.utils.telefone_chave`), e o card faz lookup instantaneo.

    So existe pro CRM (card do cliente) — a baixa de estoque do site NAO passa
    mais por aqui (roda pelo motor unico via PedidoOnline). Populado por
    `app.services.vnda_card` (cron incremental + backfill manual do admin).
    Idempotente por `code`. Tabela criada por `db.create_all()` no deploy.
    """
    __tablename__ = 'pedido_site'

    code = db.Column(db.String(100), primary_key=True)
    telefone = db.Column(db.String(50))
    telefone_chave = db.Column(db.String(20), index=True)
    comprador = db.Column(db.String(200))
    destinatario = db.Column(db.String(200))
    data_pedido = db.Column(db.Date, index=True)
    data_entrega = db.Column(db.Date)
    # Numeric(10,2): dinheiro com precisao exata (regra do projeto).
    total = db.Column(db.Numeric(10, 2), default=0)
    status_vnda = db.Column(db.String(40))
    itens_json = db.Column(db.Text)  # JSON: [{"nome","qtd","preco"}]
    atualizado_em = db.Column(db.DateTime, default=agora, onupdate=agora)


# ── Saida em lote manual (lojas com PDV sem API) ──
# Mapeia nomes digitados em /pedidos/estoque-loja/saida-lote pra catalogo.
# Vincular uma vez, lembra pra sempre. Espelha SeruProdutoMap.

class LojaProdutoMap(db.Model):
    """Mapeamento persistente de nomes digitados (saida em lote) → catalogo.

    Estados:
    - MAPEADO: receita_id/produto_id/materia_prima_id setado → baixa
    - IGNORADO: ignorar=True → nunca desconta
    - PENDENTE: nada vinculado → fica na fila, saidas nao mexem em estoque
    """
    __tablename__ = 'loja_produto_map'

    id = db.Column(db.Integer, primary_key=True)
    nome_digitado = db.Column(db.String(200), nullable=False, unique=True, index=True)
    receita_id = db.Column(db.Integer, db.ForeignKey('receita.id'), nullable=True)
    produto_id = db.Column(db.Integer, db.ForeignKey('produto.id'), nullable=True)
    materia_prima_id = db.Column(db.Integer, db.ForeignKey('materia_prima.id'), nullable=True)
    ignorar = db.Column(db.Boolean, default=False, nullable=False)
    fator_quantidade = db.Column(db.Float, nullable=False, default=1.0)

    primeira_visto_em = db.Column(db.DateTime, default=agora)
    confirmado_em = db.Column(db.DateTime, nullable=True)
    confirmado_por = db.Column(db.Integer, db.ForeignKey('usuario.id'), nullable=True)

    receita = db.relationship('Receita')
    produto = db.relationship('Produto')
    materia_prima = db.relationship('MateriaPrima')

    @property
    def estado(self):
        if self.ignorar:
            return 'ignorado'
        if self.receita_id or self.produto_id or self.materia_prima_id:
            return 'mapeado'
        return 'pendente'

    @property
    def alvo_nome(self):
        if self.receita:
            return self.receita.nome
        if self.produto:
            return self.produto.nome
        if self.materia_prima:
            return self.materia_prima.nome
        return None

    @property
    def alvo_tipo(self):
        if self.receita_id:
            return 'receita'
        if self.produto_id:
            return 'produto'
        if self.materia_prima_id:
            return 'mp'
        return None

class LojaDebito(db.Model):
    """Acumulador de fracoes pra saida em lote.

    Mesma logica do SeruDebito/VndaDebito: quando fator<1 (ex: 0.2), vender 3
    unidades nao baixa estoque (qtd_efetiva=0.6). A fracao fica aqui ate
    acumular >=1, dai baixa N inteiros. Sem isso, fracoes se perdem.
    """
    __tablename__ = 'loja_debito'

    loja_id = db.Column(db.Integer, db.ForeignKey('loja.id'), primary_key=True)
    loja_produto_map_id = db.Column(db.Integer,
                                     db.ForeignKey('loja_produto_map.id', ondelete='CASCADE'),
                                     primary_key=True)
    fracao_pendente = db.Column(db.Float, nullable=False, default=0.0)
    atualizado_em = db.Column(db.DateTime, default=agora,
                               onupdate=agora)

class SlackVinculo(db.Model):
    """Mapeia slack_user_id → Usuario do sistema.

    Sem vinculo ativo, bot recusa. Admin cria mapeamentos em /slack/install.
    """
    __tablename__ = 'slack_vinculo'

    id = db.Column(db.Integer, primary_key=True)
    slack_user_id = db.Column(db.String(30), unique=True, nullable=False, index=True)
    slack_workspace_id = db.Column(db.String(30))
    usuario_id = db.Column(db.Integer, db.ForeignKey('usuario.id'), nullable=False)
    ativo = db.Column(db.Boolean, default=True, index=True)
    criado_em = db.Column(db.DateTime, default=agora)
    criado_por_id = db.Column(db.Integer, db.ForeignKey('usuario.id'))

    usuario = db.relationship('Usuario', foreign_keys=[usuario_id])
    criado_por = db.relationship('Usuario', foreign_keys=[criado_por_id])

class SlackEventoProcessado(db.Model):
    """Idempotencia: Slack reenvia eventos se nao recebe 200 em 3s.

    Antes de processar, checa se event_id ja foi visto. TTL implicito de 1h
    (limpeza opcional via cron — eventos sao pequenos).
    """
    __tablename__ = 'slack_evento_processado'

    event_id = db.Column(db.String(50), primary_key=True)
    processado_em = db.Column(db.DateTime, default=agora, index=True)

class SlackAcaoPendente(db.Model):
    """Acao de write aguardando confirmacao via botao.

    Token unico embutido no botao. Slack nao guarda params raw (seguranca:
    evita injetar params via interacao). Expira em 10min.
    """
    __tablename__ = 'slack_acao_pendente'

    id = db.Column(db.Integer, primary_key=True)
    token = db.Column(db.String(40), unique=True, nullable=False, index=True)
    slack_user_id = db.Column(db.String(30), nullable=False)
    slack_channel_id = db.Column(db.String(30))
    slack_message_ts = db.Column(db.String(30))  # pra chat.update apos clique
    tipo_acao = db.Column(db.String(50), nullable=False)
    params_json = db.Column(db.Text, nullable=False)
    usuario_id = db.Column(db.Integer, db.ForeignKey('usuario.id'), nullable=False)
    criado_em = db.Column(db.DateTime, default=agora, index=True)
    executado_em = db.Column(db.DateTime)
    cancelado_em = db.Column(db.DateTime)

    usuario = db.relationship('Usuario')

class SlackConversa(db.Model):
    """Context multi-turn por (slack_user_id, channel_id).

    Guarda historico de msgs JSON [{role, content}] pra Claude lembrar
    do contexto entre mensagens (copilot ja aceita historico=...).
    """
    __tablename__ = 'slack_conversa'

    id = db.Column(db.Integer, primary_key=True)
    slack_user_id = db.Column(db.String(30), nullable=False, index=True)
    slack_channel_id = db.Column(db.String(30), nullable=False)
    mensagens_json = db.Column(db.Text, default='[]')
    ultima_msg_em = db.Column(db.DateTime, default=agora, onupdate=agora, index=True)

    __table_args__ = (
        db.UniqueConstraint('slack_user_id', 'slack_channel_id', name='uq_slack_conversa'),
    )


class ZapiBotConversa(db.Model):
    """Conversa persistente do Caio (dono) com o copilot via WhatsApp/Z-API.

    1 unica linha esperada (so o dono usa). Mensagens em JSON pra reusar o
    contrato `historico=[{role, content, imagens?}]` que o copilot ja aceita."""
    __tablename__ = 'zapi_bot_conversa'

    id = db.Column(db.Integer, primary_key=True)
    telefone = db.Column(db.String(30), unique=True, nullable=False, index=True)
    mensagens_json = db.Column(db.Text, default='[]')
    ultima_msg_em = db.Column(db.DateTime, default=agora, onupdate=agora, index=True)


class ZapiBotEventoProcessado(db.Model):
    """Idempotencia: Z-API pode reenviar webhook se nao recebe 200 em ~5s.

    A chave eh o messageId. TTL implicito (acumula; ~baixo volume = 1 user)."""
    __tablename__ = 'zapi_bot_evento_processado'

    message_id = db.Column(db.String(80), primary_key=True)
    processado_em = db.Column(db.DateTime, default=agora, index=True)


class ChatwootEventoProcessado(db.Model):
    """Idempotencia do webhook /crm/bot do Chatwoot.

    Chatwoot pode reenviar `message_created` se o webhook demora (o bot
    precisa de Claude + tools, facilmente passa de 5s). Sem dedupe, a
    mesma mensagem do cliente vira 2 turnos do bot — segunda execucao
    duplica resposta no canal e gasta token a toa.

    PK = message id do Chatwoot (unico por mensagem). Acumula com TTL
    implicito; volume = 1 mensagem por cliente * conversas/dia, fica
    pequeno. Retencao automatica pode entrar depois se necessario."""
    __tablename__ = 'chatwoot_evento_processado'

    message_id = db.Column(db.String(80), primary_key=True)
    conversation_id = db.Column(db.String(40), index=True)
    processado_em = db.Column(db.DateTime, default=agora, index=True)


class ChatbotConversa(db.Model):
    """Historico persistente da conversa do chatbot do cliente (Chatwoot), por
    conversation_id.

    EXISTE porque a API de historico do Chatwoot (`buscar_historico`) falha
    intermitentemente (retorna vazio mesmo havendo conversa). Quando isso
    acontecia, o bot tratava a mensagem como conversa NOVA e 'esquecia' o
    contexto — o cliente mandava o CPF e o bot perguntava 'o que voce precisa?'
    de novo (visto em prod 2026-06-09). Com o historico no NOSSO banco, o
    contexto deixa de depender da confiabilidade do Chatwoot.

    `mensagens_json`: lista [{role: 'user'|'assistant', content: str}], ja
    capada nas ultimas N por `chatbot.salvar_historico`.

    `contato_key` (19/07/2026, auditor "bot reiniciando do zero"): telefone
    canonizado (`telefone_chave`) do contato do canal. Conversa NOVA do
    Chatwoot busca por ela o historico recente do MESMO cliente
    (`chatbot.contexto_do_contato`) — sem isso a memoria morria junto com o
    conv_id. NULL = conversa antiga / canal sem telefone (IG, site)."""
    __tablename__ = 'chatbot_conversa'

    id = db.Column(db.Integer, primary_key=True)
    conv_id = db.Column(db.String(50), unique=True, nullable=False, index=True)
    contato_key = db.Column(db.String(40), index=True)
    mensagens_json = db.Column(db.Text, default='[]')
    ultima_msg_em = db.Column(db.DateTime, default=agora, onupdate=agora, index=True)


class LalamoveEntrega(db.Model):
    """Corrida Lalamove chamada a partir do painel do dia (1 linha por
    cotacao/chamada). `pedido_code` = code do card do painel (VNDA/local).
    Dinheiro em Numeric(10,2) — peso especial."""
    __tablename__ = 'lalamove_entrega'

    id = db.Column(db.Integer, primary_key=True)
    pedido_code = db.Column(db.String(60), nullable=False, index=True)
    data_ref = db.Column(db.Date)

    quotation_id = db.Column(db.String(120))
    sender_stop_id = db.Column(db.String(120))
    recipient_stop_id = db.Column(db.String(120))
    order_id = db.Column(db.String(120), unique=True, index=True)

    # 'cotacao' antes de chamar; depois o status da Lalamove
    # (ASSIGNING_DRIVER/ON_GOING/PICKED_UP/COMPLETED/CANCELED/...).
    status = db.Column(db.String(40), default='cotacao', nullable=False)
    service_type = db.Column(db.String(30))
    valor = db.Column(db.Numeric(10, 2))
    moeda = db.Column(db.String(8))
    distancia_m = db.Column(db.Integer)

    # Priority fee (gorjeta) pra acelerar a alocacao do entregador. Setado
    # via POST /v3/orders/{id}/priority-fee enquanto a corrida procura
    # motorista. Guarda o valor ATUAL da gorjeta (a API substitui, nao soma).
    priority_fee = db.Column(db.Numeric(10, 2))

    endereco_destino = db.Column(db.String(500))
    destinatario = db.Column(db.String(200))
    telefone_destino = db.Column(db.String(40))

    share_link = db.Column(db.String(500))
    motorista_nome = db.Column(db.String(120))
    motorista_telefone = db.Column(db.String(40))

    criado_em = db.Column(db.DateTime, default=agora)
    criado_por_id = db.Column(db.Integer, db.ForeignKey('usuario.id'))
    atualizado_em = db.Column(db.DateTime)


class LalamoveSaldo(db.Model):
    """Ultimo saldo da carteira Lalamove (linha unica, id=1). Alimentado
    pelo webhook WALLET_BALANCE_CHANGED — chega a cada debito/recarga.
    `payload_json` guarda o evento cru pra ajustar o parse se o formato
    real divergir do esperado."""
    __tablename__ = 'lalamove_saldo'

    id = db.Column(db.Integer, primary_key=True)
    valor = db.Column(db.Numeric(10, 2))
    moeda = db.Column(db.String(8), default='BRL')
    payload_json = db.Column(db.Text)
    atualizado_em = db.Column(db.DateTime, default=agora)


class UsoIA(db.Model):
    """Uso/custo de CADA chamada de IA (Anthropic), rotulada por funcao do app.

    Instrumentacao adicionada 25/06/2026 — antes NADA registrava tokens, e o
    gasto por funcao (vigia/auditor/bot/OCR/copilot...) era irrecuperavel. O
    registro e best-effort e em SESSAO ISOLADA (ver `app/services/uso_ia.py`)
    pra nunca contaminar a transacao de negocio do chamador nem quebrar a
    chamada principal.

    Tabela NOVA — criada por `db.create_all()` (checkfirst) no startup, sem
    ALTER legado. `custo_usd` e Numeric porque e dinheiro (regra do CLAUDE.md),
    calculado no registro a partir dos tokens + tabela de precos do servico.
    """
    __tablename__ = 'uso_ia'

    id = db.Column(db.Integer, primary_key=True)
    # Ex: 'vigia', 'auditor', 'bot_atendimento', 'followup', 'copilot',
    # 'ocr_nf', 'ocr_cupom', 'seo'. Index pra agregar rapido por funcao.
    funcao = db.Column(db.String(40), nullable=False, index=True)
    modelo = db.Column(db.String(60))
    # 'slack' | 'whatsapp' — separa o copilot (mesmo motor, canais distintos).
    canal = db.Column(db.String(20))
    input_tokens = db.Column(db.Integer, default=0)
    output_tokens = db.Column(db.Integer, default=0)
    cache_read_tokens = db.Column(db.Integer, default=0)
    cache_create_tokens = db.Column(db.Integer, default=0)
    custo_usd = db.Column(db.Numeric(10, 6))  # dinheiro = Numeric (CLAUDE.md)
    criado_em = db.Column(db.DateTime, default=agora, index=True)

    def __repr__(self):
        return f'<UsoIA {self.funcao} {self.modelo} ${self.custo_usd}>'


# ── Avaliacoes do Google (Business Profile) — 12/07/2026 ──
# Pedido do dono: conectar os comentarios do Google no gestao (ver + responder
# + alerta de review nova). 3 locations: Ribeiro do Vale (Brooklin), Anesio
# Pinto Rosa (Itaim), Nebraska (1851 Coffee). Tabelas NOVAS via db.create_all
# (sem ALTER). A integracao fica DORMENTE ate ter OAuth + acesso aprovado pelo
# Google (mesmo padrao do Seru/Chatwoot). Servico: app/services/google_reviews.py

class GoogleReviewLocation(db.Model):
    """Uma location (estabelecimento) do Google Business Profile, descoberta
    via API depois do OAuth. Mapeia pra uma Loja interna (nullable — o admin
    vincula na tela, mesmo espirito do SeruLojaMap: auto-descoberta + confirma).
    `location_name` = resourceName do Google ('accounts/123/locations/456')."""
    __tablename__ = 'google_review_location'

    id = db.Column(db.Integer, primary_key=True)
    location_name = db.Column(db.String(200), nullable=False, unique=True, index=True)
    apelido = db.Column(db.String(160))     # 'title' vindo da API (nome do local)
    loja_id = db.Column(db.Integer, db.ForeignKey('loja.id'))
    ativo = db.Column(db.Boolean, default=True)
    criado_em = db.Column(db.DateTime, default=agora)

    loja = db.relationship('Loja')


class GoogleReview(db.Model):
    """Uma avaliacao do Google Business Profile. Idempotente por `review_id`
    (o reviewId do Google) — re-sync atualiza a linha existente, nunca duplica.
    `resposta_*` preenchidos quando respondemos (via API + espelho local)."""
    __tablename__ = 'google_review'

    id = db.Column(db.Integer, primary_key=True)
    review_id = db.Column(db.String(255), nullable=False, unique=True, index=True)
    # resourceName da location a que a review pertence (liga a GoogleReviewLocation).
    location_name = db.Column(db.String(200), index=True)
    autor = db.Column(db.String(200))
    autor_foto = db.Column(db.String(500))
    nota = db.Column(db.Integer, index=True)          # 1..5 (convertido do enum)
    comentario = db.Column(db.Text)
    criado_em_google = db.Column(db.DateTime, index=True)
    atualizado_em_google = db.Column(db.DateTime)
    resposta_texto = db.Column(db.Text)
    resposta_em = db.Column(db.DateTime)
    respondida_por_id = db.Column(db.Integer, db.ForeignKey('usuario.id'))
    sincronizado_em = db.Column(db.DateTime, default=agora)
    criado_em = db.Column(db.DateTime, default=agora)

    respondida_por = db.relationship('Usuario')

    @property
    def respondida(self):
        return bool((self.resposta_texto or '').strip())
