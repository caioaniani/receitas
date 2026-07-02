"""API read-only do assistente (/api/claude/*) — token via CLAUDE_API_TOKEN.

Sem env → 503 (desligada). Token errado → 401. Token certo → JSON do
cronograma de produção (a mesma conta da tela /telaindustriateste).
"""
from datetime import timedelta

from app.extensions import db
from app.models import Loja, PedidoItem, PedidoLoja, Receita
from app.utils import hoje

TOKEN = 'token-de-teste-bem-longo-123'


def _seed(nome='Sourdough'):
    loja = Loja(nome='Loja A', ativa=True)
    r = Receita(nome=nome, categoria='Paes', rendimento_qtd=1,
                rendimento_unidade='un', peso_base=1000.0)
    db.session.add_all([loja, r])
    db.session.commit()
    p = PedidoLoja(loja_id=loja.id, status='pendente',
                   data_entrega=hoje() + timedelta(days=2),
                   data_pedido=hoje())
    db.session.add(p)
    db.session.flush()
    db.session.add(PedidoItem(pedido_id=p.id, receita_id=r.id, quantidade=40))
    db.session.commit()
    return r


def test_sem_env_responde_503(app):
    app.config['CLAUDE_API_TOKEN'] = ''
    resp = app.test_client().get('/api/claude/cronograma')
    assert resp.status_code == 503


def test_token_errado_401(app):
    app.config['CLAUDE_API_TOKEN'] = TOKEN
    resp = app.test_client().get(
        '/api/claude/cronograma',
        headers={'Authorization': 'Bearer errado'})
    assert resp.status_code == 401


def test_token_certo_devolve_cronograma(app):
    app.config['CLAUDE_API_TOKEN'] = TOKEN
    r = _seed()
    resp = app.test_client().get(
        '/api/claude/cronograma?horizonte=7&inicio=0',
        headers={'Authorization': f'Bearer {TOKEN}'})
    assert resp.status_code == 200
    d = resp.get_json()
    assert d['ok'] is True
    rr = next(x for x in d['receitas'] if x['receita_id'] == r.id)
    assert rr['nome'] == 'Sourdough'
    assert sum(c['qtd'] for c in rr['por_dia']) == 40
    assert len(d['dias']) == 7


def test_token_via_query_tambem_funciona(app):
    app.config['CLAUDE_API_TOKEN'] = TOKEN
    _seed('Baguete')
    resp = app.test_client().get(f'/api/claude/cronograma?token={TOKEN}')
    assert resp.status_code == 200
    assert resp.get_json()['ok'] is True


def test_nao_exige_login_mas_exige_token(app):
    """Rota fora do login_manager (integração por token): sem token → 401,
    nunca redirect pra /auth/login."""
    app.config['CLAUDE_API_TOKEN'] = TOKEN
    resp = app.test_client().get('/api/claude/cronograma')
    assert resp.status_code == 401
