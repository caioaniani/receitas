"""Gamificação de treinamento — CONTEÚDO e ATIVIDADE (Fases 2-8 do spec v1.0).

Trilha -> vídeo -> checkpoints; quiz com banco de questões; aplicação prática;
selo/certificado; recompensas/resgate; fechamento semanal. Tudo keyed em
`funcionario` (RH). Tabelas NOVAS (db.create_all, sem ALTER), namespaced
`treino_`. Regras de anti-fraude e pontuação: ver spec §4, §8, §9, §10, §11 e
os serviços em app/services/treino_*.py.
"""
import uuid

from app.extensions import db
from app.utils import agora

__all__ = [
    'TreinoTrilha', 'TreinoVideo', 'TreinoCheckpoint', 'TreinoProgressoVideo',
    'TreinoRespostaCheckpoint', 'TreinoQuiz', 'TreinoQuestao',
    'TreinoAlternativa', 'TreinoTentativaQuiz', 'TreinoRespostaQuiz',
    'TreinoChecklistAplicacao', 'TreinoItemChecklist', 'TreinoAplicacaoPratica',
    'TreinoSelo', 'TreinoRecompensa', 'TreinoResgate', 'TreinoFechamentoSemanal',
    'TreinoTrilhaCargo',
    'DIFICULDADES', 'APLICACAO_STATUS', 'RESGATE_STATUS',
]

DIFICULDADES = ('FACIL', 'MEDIA', 'DIFICIL')
APLICACAO_STATUS = ('REGISTRADA', 'ESTORNADA')
RESGATE_STATUS = ('SOLICITADO', 'APROVADO', 'ENTREGUE', 'CANCELADO')


# ── Conteúdo (Fase 2) ───────────────────────────────────────────────────
class TreinoTrilha(db.Model):
    __tablename__ = 'treino_trilha'
    id = db.Column(db.Integer, primary_key=True)
    nome = db.Column(db.String(150), nullable=False)
    descricao = db.Column(db.Text)
    ordem = db.Column(db.Integer, default=0, nullable=False, index=True)
    carga_horaria_minutos = db.Column(db.Integer, default=0, nullable=False)
    ativa = db.Column(db.Boolean, default=True, nullable=False)

    videos = db.relationship(
        'TreinoVideo', backref='trilha', order_by='TreinoVideo.ordem')


class TreinoVideo(db.Model):
    __tablename__ = 'treino_video'
    id = db.Column(db.Integer, primary_key=True)
    trilha_id = db.Column(
        db.Integer, db.ForeignKey('treino_trilha.id'), nullable=False,
        index=True)
    titulo = db.Column(db.String(200), nullable=False)
    provedor = db.Column(db.String(20), default='cloudflare', nullable=False)
    video_externo_id = db.Column(db.String(200))     # UID no Cloudflare Stream
    duracao_segundos = db.Column(db.Integer, default=0, nullable=False)
    ordem = db.Column(db.Integer, default=0, nullable=False)
    # Vídeo atualizado com exige_reassistir=TRUE incrementa `versao` e força
    # novo ciclo de conclusão (§5.1). Com FALSE não re-credita nada.
    versao = db.Column(db.Integer, default=1, nullable=False)
    exige_reassistir = db.Column(db.Boolean, default=False, nullable=False)
    ativo = db.Column(db.Boolean, default=True, nullable=False)
    # Roteiro de GRAVAÇÃO da aula (13/08/2026): o plano de conteúdo da
    # "Universidade" chega por planilha antes dos vídeos existirem — a aula
    # nasce rascunho com o roteiro anexado e quem grava lê aqui. Material de
    # ADMIN: o funcionário nunca vê. ALTER aplicado em prod (c32f4c37) e
    # confirmado pela sonda ?colunas= ANTES deste modelo — procedimento de
    # 2 commits.
    roteiro = db.Column(db.Text)

    checkpoints = db.relationship(
        'TreinoCheckpoint', backref='video',
        order_by='TreinoCheckpoint.segundo', cascade='all, delete-orphan')


class TreinoCheckpoint(db.Model):
    """Pergunta que PAUSA o vídeo no segundo `segundo` (§9.1). `alternativas` =
    lista JSON de textos; `indice_correto` aponta a certa."""
    __tablename__ = 'treino_checkpoint'
    id = db.Column(db.Integer, primary_key=True)
    video_id = db.Column(
        db.Integer, db.ForeignKey('treino_video.id'), nullable=False,
        index=True)
    segundo = db.Column(db.Integer, nullable=False)
    enunciado = db.Column(db.Text, nullable=False)
    alternativas = db.Column(db.JSON, nullable=False)   # ["a","b","c"]
    indice_correto = db.Column(db.Integer, nullable=False)
    pontos = db.Column(db.Integer, default=5, nullable=False)


