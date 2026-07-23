"""Fase 2 — vídeo com ANTI-FRAUDE (§9.1).

O tempo assistido é medido pelo RELÓGIO DO SERVIDOR entre heartbeats, não pelo
que o cliente afirma. Salto pra frente na timeline não conta. Velocidade >1,25×
conta tempo proporcionalmente reduzido (não dá pra "concluir" acelerando).
Conclusão credita `VIDEO_CONCLUIDO` (+ `CHECKPOINT_CORRETO`) pelo ledger só com:
percentual ≥90% E tempo_real ≥0,8×duração E TODOS os checkpoints respondidos.
"""
import json
import math

from app.extensions import db
from app.models import (
    TreinoProgressoVideo,
    TreinoRespostaCheckpoint,
)
from app.services import treino_ledger as ledger
from app.services import treino_pontos as cfg
from app.utils import agora

BUCKET_SEG = 5                 # granularidade de "conteúdo assistido"
INTERVALO_MIN_HEARTBEAT = 10   # heartbeat com intervalo real < 10s é descartado
LIMIAR_PERCENTUAL = 0.90       # §9.1: ≥90% do vídeo
LIMIAR_TEMPO = 0.80            # §9.1: ≥0,8×duração em tempo real


def resolver_funcionario(usuario):
    """current_user (Usuario de login) -> Funcionario do RH (backref). None se
    a conta não estiver vinculada a um funcionário."""
    return getattr(usuario, 'funcionario', None)


def _progresso(funcionario_id, video):
    p = TreinoProgressoVideo.query.filter_by(
        funcionario_id=funcionario_id, video_id=video.id,
        versao_video=video.versao).first()
    if p is not None:
        return p
    from sqlalchemy.exc import IntegrityError
    try:
        with db.session.begin_nested():
            p = TreinoProgressoVideo(
                funcionario_id=funcionario_id, video_id=video.id,
                versao_video=video.versao)
            db.session.add(p)
        return p
    except IntegrityError:
        return TreinoProgressoVideo.query.filter_by(
            funcionario_id=funcionario_id, video_id=video.id,
            versao_video=video.versao).first()


def _baldes(p):
    try:
        return set(json.loads(p.baldes_json or '[]'))
    except (ValueError, TypeError):
        return set()


def _todos_checkpoints_respondidos(funcionario_id, video):
    ids = [c.id for c in video.checkpoints]
    if not ids:
        return True
    respondidos = {r.checkpoint_id for r in
                   TreinoRespostaCheckpoint.query.filter(
                       TreinoRespostaCheckpoint.funcionario_id == funcionario_id,
                       TreinoRespostaCheckpoint.checkpoint_id.in_(ids)).all()}
    return set(ids).issubset(respondidos)


