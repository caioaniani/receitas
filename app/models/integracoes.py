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

class SeruLojaMap(db.Model):
    """Mapeia 'company.name' da Seru pra nossa Loja. Auto-fuzzy na primeira
    aparicao; admin pode confirmar/corrigir/ignorar via /pdv/config-lojas."""
    __tablename__ = 'seru_loja_map'

    id = db.Column(db.Integer, primary_key=True)
    seru_company_name = db.Column(db.String(300), nullable=False, unique=True, index=True)
    loja_id = db.Column(db.Integer, db.ForeignKey('loja.id'), nullable=True)
    ignorar = db.Column(db.Boolean, default=False, nullable=False)
    auto_match = db.Column(db.Boolean, default=False)  # True se foi setado via fuzzy
    confirmado_em = db.Column(db.DateTime, nullable=True)
    confirmado_por = db.Column(db.Integer, db.ForeignKey('usuario.id'), nullable=True)

    loja = db.relationship('Loja')

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


# ── Integracao VNDA (site/e-commerce): mapeamentos + idempotencia ──
# Sempre baixa da loja fixa (Loja Anesio Pinto Rosa). Baixa acontece no
# dia da entrega (expected_delivery_date), nao quando pago/entregue.

class VndaProdutoMap(db.Model):
    """Espelha SeruProdutoMap — mesma logica de estado e fator."""
    __tablename__ = 'vnda_produto_map'

    id = db.Column(db.Integer, primary_key=True)
    vnda_nome = db.Column(db.String(300), nullable=False, unique=True, index=True)
    vnda_sku = db.Column(db.String(100), nullable=True)
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

class VndaPedidoProcessado(db.Model):
    """Idempotencia: cada pedido VNDA processado uma vez. Identificado pelo
    'code' do VNDA. Cancelados depois geram estorno automatico."""
    __tablename__ = 'vnda_pedido_processado'

    vnda_pedido_code = db.Column(db.String(100), primary_key=True)
    processado_em = db.Column(db.DateTime, default=agora)
    data_entrega = db.Column(db.Date)  # data agendada de entrega
    n_itens_total = db.Column(db.Integer, default=0)
    n_itens_baixados = db.Column(db.Integer, default=0)
    cancelado_em = db.Column(db.DateTime, nullable=True)
    estornado_em = db.Column(db.DateTime, nullable=True)

class VndaDebito(db.Model):
    """Acumulador de baixas fracionadas por produto VNDA + componente.

    `componente_key` permite que CESTAS (Produto com ProdutoItens) tenham
    um acumulador POR COMPONENTE — cada item interno baixa separado.
    Valores: 'self' (produto simples) | 'r:<id>' (receita componente) |
    'm:<id>' (materia-prima componente).
    """
    __tablename__ = 'vnda_debito'

    vnda_produto_map_id = db.Column(db.Integer,
                                     db.ForeignKey('vnda_produto_map.id', ondelete='CASCADE'),
                                     primary_key=True)
    componente_key = db.Column(db.String(50), primary_key=True, default='self')
    fracao_pendente = db.Column(db.Float, nullable=False, default=0.0)
    atualizado_em = db.Column(db.DateTime, default=agora,
                               onupdate=agora)


class PedidoSite(db.Model):
    """Cache local de pedidos do site (VNDA) indexado por telefone, para o
    card de cliente do CRM (Chatwoot).

    Por que existe: a API VNDA filtra pedidos por data de criacao (nao por
    telefone), e o telefone so aparece apos enriquecer cada pedido
    (shipping/cliente). Consultar isso on-demand quando o atendente abre a
    conversa seria lento e bateria demais na API (429). Entao pre-populamos
    esta tabela, indexada por `telefone_chave` (mesma normalizacao BR de
    `app.utils.telefone_chave`), e o card faz lookup instantaneo.

    `VndaPedidoProcessado` NAO serve: guarda so o code (idempotencia da baixa
    de estoque), sem telefone nem itens. Populado por
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
