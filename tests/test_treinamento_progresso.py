"""Rastreio REAL de "assistiu tudo?" — o coração do requisito do dono: o
sistema tem que SABER se o funcionário assistiu o vídeo inteiro.

Regra: completa (marca assistido) só quando cobre >=95% dos baldes de tempo E
gastou >=40% da duração em tempo real. Pular pro fim ou varrer a barra NÃO
completa.
"""
from datetime import timedelta

from app.extensions import db
from app.models import Treinamento, Usuario
from app.services import treinamento as svc
from app.utils import agora


def _u(login):
    u = Usuario(nome='F', login=login, papel='funcionario')
    u.set_senha('x' * 8)
    db.session.add(u)
    db.session.commit()
    return u


def _t(**kw):
    t = Treinamento(titulo='V', ativo=True, **kw)
    db.session.add(t)
    db.session.commit()
    return t


def _assistido(uid, tid):
    c = svc.conclusao_de(uid, tid)
    return bool(c and c.assistido_em)


def test_pular_pro_fim_nao_completa(app):
    """Um heartbeat lá no fim cobre 1 balde só — não completa."""
    with app.app_context():
        t, u = _t(), _u('f-skip')
        r = svc.registrar_progresso(t, u, 48, 50)   # vídeo de 50s, posição 48
        assert r['assistido'] is False
        assert r['pct'] < 20
        assert not _assistido(u.id, t.id)


def test_assistir_tudo_completa(app):
    """Cobrir todos os baldes COM tempo real suficiente marca assistido."""
    with app.app_context():
        t, u = _t(), _u('f-full')
        svc.registrar_progresso(t, u, 0, 50)         # cria a linha
        p = svc.progresso_de(u.id, t.id)
        p.iniciado_em = agora() - timedelta(seconds=30)   # passa o portão (>=20s)
        db.session.commit()
        r = None
        for pos in range(0, 50, 5):                  # 0,5,...,45 = 10 baldes
            r = svc.registrar_progresso(t, u, pos, 50)
        assert r['pct'] == 100 and r['assistido'] is True
        assert _assistido(u.id, t.id)


def test_portao_de_tempo_bloqueia_varredura_rapida(app):
    """Cobrir todos os baldes SEM tempo real (arrastar a barra rápido) não
    completa — o portão de tempo pega isso."""
    with app.app_context():
        t, u = _t(), _u('f-fast')
        r = None
        for pos in range(0, 50, 5):
            r = svc.registrar_progresso(t, u, pos, 50)   # iniciado agora = ~0s
        assert r['pct'] == 100          # cobertura cheia...
        assert r['assistido'] is False  # ...mas tempo insuficiente
        assert not _assistido(u.id, t.id)


def test_duracao_autoritativa_do_cloudflare(app, monkeypatch):
    """No Stream a duração vem do Cloudflare — cliente não fura mandando uma
    duração minúscula pra 'completar' com 1 balde."""
    with app.app_context():
        t, u = _t(video_tipo='stream', video_ref='a' * 32), _u('f-cf')
        from app.services import treinamento_stream as ts
        monkeypatch.setattr(ts, 'status',
                            lambda uid: {'duracao': 50, 'pronto': True})
        r = svc.registrar_progresso(t, u, 1, 2)   # cliente MENTE d=2
        p = svc.progresso_de(u.id, t.id)
        assert p.duracao_seg == 50                # usou o do Cloudflare, não 2
        assert r['assistido'] is False            # 1 balde de 10


def test_idempotente_depois_de_completo(app):
    with app.app_context():
        t, u = _t(), _u('f-idem')
        svc.marcar_assistido(t, u)                # já assistido
        r = svc.registrar_progresso(t, u, 3, 50)
        assert r == {'pct': 100, 'assistido': True}


def test_rota_progresso_responde_pct(app):
    with app.app_context():
        t, u = _t(), _u('f-rota')
        uid, tid = u.id, t.id
    c = app.test_client()
    with c.session_transaction() as s:
        s['_user_id'] = str(uid)
        s['_fresh'] = True
    r = c.post(f'/treinamento/{tid}/progresso', data={'t': '10', 'd': '50'})
    assert r.status_code == 200
    j = r.get_json()
    assert j['ok'] and 'pct' in j and j['assistido'] is False


def test_progresso_exige_login(app):
    with app.app_context():
        t = _t()
        tid = t.id
    r = app.test_client().post(f'/treinamento/{tid}/progresso',
                               data={'t': '5', 'd': '50'})
    assert r.status_code in (302, 401)
