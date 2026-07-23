"""v2 §16.2 — geração de perguntas por IA (revisão humana obrigatória).
Anthropic SEMPRE mockada (padrão da casa). A IA só PROPÕE; nada é salvo sem o
gesto humano (o salvar reusa o endpoint de questão, coberto em outro teste).
"""
import json as _json
from unittest.mock import MagicMock, patch

from app.extensions import db
from app.models import TreinoQuiz, TreinoTrilha
from app.services import treino_ia_perguntas as ia

CONTEUDO = ('Higiene de manipuladores de alimentos: lavar as mãos antes de '
            'manusear, usar touca, evitar contaminação cruzada. ' * 3)


def _fake_client(payload):
    blk = MagicMock()
    blk.type = 'text'
    blk.text = _json.dumps(payload)
    resp = MagicMock()
    resp.content = [blk]
    resp.usage = None
    client = MagicMock()
    client.messages.create.return_value = resp
    return client


def _admin(app, admin_user):
    c = app.test_client()
    with c.session_transaction() as s:
        s['_user_id'] = str(admin_user.id)
        s['_fresh'] = True
    return c


def test_gerar_sanitiza_propostas(app, monkeypatch):
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'sk-teste')
    payload = [
        {'enunciado': 'Lavar as mãos quando?', 'dificuldade': 'FACIL',
         'alternativas': ['Antes de manusear', 'Nunca', 'Só no fim', 'Tanto faz'],
         'correta': 0},
        {'enunciado': '', 'alternativas': ['a', 'b'], 'correta': 0},   # descarta
        {'enunciado': 'X?', 'alternativas': ['só uma'], 'correta': 0},  # descarta
        {'enunciado': 'Y?', 'alternativas': ['a', 'b'], 'correta': 9},  # idx ruim
    ]
    with app.app_context(), \
            patch('anthropic.Anthropic', return_value=_fake_client(payload)):
        r = ia.gerar(CONTEUDO, n=5)
    assert 'perguntas' in r and len(r['perguntas']) == 1   # só a válida sobrou
    assert r['perguntas'][0]['correta'] == 0


def test_texto_curto_recusa(app):
    with app.app_context():
        r = ia.gerar('oi')
        assert 'erro' in r


def test_sem_api_key_recusa(app, monkeypatch):
    monkeypatch.delenv('ANTHROPIC_API_KEY', raising=False)
    with app.app_context():
        r = ia.gerar(CONTEUDO)
        assert 'erro' in r


def test_rota_ia_gerar_devolve_propostas(app, admin_user, monkeypatch):
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'sk-teste')
    payload = [{'enunciado': 'Q?', 'alternativas': ['a', 'b', 'c', 'd'],
                'correta': 1, 'dificuldade': 'MEDIA'}]
    with app.app_context():
        t = TreinoTrilha(nome='Seg')
        db.session.add(t)
        db.session.commit()
        q = TreinoQuiz(trilha_id=t.id, titulo='Q')
        db.session.add(q)
        db.session.commit()
        qid = q.id
    c = _admin(app, admin_user)
    with patch('anthropic.Anthropic', return_value=_fake_client(payload)):
        r = c.post(f'/treino/admin/quiz/{qid}/ia-gerar',
                   data={'texto': CONTEUDO, 'n': 3})
    assert r.status_code == 200
    j = r.get_json()
    assert j['ok'] and len(j['perguntas']) == 1 and j['perguntas'][0]['correta'] == 1
