"""Alertas do Vigia no painel de entregas (15/06/2026).

Banner + som "chato" + aba lateral com histórico e link do Chatwoot.
- api_painel devolve resumo `vigia` (pendentes + último) → dirige banner/som.
- POST .../vigia/reconhecer → marca reconhecido (para o som em todos os
  aparelhos).
- GET .../vigia/historico → lista pro drawer com link do Chatwoot.

Os alertas são VigiaVeredito com alerta=True, gravidade='alta' — a MESMA
fonte que dispara o WhatsApp. Banner = pendente (não reconhecido + janela).
"""
from datetime import timedelta

import pytest


@pytest.fixture
def admin_logado(app):
    from app.extensions import db
    from app.models import Usuario
    u = Usuario(nome='Gerente', login='ger', papel='admin')
    u.set_senha('x' * 8)
    db.session.add(u)
    db.session.commit()
    client = app.test_client()
    with client.session_transaction() as sess:
        sess['_user_id'] = str(u.id)
        sess['_fresh'] = True
    return client


def _alerta(app, **kw):
    """Cria um VigiaVeredito de alerta. Defaults = alta/pendente/agora."""
    from app.extensions import db
    from app.models import VigiaVeredito
    from app.utils import agora
    base = dict(conv_id='115', cliente='Bruna', alerta=True, gravidade='alta',
                motivo_vigia='Handoff preguiçoso em consulta de pedido',
                mensagem_cliente='cadê meu pedido?', criado_em=agora())
    base.update(kw)
    v = VigiaVeredito(**base)
    db.session.add(v)
    db.session.commit()
    return v


# ── Resumo no api_painel ────────────────────────────────────────────────

def test_api_painel_inclui_resumo_vigia(app, admin_logado):
    from unittest.mock import patch
    with app.app_context():
        _alerta(app)
    with patch('app.services.vnda.buscar_pedidos_do_dia',
               return_value={'pedidos': []}):
        d = admin_logado.get('/entregas/api/painel').get_json()
    assert 'vigia' in d
    assert d['vigia']['pendentes'] == 1
    assert d['vigia']['ultimo']['cliente'] == 'Bruna'
    assert 'Handoff' in d['vigia']['ultimo']['motivo']


def test_api_painel_so_conta_alta_pendente(app, admin_logado):
    """media, reconhecido e antigo NÃO entram nos pendentes do banner."""
    from unittest.mock import patch

    from app.utils import agora
    with app.app_context():
        _alerta(app, gravidade='media')                       # media → fora
        _alerta(app, reconhecido_em=agora())                  # já visto → fora
        _alerta(app, criado_em=agora() - timedelta(hours=20))  # velho → fora
        _alerta(app)                                          # esse conta
    with patch('app.services.vnda.buscar_pedidos_do_dia',
               return_value={'pedidos': []}):
        d = admin_logado.get('/entregas/api/painel').get_json()
    assert d['vigia']['pendentes'] == 1


def test_api_painel_sem_alertas_zera(app, admin_logado):
    from unittest.mock import patch
    with patch('app.services.vnda.buscar_pedidos_do_dia',
               return_value={'pedidos': []}):
        d = admin_logado.get('/entregas/api/painel').get_json()
    assert d['vigia']['pendentes'] == 0
    assert d['vigia']['ultimo'] is None


# ── Reconhecer (clique no banner para o som) ────────────────────────────

def test_reconhecer_marca_todos_pendentes(app, admin_logado):
    from app.models import VigiaVeredito
    with app.app_context():
        _alerta(app)
        _alerta(app, conv_id='200', cliente='Ka')
    r = admin_logado.post('/entregas/api/painel/vigia/reconhecer', json={})
    assert r.get_json()['reconhecidos'] == 2
    with app.app_context():
        pend = VigiaVeredito.query.filter(
            VigiaVeredito.reconhecido_em.is_(None)).count()
        assert pend == 0