class TreinoProgressoVideo(db.Model):
    """Anti-fraude do vídeo (§9.1): `tempo_real_decorrido` medido pelo RELÓGIO
    DO SERVIDOR entre heartbeats. Conclui só com ≥90% E ≥0,8×duração E todos os
    checkpoints. UMA linha por (funcionário, vídeo, versão)."""
    __tablename__ = 'treino_progresso_video'
    __table_args__ = (
        db.UniqueConstraint('funcionario_id', 'video_id', 'versao_video',
                            name='uq_treino_prog_video'),
    )
    id = db.Column(db.Integer, primary_key=True)
    funcionario_id = db.Column(
        db.Integer, db.ForeignKey('funcionario.id'), nullable=False, index=True)
    video_id = db.Column(
        db.Integer, db.ForeignKey('treino_video.id'), nullable=False,
        index=True)
    versao_video = db.Column(db.Integer, default=1, nullable=False)
    segundos_assistidos = db.Column(db.Integer, default=0, nullable=False)
    percentual = db.Column(db.Numeric(5, 2), default=0, nullable=False)
    tempo_real_decorrido = db.Column(db.Integer, default=0, nullable=False)
    # Baldes de segundo REALMENTE reproduzidos (anti-pulo). JSON list de ints.
    baldes_json = db.Column(db.Text, default='[]', nullable=False)
    # Última posição/heartbeat pra medir delta pelo relógio do servidor.
    ultima_posicao = db.Column(db.Integer, default=0, nullable=False)
    ultimo_heartbeat_em = db.Column(db.DateTime)
    iniciado_em = db.Column(db.DateTime, default=agora, nullable=False)
    concluido_em = db.Column(db.DateTime)


class TreinoRespostaCheckpoint(db.Model):
    __tablename__ = 'treino_resposta_checkpoint'
    __table_args__ = (
        db.UniqueConstraint('funcionario_id', 'checkpoint_id',
                            name='uq_treino_resp_checkpoint'),
    )
    id = db.Column(db.Integer, primary_key=True)
    funcionario_id = db.Column(
        db.Integer, db.ForeignKey('funcionario.id'), nullable=False, index=True)
    checkpoint_id = db.Column(
        db.Integer, db.ForeignKey('treino_checkpoint.id'), nullable=False,
        index=True)
    indice_escolhido = db.Column(db.Integer, nullable=False)
    correta = db.Column(db.Boolean, nullable=False)
    respondido_em = db.Column(db.DateTime, default=agora, nullable=False)


# ── Quiz (Fase 3) ───────────────────────────────────────────────────────
class TreinoQuiz(db.Model):
    __tablename__ = 'treino_quiz'
    __table_args__ = (
        # Exatamente um de (video_id, trilha_id) não-nulo.
        db.CheckConstraint('(video_id IS NULL) <> (trilha_id IS NULL)',
                           name='ck_treino_quiz_alvo'),
    )
    id = db.Column(db.Integer, primary_key=True)
    video_id = db.Column(db.Integer, db.ForeignKey('treino_video.id'),
                         index=True)
    trilha_id = db.Column(db.Integer, db.ForeignKey('treino_trilha.id'),
                          index=True)
    titulo = db.Column(db.String(200), nullable=False)
    questoes_por_tentativa = db.Column(db.Integer, default=5, nullable=False)
    nota_minima = db.Column(db.Numeric(3, 2), default=0.70, nullable=False)
    cooldown_minutos = db.Column(db.Integer, default=120, nullable=False)
    ativo = db.Column(db.Boolean, default=True, nullable=False)

    questoes = db.relationship('TreinoQuestao', backref='quiz')


class TreinoQuestao(db.Model):
    __tablename__ = 'treino_questao'
    id = db.Column(db.Integer, primary_key=True)
    quiz_id = db.Column(
        db.Integer, db.ForeignKey('treino_quiz.id'), nullable=False, index=True)
    enunciado = db.Column(db.Text, nullable=False)
    dificuldade = db.Column(db.String(10), default='MEDIA', nullable=False)
    ativa = db.Column(db.Boolean, default=True, nullable=False)

    alternativas = db.relationship(
        'TreinoAlternativa', backref='questao', cascade='all, delete-orphan')


class TreinoAlternativa(db.Model):
    __tablename__ = 'treino_alternativa'
    id = db.Column(db.Integer, primary_key=True)
    questao_id = db.Column(
        db.Integer, db.ForeignKey('treino_questao.id'), nullable=False,
        index=True)
    texto = db.Column(db.Text, nullable=False)
    correta = db.Column(db.Boolean, default=False, nullable=False)
    # "todas as anteriores"/"nenhuma" não podem ser embaralhadas (§9.2).
    ordem_fixa = db.Column(db.Boolean, default=False, nullable=False)


