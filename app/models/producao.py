"""Modelos do dominio: producao.

Faz parte de `app.models` (split em multiplos arquivos por dominio
em 2026-05-21). Importar via `from app.models import X` continua
funcionando porque `app/models/__init__.py` re-exporta tudo.
"""

from app.extensions import db
from app.utils import agora


class PlanejamentoProducao(db.Model):
    __tablename__ = 'planejamento_producao'

    id = db.Column(db.Integer, primary_key=True)
    data = db.Column(db.Date, nullable=False)
    nome = db.Column(db.String(100))
    criado_em = db.Column(db.DateTime, default=agora)
    criado_por = db.Column(db.Integer, db.ForeignKey('usuario.id'))
    status = db.Column(db.String(20), default='rascunho')
    # 'cronograma' = plano aprovado do cronograma diario (desce pro padeiro);
    # NULL/'manual' = plano avulso/deficit. So pra distinguir na UI.
    origem = db.Column(db.String(20))
    # Fluxo de 2 passos: APROVAR cria a ordem (rascunho, enviado_ao_padeiro=
    # False) -> ENVIAR libera pro padeiro (True). O padeiro só vê o que foi
    # enviado. Ordens antigas nascem True (coluna DEFAULT TRUE na migração).
    enviado_ao_padeiro = db.Column(db.Boolean, default=True)

    itens = db.relationship('PlanejamentoItem', backref='planejamento',
                            cascade='all, delete-orphan', lazy=True)
    autor = db.relationship('Usuario', backref='planejamentos')

    def __repr__(self):
        return f'<Planejamento {self.nome} em {self.data}>'

class PlanejamentoItem(db.Model):
    __tablename__ = 'planejamento_item'

    id = db.Column(db.Integer, primary_key=True)
    planejamento_id = db.Column(db.Integer, db.ForeignKey('planejamento_producao.id'), nullable=False)
    receita_id = db.Column(db.Integer, db.ForeignKey('receita.id'), nullable=False)
    multiplicador = db.Column(db.Integer, default=1)
    # Unidades-alvo (do cronograma) e quanto ja foi produzido. produzido_qtd
    # avanca quando o padeiro marca producao (credita estoque + baixa MP).
    qtd_alvo = db.Column(db.Integer)
    produzido_qtd = db.Column(db.Integer, nullable=False, default=0,
                              server_default='0')
    # Parcela EXTRA adicionada a mao — hoje, o reagendamento da auditoria
    # ("Produzir HOJE os marcados") soma aqui. O re-aprovar/re-enviar do
    # cronograma reconstroi os itens A PARTIR DO GRID e apagava o que nao
    # estava nele: os paes reagendados sumiam da tela do padeiro (bug pego
    # pelo dono 02/07). O sync agora SOMA qtd_extra ao alvo do grid e nunca
    # remove item com extra > 0.
    qtd_extra = db.Column(db.Integer, nullable=False, default=0,
                          server_default='0')
    # Dispensa de pendencia (auditoria): quando o admin verifica que a producao
    # NAO aconteceu (ou aconteceu a menos) e da OK, marca aqui. O item some de
    # TUDO que conta producao pendente: overlay verde, auditoria, gantt e plano
    # do padeiro — e produzir esse item fica BLOQUEADO (decisao do dono 01/07). So
    # NAO mexe em estoque/produzido_qtd (o furo real fica preservado). Reversivel
    # (volta a NULL, reabre em tudo). Ver app/services/producao_pendente.py e o
    # filtro `dispensada_em is None` nos consumidores (gantt.py, padeiro/routes.py).
    dispensada_em = db.Column(db.DateTime, nullable=True)
    dispensada_por_id = db.Column(db.Integer, db.ForeignKey('usuario.id'),
                                  nullable=True)

    receita = db.relationship('Receita')
    dispensada_por = db.relationship('Usuario', foreign_keys=[dispensada_por_id])

    def __repr__(self):
        return f'<PlanejamentoItem receita={self.receita_id} x{self.multiplicador}>'