def heartbeat(funcionario, video, posicao_segundos, velocidade=1.0):
    """Processa um heartbeat do player. Mede o tempo REAL pelo relógio do
    servidor (entre este e o último heartbeat); descarta intervalos < 10s;
    ignora saltos pra frente; velocidade >1,25× reduz o tempo contado. Credita
    conclusão quando os 3 gates batem. Retorna {pct, tempo, concluido}."""
    p = _progresso(funcionario.id, video)
    if p.concluido_em:
        return {'pct': 100, 'tempo': p.tempo_real_decorrido, 'concluido': True}

    dur = int(video.duracao_segundos or 0)
    pos = max(0, int(posicao_segundos or 0))
    try:
        vel = float(velocidade or 1.0)
    except (TypeError, ValueError):
        vel = 1.0
    agora_dt = agora()

    if p.ultimo_heartbeat_em is not None:
        delta_real = (agora_dt - p.ultimo_heartbeat_em).total_seconds()
        if delta_real < INTERVALO_MIN_HEARTBEAT:
            return {'pct': _pct(p, dur), 'tempo': p.tempo_real_decorrido,
                    'concluido': False}       # anti-spam: descartado
        fator = vel if vel > 1.25 else 1.0
        avanco_real = pos - p.ultima_posicao
        avanco_esperado = delta_real * fator
        # Reprodução contínua (inclui re-assistir trecho anterior): credita
        # tempo e os baldes percorridos. Salto pra frente: NÃO credita.
        if -1 <= avanco_real <= avanco_esperado * 1.5 + BUCKET_SEG:
            p.tempo_real_decorrido = int(
                (p.tempo_real_decorrido or 0) + delta_real / fator)
            baldes = _baldes(p)
            ini = min(p.ultima_posicao, pos)
            for s in range(ini, pos + 1, BUCKET_SEG):
                baldes.add(s // BUCKET_SEG)
            baldes.add(pos // BUCKET_SEG)
            p.baldes_json = json.dumps(sorted(baldes))

    p.ultima_posicao = pos
    p.ultimo_heartbeat_em = agora_dt
    if dur <= 0:
        # Duração ainda desconhecida (Cloudflare processando / devolveu -1).
        # Sem ela NÃO dá pra medir cobertura — NÃO reporta 100% nem conclui
        # (senão o 1º balde já daria 100%, bug real). A rota do vídeo busca a
        # duração; quando vier positiva, o progresso passa a valer de fato.
        db.session.commit()
        return {'pct': 0, 'tempo': p.tempo_real_decorrido, 'concluido': False}
    # segundos_assistidos / percentual derivados dos baldes cobertos.
    total_baldes = max(1, math.ceil(dur / BUCKET_SEG))
    cobertos = len(_baldes(p))
    p.segundos_assistidos = cobertos * BUCKET_SEG
    p.percentual = min(100, round(100 * cobertos / total_baldes, 2))

    concluido = _tentar_concluir(funcionario, video, p, dur, total_baldes)
    db.session.commit()
    return {'pct': float(p.percentual), 'tempo': p.tempo_real_decorrido,
            'concluido': concluido}


def _pct(p, dur):
    return float(p.percentual or 0)


def _tentar_concluir(funcionario, video, p, dur, total_baldes):
    """Aplica os 3 gates do §9.1 e credita a conclusão pelo ledger (idempotente
    pela referência video/id). Retorna True se está concluído."""
    if p.concluido_em:
        return True
    cobertura = len(_baldes(p)) / total_baldes
    tempo_ok = dur <= 0 or (p.tempo_real_decorrido or 0) >= LIMIAR_TEMPO * dur
    checkpoints_ok = _todos_checkpoints_respondidos(funcionario.id, video)
    if cobertura >= LIMIAR_PERCENTUAL and tempo_ok and checkpoints_ok:
        p.concluido_em = agora()
        ledger.creditar(
            funcionario, 'VIDEO_CONCLUIDO', cfg.valor('VIDEO_CONCLUIDO'),
            referencia_tipo='video', referencia_id=video.id,
            observacao=f'v{video.versao}')
        return True
    return False


def responder_checkpoint(funcionario, checkpoint, indice_escolhido):
    """Registra a resposta do checkpoint (1ª vez só; §4). Se correta, credita
    `CHECKPOINT_CORRETO` pelo ledger (idempotente). Retorna
    {correta, ja_respondido}."""
    ja = TreinoRespostaCheckpoint.query.filter_by(
        funcionario_id=funcionario.id, checkpoint_id=checkpoint.id).first()
    if ja is not None:
        return {'correta': ja.correta, 'ja_respondido': True}
    correta = int(indice_escolhido) == int(checkpoint.indice_correto)
    db.session.add(TreinoRespostaCheckpoint(
        funcionario_id=funcionario.id, checkpoint_id=checkpoint.id,
        indice_escolhido=int(indice_escolhido), correta=correta))
    db.session.commit()
    if correta:
        ledger.creditar(
            funcionario, 'CHECKPOINT_CORRETO', cfg.valor('CHECKPOINT_CORRETO'),
            referencia_tipo='checkpoint', referencia_id=checkpoint.id)
    return {'correta': correta, 'ja_respondido': False}
