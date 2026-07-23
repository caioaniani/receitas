"""Fase 2 — anti-fraude do vídeo (§9.1, critérios de aceite 1, 2, 3).

O relógio do servidor é simulado por um Clock controlável (monkeypatch de
treino_video.agora) pra exercitar os deltas reais entre heartbeats.
"""
from datetime import datetime, timedelta

from app.extensions import db
from app.models import (
    Funcionario,
    Loja,
    TreinoCheckpoint,
    TreinoTemporada,
    TreinoTrilha,
    TreinoVideo,
)
from app.services import treino_ledger as ledger
from app.services import treino_video as tv
from app.utils import hoje


class Clock:
    def __init__(self, start):
        self.t = start

    def __call__(self):
        return self.t

    def tick(self, secs):
        self.t += timedelta(seconds=secs)


def _setup(dur=100, com_checkpoint=False):
    temp = TreinoTemporada(nome='T', inicio=hoje() - timedelta(days=1),
                           fim=hoje() + timedelta(days=30), status='ATIVA')
    loja = Loja(nome='Brooklin', ativa=True)
    db.session.add_all([temp, loja])
    db.session.commit()
    f = Funcionario(nome='Ana', cpf='111.111.111-11')
    f.lojas.append(loja)
    trilha = TreinoTrilha(nome='Seg. Alimentar')
    db.session.add_all([f, trilha])
    db.session.commit()
    v = TreinoVideo(trilha_id=trilha.id, titulo='Aula 1',
                    duracao_segundos=dur, versao=1)
    db.session.add(v)
    db.session.commit()
    cp = None
    if com_checkpoint:
        cp = TreinoCheckpoint(video_id=v.id, segundo=30,
                              enunciado='Lava a mão?',
                              alternativas=['Sim', 'Não'], indice_correto=0)
        db.session.add(cp)
        db.session.commit()
    return temp, f, v, cp


def _assistir_ate_fim(f, v, clock, monkeypatch, velocidade=1.0, passo=15):
    monkeypatch.setattr(tv, 'agora', clock)
    r = None
    pos = 0
    r = tv.heartbeat(f, v, 0, velocidade)          # baseline
    while pos < v.duracao_segundos:
        clock.tick(15)
        pos = min(v.duracao_segundos, pos + int(15 * velocidade))
        r = tv.heartbeat(f, v, pos, velocidade)
    return r


def test_pular_pro_fim_nao_conclui(app, monkeypatch):   # critério 2
    with app.app_context():
        temp, f, v, _ = _setup(dur=100)
        clock = Clock(datetime(2026, 7, 23, 10, 0, 0))
        monkeypatch.setattr(tv, 'agora', clock)
        tv.heartbeat(f, v, 0)          # baseline
        clock.tick(15)
        r = tv.heartbeat(f, v, 98)     # SALTO pro fim
        assert r['concluido'] is False
        assert r['pct'] < 20           # o salto não credita o meio
        assert ledger.saldo(f.id, temp.id) == 0


def test_assistir_normal_conclui_e_credita(app, monkeypatch):   # base do 1
    with app.app_context():
        temp, f, v, _ = _setup(dur=100)
        clock = Clock(datetime(2026, 7, 23, 10, 0, 0))
        r = _assistir_ate_fim(f, v, clock, monkeypatch)
        assert r['concluido'] is True
        assert ledger.saldo(f.id, temp.id) == 10   # VIDEO_CONCLUIDO


def test_sem_responder_checkpoint_nao_conclui(app, monkeypatch):   # critério 1
    with app.app_context():
        temp, f, v, cp = _setup(dur=100, com_checkpoint=True)
        clock = Clock(datetime(2026, 7, 23, 10, 0, 0))
        r = _assistir_ate_fim(f, v, clock, monkeypatch)
        assert r['concluido'] is False        # checkpoint pendente barra
        assert ledger.saldo(f.id, temp.id) == 0
        # responde o checkpoint -> agora conclui no próximo heartbeat
        tv.responder_checkpoint(f, cp, 0)      # correta (+5)
        clock.tick(15)
        r2 = tv.heartbeat(f, v, v.duracao_segundos)
        assert r2['concluido'] is True
        # 5 (checkpoint) + 10 (vídeo)
        assert ledger.saldo(f.id, temp.id) == 15


def test_2x_nao_conclui_cedo(app, monkeypatch):   # critério 3
    with app.app_context():
        temp, f, v, _ = _setup(dur=100)
        clock = Clock(datetime(2026, 7, 23, 10, 0, 0))
        r = _assistir_ate_fim(f, v, clock, monkeypatch, velocidade=2.0)
        # cobriu o conteúdo, mas o tempo real contado (delta/velocidade) é
        # metade -> não atinge 0,8×duração -> NÃO conclui.
        assert r['pct'] >= 90
        assert r['concluido'] is False
        assert ledger.saldo(f.id, temp.id) == 0


def test_sem_duracao_nao_reporta_100(app, monkeypatch):   # bug do -1/0
    """Duração <=0 (Cloudflare ainda processando / devolveu -1) NÃO pode dar
    100% no 1º balde (total_baldes=1) nem concluir."""
    with app.app_context():
        temp, f, v, _ = _setup(dur=0)     # duração desconhecida
        clock = Clock(datetime(2026, 7, 23, 10, 0, 0))
        monkeypatch.setattr(tv, 'agora', clock)
        tv.heartbeat(f, v, 0)             # baseline
        clock.tick(15)
        r = tv.heartbeat(f, v, 10)        # assistiu 10s
        assert r['pct'] == 0              # não dispara 100 falso
        assert r['concluido'] is False
        assert ledger.saldo(f.id, temp.id) == 0


def test_checkpoint_idempotente_e_pontua_uma_vez(app, monkeypatch):
    with app.app_context():
        temp, f, v, cp = _setup(dur=100, com_checkpoint=True)
        r1 = tv.responder_checkpoint(f, cp, 0)
        assert r1['correta'] is True and r1['ja_respondido'] is False
        r2 = tv.responder_checkpoint(f, cp, 1)     # tenta de novo
        assert r2['ja_respondido'] is True
        assert ledger.saldo(f.id, temp.id) == 5    # só uma vez