class TreinoTentativaQuiz(db.Model):
    __tablename__ = 'treino_tentativa_quiz'
    __table_args__ = (
        db.UniqueConstraint('funcionario_id', 'quiz_id', 'numero_tentativa',
                            name='uq_treino_tentativa'),
    )
    id = db.Column(db.Integer, primary_key=True)
    funcionario_id = db.Column(
        db.Integer, db.ForeignKey('funcionario.id'), nullable=False, index=True)
    quiz_id = db.Column(
        db.Integer, db.ForeignKey('treino_quiz.id'), nullable=False, index=True)
    numero_tentativa = db.Column(db.Integer, nullable=False)
    # Congela o sorteio (ids das questões + ordem das alternativas) pra auditar
    # a prova exata mesmo que a questão seja desativada depois (§5.1).
    questoes_sorteadas = db.Column(db.JSON, nullable=False)
    iniciado_em = db.Column(db.DateTime, default=agora, nullable=False)
    finalizado_em = db.Column(db.DateTime)
    acertos = db.Column(db.Integer, default=0, nullable=False)
    total = db.Column(db.Integer, default=0, nullable=False)
    aprovada = db.Column(db.Boolean, default=False, nullable=False)


class TreinoRespostaQuiz(db.Model):
    __tablename__ = 'treino_resposta_quiz'
    __table_args__ = (
        db.UniqueConstraint('tentativa_id', 'questao_id',
                            name='uq_treino_resp_quiz'),
    )
    id = db.Column(db.Integer, primary_key=True)
    tentativa_id = db.Column(
        db.Integer, db.ForeignKey('treino_tentativa_quiz.id'), nullable=False,
        index=True)
    questao_id = db.Column(
        db.Integer, db.ForeignKey('treino_questao.id'), nullable=False)
    alternativa_id = db.Column(
        db.Integer, db.ForeignKey('treino_alternativa.id'))
    correta = db.Column(db.Boolean, default=False, nullable=False)
    segundos_na_questao = db.Column(db.Integer, default=0, nullable=False)
    # Não pontua se abaixo do tempo mínimo (§9.2). Registrada mesmo assim.
    pontuou = db.Column(db.Boolean, default=True, nullable=False)
    respondido_em = db.Column(db.DateTime, default=agora, nullable=False)


# ── Aplicação prática (Fase 4) ──────────────────────────────────────────
class TreinoChecklistAplicacao(db.Model):
    __tablename__ = 'treino_checklist_aplicacao'
    id = db.Column(db.Integer, primary_key=True)
    trilha_id = db.Column(
        db.Integer, db.ForeignKey('treino_trilha.id'), nullable=False,
        index=True)
    descricao = db.Column(db.String(200), nullable=False)
    ativo = db.Column(db.Boolean, default=True, nullable=False)

    itens = db.relationship(
        'TreinoItemChecklist', backref='checklist',
        order_by='TreinoItemChecklist.ordem', cascade='all, delete-orphan')


class TreinoItemChecklist(db.Model):
    __tablename__ = 'treino_item_checklist'
    id = db.Column(db.Integer, primary_key=True)
    checklist_id = db.Column(
        db.Integer, db.ForeignKey('treino_checklist_aplicacao.id'),
        nullable=False, index=True)
    descricao = db.Column(db.String(300), nullable=False)
    ordem = db.Column(db.Integer, default=0, nullable=False)
    # Desativar preserva o item nos registros antigos sem mostrá-lo nas novas
    # observações. Editar a lista nunca apaga silenciosamente o histórico.
    ativo = db.Column(db.Boolean, default=True, nullable=False)


class TreinoAplicacaoPratica(db.Model):
    __tablename__ = 'treino_aplicacao_pratica'
    id = db.Column(db.Integer, primary_key=True)
    funcionario_id = db.Column(
        db.Integer, db.ForeignKey('funcionario.id'), nullable=False, index=True)
    trilha_id = db.Column(
        db.Integer, db.ForeignKey('treino_trilha.id'), nullable=False,
        index=True)
    gestor_id = db.Column(
        db.Integer, db.ForeignKey('funcionario.id'), nullable=False)
    temporada_id = db.Column(
        db.Integer, db.ForeignKey('treino_temporada.id'), nullable=False)
    data = db.Column(db.Date, nullable=False)
    itens_ok = db.Column(db.JSON)              # [item_checklist_id, ...]
    evidencia = db.Column(db.Text, nullable=False)
    status = db.Column(db.String(12), default='REGISTRADA', nullable=False)

    funcionario = db.relationship('Funcionario', foreign_keys=[funcionario_id])
    gestor = db.relationship('Funcionario', foreign_keys=[gestor_id])