class PreBaixaMP(db.Model):
    """PRÉ-BAIXA de MP da ordem de produção ENVIADA (pedido do dono
    07/07/2026): ao enviar o plano ao padeiro, a MP da FALTA (alvo −
    produzido dos itens não dispensados) é baixada provisoriamente do
    estoque; quando o padeiro confirma a produção, a parte confirmada vira
    baixa REAL (`produzir_item_plano`) e a pré-baixa correspondente é
    estornada na mesma transação.

    Uma linha por (plano, MP) com a quantidade atualmente pré-baixada.
    Linha com quantidade 0 é MARCADOR de regime: plano SEM nenhuma linha =
    ordem enviada antes da feature (não se pré-baixa retroativo). Toda
    mudança passa por `producao.sincronizar_pre_baixa_mp` (reconciliador
    idempotente) — NUNCA escrever quantidade por fora dele."""
    __tablename__ = 'pre_baixa_mp'

    id = db.Column(db.Integer, primary_key=True)
    plano_id = db.Column(db.Integer,
                         db.ForeignKey('planejamento_producao.id'),
                         nullable=False, index=True)
    materia_prima_id = db.Column(db.Integer,
                                 db.ForeignKey('materia_prima.id'),
                                 nullable=False)
    quantidade = db.Column(db.Float, nullable=False, default=0.0)
    atualizado_em = db.Column(db.DateTime, default=agora, onupdate=agora)

    plano = db.relationship(
        'PlanejamentoProducao',
        backref=db.backref('pre_baixas', cascade='all, delete-orphan',
                           lazy=True))
    materia_prima = db.relationship('MateriaPrima')

    __table_args__ = (
        db.UniqueConstraint('plano_id', 'materia_prima_id',
                            name='uq_pre_baixa_plano_mp'),
    )

    def __repr__(self):
        return (f'<PreBaixaMP plano={self.plano_id} mp={self.materia_prima_id} '
                f'qtd={self.quantidade}>')


class PrevisaoSnapshot(db.Model):
    """Instrumentacao de acuracia do forecast (28/06/2026): congela o
    `previsto` do pedido semanal por (data de entrega, loja, receita) no
    momento em que foi gerado, e depois casa com o `realizado` (entregue)
    pra medir vies e erro. Sem isso nao havia como saber se a previsao
    acerta — qualquer 'melhoria' era no escuro.

    Uma linha por (data_alvo, loja, receita, MOTOR, LEAD): o cron diario
    congela a previsao de CADA antecedencia (D-6..D-0) da mesma data — a
    tabela "por lead" da acuracia compara antecedencias de verdade
    (11/07/2026, aprovado pelo dono; antes so a primeira previsao vista
    era gravada e quase tudo caia no lead maximo). `realizado` fica NULL
    ate a data passar e o cron casar.

    Fase 0 (02/07/2026): a acuracia media SO o motor aposentado
    (sugerir_pedidos_semana). Agora cada snapshot registra de QUAL motor veio
    ('pedido_semana' legado, 'media_pedido', 'venda_estoque') e o `lead_dias`
    (antecedencia) — sem isso, erros de leads diferentes se misturavam na
    mesma metrica e nao dava pra comparar motores.
    """
    __tablename__ = 'previsao_snapshot'

    id = db.Column(db.Integer, primary_key=True)
    data_alvo = db.Column(db.Date, nullable=False, index=True)
    loja_id = db.Column(db.Integer, db.ForeignKey('loja.id'), nullable=False)
    receita_id = db.Column(db.Integer, db.ForeignKey('receita.id'),
                           nullable=False)
    previsto = db.Column(db.Integer, nullable=False, default=0)
    # NULL ate a data_alvo passar; preenchido pelo cron com o entregue real.
    realizado = db.Column(db.Integer, nullable=True)
    casado_em = db.Column(db.DateTime, nullable=True)
    criado_em = db.Column(db.DateTime, default=agora, index=True)
    # Motor que gerou o previsto: 'media_pedido' (1b) / 'venda_estoque' (1a) /
    # 'pedido_semana' (legado, linhas antigas).
    motor = db.Column(db.String(20), nullable=False, default='pedido_semana',
                      server_default='pedido_semana')
    # (data_alvo - hoje) no momento do snapshot — segmenta o erro por
    # antecedencia (prever pra amanha e mais facil que pra daqui a 6 dias).
    lead_dias = db.Column(db.Integer, nullable=True)

    loja = db.relationship('Loja')
    receita = db.relationship('Receita')

    __table_args__ = (
        # Commit 2/2 da antecedencia (11/07/2026): o ALTER que trocou a
        # unique em prod ja deployou (migrations_legacy, commit 1).
        db.UniqueConstraint('data_alvo', 'loja_id', 'receita_id', 'motor',
                            'lead_dias',
                            name='uq_previsao_snapshot_alvo_motor_lead'),
    )

    def __repr__(self):
        return (f'<PrevisaoSnapshot {self.data_alvo} loja={self.loja_id} '
                f'rec={self.receita_id} motor={self.motor} '
                f'prev={self.previsto} real={self.realizado}>')


