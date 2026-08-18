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


def test_preview_preserva_contrato_do_modo_embed(app, admin_user):
    app.config['PREVIEW_MODE'] = True
    html = _login(app, admin_user).get(
        '/admin/loja-online/pedidos/inexistente?embed=1'
    ).get_data(as_text=True)

    assert '<body class="embed-mode">' in html
    assert '<body class="embed-mode ui-v2">' not in html


def test_preview_home_usa_nova_hierarquia_sem_alterar_dados(app, admin_user):
    app.config['PREVIEW_MODE'] = True
    html = _login(app, admin_user).get('/').get_data(as_text=True)

    assert 'home-v2' in html
    assert 'Precisa de você hoje' in html
    assert 'Áreas de trabalho' in html
    assert 'Planejar produção' in html
    assert 'menu-grid' not in html
    assert 'css/ui-v2.css' in html


def test_home_legada_permanece_fora_do_preview(app, admin_user):
    app.config['PREVIEW_MODE'] = False
    html = _login(app, admin_user).get('/').get_data(as_text=True)

    assert 'home-v2' not in html
    assert 'menu-grid' in html
    assert 'Escolha uma área para continuar.' in html


def test_preview_area_usa_mesma_entrada_em_todas_as_equipes(app, admin_user):
    app.config['PREVIEW_MODE'] = True
    client = _login(app, admin_user)

    for slug, titulo in (
        ('lojas', 'Lojas'),
        ('producao', 'Produção'),
        ('catalogo', 'Catálogo'),
        ('vendas', 'Vendas &amp; Entregas'),
        ('financeiro', 'Financeiro'),
        ('relatorios', 'Relatórios'),
        ('administracao', 'Administração'),
    ):
        html = client.get(f'/area/{slug}').get_data(as_text=True)
        assert 'area-v2' in html
        assert 'O que você quer fazer?' in html
        assert 'area-v2-link' in html
        assert titulo in html
        assert 'area-wrap' not in html


def test_area_legada_permanece_fora_do_preview(app, admin_user):
    app.config['PREVIEW_MODE'] = False
    html = _login(app, admin_user).get('/area/producao').get_data(as_text=True)

    assert 'area-wrap' in html
    assert 'area-v2-heading' not in html
    assert 'O que você quer fazer?' not in html