def test_reconhecer_registra_quem_clicou(app, admin_logado):
    from app.models import VigiaVeredito
    with app.app_context():
        _alerta(app)
    admin_logado.post('/entregas/api/painel/vigia/reconhecer', json={})
    with app.app_context():
        v = VigiaVeredito.query.first()
        assert v.reconhecido_em is not None
        assert v.reconhecido_por_id is not None


def test_reconhecer_idempotente(app, admin_logado):
    """2º clique não acha mais pendentes — não quebra."""
    with app.app_context():
        _alerta(app)
    admin_logado.post('/entregas/api/painel/vigia/reconhecer', json={})
    r2 = admin_logado.post('/entregas/api/painel/vigia/reconhecer', json={})
    assert r2.get_json()['reconhecidos'] == 0


# ── Histórico (aba lateral) ─────────────────────────────────────────────

def test_historico_inclui_reconhecidos_e_link_chatwoot(app, admin_logado):
    from app.utils import agora
    with app.app_context():
        app.config['CHATWOOT_URL'] = 'https://atendimento.opao.com.br'
        app.config['CHATWOOT_ACCOUNT_ID'] = '1'
        _alerta(app, conv_id='115')
        _alerta(app, conv_id='200', reconhecido_em=agora())
    d = admin_logado.get('/entregas/api/painel/vigia/historico').get_json()
    assert len(d['alertas']) == 2
    # link montado pra conv numérica
    urls = [a['chatwoot_url'] for a in d['alertas']]
    assert any('/conversations/115' in (u or '') for u in urls)
    # inclui o já reconhecido (histórico ≠ pendente)
    assert any(a['reconhecido'] for a in d['alertas'])


def test_historico_conv_nao_numerica_sem_link(app, admin_logado):
    """Alerta de teste (conv_id 'teste-x') não vira link quebrado."""
    with app.app_context():
        app.config['CHATWOOT_URL'] = 'https://atendimento.opao.com.br'
        app.config['CHATWOOT_ACCOUNT_ID'] = '1'
        _alerta(app, conv_id='teste-estoque')
    d = admin_logado.get('/entregas/api/painel/vigia/historico').get_json()
    assert d['alertas'][0]['chatwoot_url'] == ''


def test_link_chatwoot_sem_config_vazio(app):
    from app.services import chatbot_vigia
    with app.app_context():
        app.config.pop('CHATWOOT_URL', None)
        assert chatbot_vigia.link_chatwoot('115') == ''


# ── Permissão + robustez ────────────────────────────────────────────────

def test_endpoints_exigem_login(app):
    c = app.test_client()
    assert c.get('/entregas/api/painel/vigia/historico').status_code in (302, 401)
    assert c.post('/entregas/api/painel/vigia/reconhecer',
                  json={}).status_code in (302, 401)


def test_painel_html_tem_banner_e_drawer(admin_logado):
    # Swap 26/06/2026: o banner/drawer do vigia (v1) virou /painel-testes;
    # /painel agora e o v2 (atendimento + pedidos embutidos).
    r = admin_logado.get('/entregas/painel-testes')
    assert r.status_code == 200
    assert b'vigia-banner' in r.data
    assert b'drawer' in r.data
    assert b'Alertas do atendimento' in r.data
    # som klaxon presente
    assert b'klaxon' in r.data


def test_api_painel_resiliente_a_falha_do_vigia(app, admin_logado):
    """Se o resumo do vigia explodir, o painel de pedidos não pode cair."""
    from unittest.mock import patch
    with patch('app.services.vnda.buscar_pedidos_do_dia',
               return_value={'pedidos': []}), \
         patch('app.services.chatbot_vigia.alertas_pendentes_resumo',
               side_effect=RuntimeError('boom')):
        r = admin_logado.get('/entregas/api/painel')
    assert r.status_code == 200
    d = r.get_json()
    assert d['vigia']['pendentes'] == 0  # fallback seguro
