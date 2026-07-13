"""Rota de saude do Seru (/pdv/debug-seru, owner): testa auth + 1 request e
mostra o erro REAL da API pra diagnosticar falha de busca/sync, sem vazar
segredo. Somente leitura."""
from unittest.mock import patch


def _login(app, user):
    c = app.test_client()
    c.post('/auth/login', data={'login': user.login, 'senha': '123'},
           follow_redirects=True)
    return c


def test_debug_seru_api_ok(app, owner_user):
    """Auth + request OK -> conclusao aponta pro navegador, nao pra API."""
    app.config['SERU_CLIENT_ID'] = 'cid-teste'
    app.config['SERU_CLIENT_SECRET'] = 'secret-super-secreto'
    c = _login(app, owner_user)
    with patch('app.services.seru._obter_token', return_value='tok-123'), \
         patch('app.services.seru.listar_pedidos',
               return_value={'totalPages': 3, 'data': [{'id': 1}]}):
        r = c.get('/pdv/debug-seru')
    assert r.status_code == 200
    j = r.get_json()
    assert j['auth']['ok'] is True
    assert j['request']['ok'] is True
    assert j['request']['n_no_page'] == 1
    assert 'navegador' in j['conclusao'].lower()
    # NAO vaza o segredo — so presenca/tamanho
    assert 'secret-super-secreto' not in r.get_data(as_text=True)
    assert j['config']['client_secret_set'] is True
    assert j['config']['client_secret_len'] == len('secret-super-secreto')


def test_debug_seru_auth_falha(app, owner_user):
    """Auth falha -> conclusao aponta pras credenciais; nao tenta o request."""
    c = _login(app, owner_user)
    with patch('app.services.seru._obter_token',
               side_effect=RuntimeError('Seru auth 401: bad creds')), \
         patch('app.services.seru.listar_pedidos') as m_lp:
        r = c.get('/pdv/debug-seru')
    j = r.get_json()
    assert j['auth']['ok'] is False
    assert 'auth 401' in j['auth']['erro']
    assert j['request'] is None                 # nao tentou o request
    assert 'autentica' in j['conclusao'].lower()
    m_lp.assert_not_called()


def test_debug_seru_request_falha_mostra_erro_real(app, owner_user):
    """Auth OK mas request falha -> expoe o erro REAL da API."""
    c = _login(app, owner_user)
    with patch('app.services.seru._obter_token', return_value='tok'), \
         patch('app.services.seru.listar_pedidos',
               side_effect=RuntimeError('Seru /orders 500: upstream boom')):
        r = c.get('/pdv/debug-seru')
    j = r.get_json()
    assert j['auth']['ok'] is True
    assert j['request']['ok'] is False
    assert 'upstream boom' in j['request']['erro']
    assert 'erro real' in j['conclusao'].lower()


def test_debug_seru_bloqueia_nao_owner(app, admin_user):
    """Admin comum (nao owner) nao acessa."""
    c = _login(app, admin_user)
    r = c.get('/pdv/debug-seru', follow_redirects=False)
    assert r.status_code in (302, 403)


def test_sonda_seru_debug_na_api_do_assistente(app, monkeypatch):
    """/api/claude/seru-debug: mesma função do /pdv/debug-seru (fonte
    única), com Bearer — criada quando a API do Seru caiu e o container
    do assistente não alcançava o host."""
    from unittest.mock import patch
    app.config['CLAUDE_API_TOKEN'] = 'tok-sonda'
    c = app.test_client()
    assert c.get('/api/claude/seru-debug').status_code == 401
    with patch('app.services.seru._obter_token', return_value='t' * 20), \
         patch('app.services.seru.listar_pedidos',
               return_value={'data': [], 'totalPages': 1}):
        resp = c.get('/api/claude/seru-debug',
                     headers={'Authorization': 'Bearer tok-sonda'})
    d = resp.get_json()
    assert resp.status_code == 200 and d['ok'] is True
    assert d['auth']['ok'] is True and d['request']['ok'] is True
