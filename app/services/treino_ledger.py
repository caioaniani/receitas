"""Ledger de pontos (§5.1, §9.3) + helpers de unidade/papel.

Regras de ferro:
- Saldo = SUM(pontos) do ledger. NUNCA há coluna de saldo mutável.
- Crédito é IDEMPOTENTE pela chave (funcionário, tipo, referência) — o índice
  único parcial do modelo barra o 2º lançamento (critério 4).
- Correção nunca apaga: é ESTORNO (lançamento negativo com estorno_de_id;
  critério 15).
- Teto diário (§4.2): lançamento que ultrapassa o teto do dia entra com
  pontos=0 e observação — o progresso é preservado, só o ponto não credita.
"""
from datetime import datetime, time, timedelta

from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models import TreinoEventoPontos, TreinoTemporada
from app.services import treino_pontos as cfg
from app.utils import hoje

TIPO_ESTORNO = 'ESTORNO'


# ── Helpers de reuso do que já existe ───────────────────────────────────
def unidade_do_funcionario(funcionario):
    """Unidade (Loja) do funcionário pro registro/ranking. Reusa o vínculo do
    RH (M2M `funcionario.lojas`): a 1ª loja ATIVA (senão a 1ª qualquer, senão
    None). LIMITAÇÃO conhecida: quem está em várias lojas conta na 1ª —
    refinar na fase de ranking se necessário."""
    lojas = list(funcionario.lojas or [])
    for loja in lojas:
        if getattr(loja, 'ativa', True):
            return loja
    return lojas[0] if lojas else None


def papel_treino(usuario):
    """Papel no módulo (FUNCIONARIO/GESTOR/ADMIN) derivado do Usuario de login
    (§5). admin/dono -> ADMIN; gerente -> GESTOR; resto -> FUNCIONARIO."""
    if usuario is None:
        return 'FUNCIONARIO'
    if usuario.is_admin():          # admin ou dono
        return 'ADMIN'
    if usuario.is_gerente() or usuario.lidera_equipe():
        return 'GESTOR'
    return 'FUNCIONARIO'


def temporada_ativa():
    """Temporada em curso (status ATIVA cobrindo hoje). None se não houver."""
    h = hoje()
    return (TreinoTemporada.query
            .filter(TreinoTemporada.status == 'ATIVA',
                    TreinoTemporada.inicio <= h, TreinoTemporada.fim >= h)
            .order_by(TreinoTemporada.inicio.desc()).first())


# ── Saldo (sempre SUM, nunca coluna) ────────────────────────────────────
def saldo(funcionario_id, temporada_id):
    total = (db.session.query(
        db.func.coalesce(db.func.sum(TreinoEventoPontos.pontos), 0))
        .filter(TreinoEventoPontos.funcionario_id == funcionario_id,
                TreinoEventoPontos.temporada_id == temporada_id).scalar())
    return int(total or 0)


def _creditado_hoje(funcionario_id, temporada_id):
    """Soma dos pontos POSITIVOS creditados hoje (BRT) — base do teto diário.
    Usa faixa [meia-noite, meia-noite+1d) pra ser robusto em SQLite e Postgres
    (sem depender de func.date)."""
    inicio = datetime.combine(hoje(), time.min)
    fim = inicio + timedelta(days=1)
    total = (db.session.query(
        db.func.coalesce(db.func.sum(TreinoEventoPontos.pontos), 0))
        .filter(TreinoEventoPontos.funcionario_id == funcionario_id,
                TreinoEventoPontos.temporada_id == temporada_id,
                TreinoEventoPontos.pontos > 0,
                TreinoEventoPontos.criado_em >= inicio,
                TreinoEventoPontos.criado_em < fim).scalar())
    return int(total or 0)


def _existente(funcionario_id, tipo, referencia_tipo, referencia_id):
    return (TreinoEventoPontos.query.filter_by(
        funcionario_id=funcionario_id, tipo=tipo,
        referencia_tipo=referencia_tipo, referencia_id=referencia_id,
        estorno_de_id=None).first())


