"""Validacao timing-safe do token do webhook Z-API (`?k=<TOKEN>`).

`==` em Python sai cedo no 1o caractere diferente — atacante pode medir
o microtempo e adivinhar o token. compare_digest sempre demora o mesmo.
Mesmo padrao do crm/routes.py:168 (Chatwoot webhook)."""


def test_token_invalido_retorna_403(app):
    app.config['ZAPI_BOT_WEBHOOK_TOKEN'] = 'abc123segredo'
    app.config['ZAPI_BOT_DONO_NUMERO'] = '5511999990000'
    c = app.test_client()
    r = c.post('/zapi/webhook?k=errado', json={})
    assert r.status_code == 403


def test_token_valido_passa(app):
    app.config['ZAPI_BOT_WEBHOOK_TOKEN'] = 'abc123segredo'
    app.config['ZAPI_BOT_DONO_NUMERO'] = '5511999990000'
    c = app.test_client()
    # Token correto chega no handler (responde 200 mesmo com payload vazio
    # — handler ignora e retorna ok).
    r = c.post('/zapi/webhook?k=abc123segredo', json={})
    assert r.status_code == 200


def test_token_ausente_rejeitado(app):
    app.config['ZAPI_BOT_WEBHOOK_TOKEN'] = 'abc123segredo'
    c = app.test_client()
    r = c.post('/zapi/webhook', json={})
    assert r.status_code == 403


def test_token_vazio_no_config_rejeita_qualquer_coisa(app):
    """Se ZAPI_BOT_WEBHOOK_TOKEN nao foi configurado, NENHUM webhook
    pode passar — fail-closed."""
    app.config['ZAPI_BOT_WEBHOOK_TOKEN'] = ''
    c = app.test_client()
    r = c.post('/zapi/webhook?k=qualquercoisa', json={})
    assert r.status_code == 403


def test_implementacao_usa_compare_digest(app):
    """Trava de regressao estatica: ninguem pode voltar pra `==`."""
    import pathlib
    src = pathlib.Path('app/blueprints/zapi_bot/routes.py').read_text()
    # nao pode mais ter o `provided == expected` no _token_ok
    assert 'compare_digest' in src, \
        'voltou pra == ; vetor de timing attack reaberto'
