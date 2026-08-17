"""Shell de navegação exclusivo do ambiente visual de preview."""


def _login(app, admin_user):
    client = app.test_client()
    client.post('/auth/login', data={
        'login': admin_user.login,
        'senha': '123',
    })
    return client


def test_preview_mode_usa_navegacao_reduzida(app, admin_user):
    app.config['PREVIEW_MODE'] = True
    html = _login(app, admin_user).get('/area/producao').get_data(as_text=True)

    assert 'ui-v2-sidebar' in html
    assert 'Planejamento da indústria' in html
    assert 'ui-v2.css' in html
    assert 'Fichas Técnicas' not in html


def test_producao_mantem_shell_atual_fora_do_preview(app, admin_user):
    app.config['PREVIEW_MODE'] = False
    html = _login(app, admin_user).get('/area/producao').get_data(as_text=True)

    assert 'ui-v2-sidebar' not in html
    assert 'Fichas Técnicas' in html