# ── Crédito (idempotente + teto) ────────────────────────────────────────
def creditar(funcionario, tipo, pontos, *, temporada=None,
             referencia_tipo=None, referencia_id=None, criado_por_id=None,
             observacao=None, unidade_id=None, aplica_teto=True):
    """Credita pontos no ledger. IDEMPOTENTE: já tendo lançamento com a mesma
    chave (funcionário, tipo, referência), devolve o existente sem creditar de
    novo. Aplica o TETO DIÁRIO a créditos positivos (§4.2). Retorna
    (evento, creditou_agora: bool). Levanta ValueError se não há temporada."""
    temp = temporada or temporada_ativa()
    if temp is None:
        raise ValueError('Sem temporada ATIVA — não há onde lançar pontos.')
    if unidade_id is None:
        u = unidade_do_funcionario(funcionario)
        unidade_id = u.id if u else None

    # Idempotência SÓ quando há referência real. Eventos sem referência
    # (AJUSTE_MANUAL) são fatos independentes e podem repetir — o índice único
    # trata NULLs como distintos, então a pré-checagem também precisa pular.
    tem_ref = referencia_id is not None or referencia_tipo is not None
    if tem_ref:
        ja = _existente(funcionario.id, tipo, referencia_tipo, referencia_id)
        if ja is not None:
            return ja, False

    pontos_efetivos = int(pontos)
    obs = observacao
    if aplica_teto and pontos_efetivos > 0:
        teto = cfg.valor('TETO_DIARIO_PONTOS')
        if teto and _creditado_hoje(funcionario.id, temp.id) + pontos_efetivos > teto:
            pontos_efetivos = 0
            obs = f'{observacao} | teto diario atingido' if observacao \
                else 'teto diario atingido'

    ev = TreinoEventoPontos(
        funcionario_id=funcionario.id, unidade_id=unidade_id,
        temporada_id=temp.id, tipo=tipo, referencia_tipo=referencia_tipo,
        referencia_id=referencia_id, pontos=pontos_efetivos,
        criado_por_id=criado_por_id, observacao=obs)
    try:
        with db.session.begin_nested():     # savepoint: corrida cai no unique
            db.session.add(ev)
        db.session.commit()
        return ev, True
    except IntegrityError:
        db.session.rollback()               # perdeu a corrida -> devolve o que há
        return _existente(funcionario.id, tipo, referencia_tipo,
                          referencia_id) if tem_ref else None, False


# ── Estorno (nunca apaga o original) ────────────────────────────────────
def estornar(evento, *, criado_por_id=None, observacao=None):
    """Cria um lançamento de ESTORNO (pontos negativos) referenciando o
    original via estorno_de_id. NUNCA apaga/edita o original (§3.5, critério
    15). Idempotente: já havendo estorno desse evento, devolve-o. Retorna
    (estorno, estornou_agora: bool)."""
    ja = TreinoEventoPontos.query.filter_by(estorno_de_id=evento.id).first()
    if ja is not None:
        return ja, False
    est = TreinoEventoPontos(
        funcionario_id=evento.funcionario_id, unidade_id=evento.unidade_id,
        temporada_id=evento.temporada_id, tipo=TIPO_ESTORNO,
        referencia_tipo=evento.referencia_tipo,
        referencia_id=evento.referencia_id, pontos=-int(evento.pontos or 0),
        criado_por_id=criado_por_id,
        observacao=observacao or f'estorno do evento {evento.id}',
        estorno_de_id=evento.id)
    db.session.add(est)
    db.session.commit()
    return est, True


def ajuste_manual(funcionario, pontos, justificativa, *, criado_por_id,
                  temporada=None, unidade_id=None):
    """Ajuste manual de pontos (§4, tipo AJUSTE_MANUAL) — só admin, exige
    justificativa. Fora do teto diário. Não é idempotente (cada ajuste é um
    fato novo): referência nula, o índice permite múltiplos."""
    if not (justificativa or '').strip():
        raise ValueError('Ajuste manual exige justificativa.')
    return creditar(
        funcionario, 'AJUSTE_MANUAL', pontos, temporada=temporada,
        referencia_tipo=None, referencia_id=None, criado_por_id=criado_por_id,
        observacao=justificativa.strip(), unidade_id=unidade_id,
        aplica_teto=False)