class CronogramaOverride(db.Model):
    """Edicao MANUAL de uma celula do cronograma (29/06/2026): o admin ajusta
    quanto produzir de uma receita num dia, direto na grade. Guardamos a
    distribuicao manual por (data, receita) — o cronograma usa esses valores no
    lugar da distribuicao calculada.

    Modelo POR CELULA (30/06): cada dia editado guarda o seu override; os demais
    seguem a sugestao calculada. O total da linha e a SOMA das celulas (da pra
    produzir mais/menos que o sugerido e programar linha zerada). `criado_em`
    data a edicao. Anti-staleness (E3): o override NAO reverte sozinho quando a
    demanda muda; o cronograma compara o total manual com a sugestao atual e, se
    divergem e a edicao e de um dia anterior, avisa no grid (override_stale) —
    manter ou resetar fica a cargo do usuario (ver cronograma_edit.aplicar_overrides).
    """
    __tablename__ = 'cronograma_override'

    id = db.Column(db.Integer, primary_key=True)
    data = db.Column(db.Date, nullable=False, index=True)
    receita_id = db.Column(db.Integer, db.ForeignKey('receita.id'),
                           nullable=False, index=True)
    qtd = db.Column(db.Integer, nullable=False, default=0)
    criado_em = db.Column(db.DateTime, default=agora)

    receita = db.relationship('Receita')

    __table_args__ = (
        db.UniqueConstraint('data', 'receita_id',
                            name='uq_cronograma_override'),
    )

    def __repr__(self):
        return (f'<CronogramaOverride {self.data} rec={self.receita_id} '
                f'qtd={self.qtd}>')


class CronogramaDiaFechado(db.Model):
    """Cadeado (🔒) de um DIA do grid do cronograma (pedido do dono
    08/07/2026): dia fechado nao aceita edicao de celula por NENHUM caminho
    (grid, mao-dupla do editar-plano) e as acoes em massa — "limpar edicoes
    manuais" e o reset (↺) por linha — PULAM o dia, preservando o trabalho
    manual dele. Reversivel: reabrir o cadeado volta tudo ao normal.

    O cadeado protege as EDICOES do rascunho; os gestos explicitos de ordem
    (enviar/atualizar producao, aprovar, excluir) continuam funcionando —
    eles ja tem confirm() proprio e sao o proposito do dia fechado ("fechei,
    agora envio").
    """
    __tablename__ = 'cronograma_dia_fechado'

    id = db.Column(db.Integer, primary_key=True)
    data = db.Column(db.Date, nullable=False, unique=True, index=True)
    criado_em = db.Column(db.DateTime, default=agora)
    criado_por_id = db.Column(db.Integer, db.ForeignKey('usuario.id'),
                              nullable=True)

    def __repr__(self):
        return f'<CronogramaDiaFechado {self.data}>'
