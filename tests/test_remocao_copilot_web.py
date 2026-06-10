"""Copilot WEB desativado em 10/06/2026: FAB/painel/rotas /copilot/api/*
fora; servico copilot.py + canais Slack e WhatsApp 'power' continuam."""


def _login(c):
    c.post('/auth/login', data={'login': 'admin', 'senha': '123'})


def test_endpoints_web_do_copilot_sumiram(app, admin_user):
    c = app.test_client()
    _login(c)
    assert c.get('/copilot/api/lojas').status_code == 404
    assert c.post('/copilot/api/interpretar',
                  json={'mensagem': 'oi'}).status_code == 404


def test_base_sem_fab_e_sem_script_copilot(app, admin_user):
    c = app.test_client()
    _login(c)
    r = c.get('/')
    assert b'id="copilot-fab"' not in r.data
    assert b'copilot.js' not in r.data


def test_servico_copilot_continua_disponivel(app):
    """O bot do Slack importa este servico — nao pode ter sumido."""
    from app.services import copilot as copilot_svc
    assert hasattr(copilot_svc, 'interpretar')
    assert hasattr(copilot_svc, 'executar')