# ── Selo / certificado (Fase 7) ─────────────────────────────────────────
class TreinoSelo(db.Model):
    __tablename__ = 'treino_selo'
    id = db.Column(db.Integer, primary_key=True)
    funcionario_id = db.Column(
        db.Integer, db.ForeignKey('funcionario.id'), nullable=False, index=True)
    trilha_id = db.Column(
        db.Integer, db.ForeignKey('treino_trilha.id'), nullable=False,
        index=True)
    emitido_em = db.Column(db.DateTime, default=agora, nullable=False)
    codigo_verificacao = db.Column(
        db.String(32), unique=True, nullable=False,
        default=lambda: uuid.uuid4().hex)
    carga_horaria_minutos = db.Column(db.Integer, default=0, nullable=False)

    funcionario = db.relationship('Funcionario')
    trilha = db.relationship('TreinoTrilha')


# ── Recompensas / resgate (Fase 6) ──────────────────────────────────────
class TreinoRecompensa(db.Model):
    __tablename__ = 'treino_recompensa'
    id = db.Column(db.Integer, primary_key=True)
    nome = db.Column(db.String(150), nullable=False)
    descricao = db.Column(db.Text)
    custo_pontos = db.Column(db.Integer, nullable=False)
    estoque = db.Column(db.Integer)            # NULL = ilimitado
    unidade_id = db.Column(db.Integer, db.ForeignKey('loja.id'))  # NULL = todas
    ativa = db.Column(db.Boolean, default=True, nullable=False)


class TreinoResgate(db.Model):
    __tablename__ = 'treino_resgate'
    id = db.Column(db.Integer, primary_key=True)
    funcionario_id = db.Column(
        db.Integer, db.ForeignKey('funcionario.id'), nullable=False, index=True)
    recompensa_id = db.Column(
        db.Integer, db.ForeignKey('treino_recompensa.id'), nullable=False)
    temporada_id = db.Column(
        db.Integer, db.ForeignKey('treino_temporada.id'), nullable=False)
    pontos_debitados = db.Column(db.Integer, default=0, nullable=False)
    status = db.Column(db.String(12), default='SOLICITADO', nullable=False)
    # Evento de débito no ledger (setado na APROVAÇÃO). Amarra estorno no cancel.
    evento_debito_id = db.Column(
        db.Integer, db.ForeignKey('treino_evento_pontos.id'))
    solicitado_em = db.Column(db.DateTime, default=agora, nullable=False)
    decidido_em = db.Column(db.DateTime)
    decidido_por_id = db.Column(db.Integer, db.ForeignKey('usuario.id'))

    funcionario = db.relationship('Funcionario')
    recompensa = db.relationship('TreinoRecompensa')


# ── Fechamento semanal (Fase 8) ─────────────────────────────────────────
class TreinoFechamentoSemanal(db.Model):
    __tablename__ = 'treino_fechamento_semanal'
    __table_args__ = (
        db.UniqueConstraint('funcionario_id', 'ano', 'semana_iso',
                            name='uq_treino_fechamento'),
    )
    id = db.Column(db.Integer, primary_key=True)
    funcionario_id = db.Column(
        db.Integer, db.ForeignKey('funcionario.id'), nullable=False, index=True)
    ano = db.Column(db.Integer, nullable=False)
    semana_iso = db.Column(db.Integer, nullable=False)
    meta_cumprida = db.Column(db.Boolean, default=False, nullable=False)
    semanas_consecutivas = db.Column(db.Integer, default=0, nullable=False)
    processado_em = db.Column(db.DateTime, default=agora, nullable=False)


# ── Onboarding por cargo / progressão (v2 §16.1, §16.3) ─────────────────
class TreinoTrilhaCargo(db.Model):
    """Mapeia quais TRILHAS são exigidas por CARGO. Serve pro onboarding
    automático (na admissão, o funcionário já vê as trilhas do cargo dele) E
    pra progressão (apto ao cargo = concluiu — tem selo — as trilhas exigidas).
    Tabela nova (db.create_all)."""
    __tablename__ = 'treino_trilha_cargo'
    __table_args__ = (
        db.UniqueConstraint('trilha_id', 'cargo_id',
                            name='uq_treino_trilha_cargo'),
    )
    id = db.Column(db.Integer, primary_key=True)
    trilha_id = db.Column(
        db.Integer, db.ForeignKey('treino_trilha.id'), nullable=False,
        index=True)
    cargo_id = db.Column(
        db.Integer, db.ForeignKey('cargo.id'), nullable=False, index=True)
    obrigatoria = db.Column(db.Boolean, default=True, nullable=False)

    trilha = db.relationship('TreinoTrilha')
    cargo = db.relationship('Cargo')
