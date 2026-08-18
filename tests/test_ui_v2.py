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


def test_preview_materias_primas_prioriza_leitura_e_pagina_resultados(
        app, admin_user):
    from app.extensions import db
    from app.models import MateriaPrima

    db.session.add_all([
        MateriaPrima(nome=f'Ingrediente {indice:02d}', unidade='kg',
                     custo_por_kg=indice + 1, fornecedor='Fornecedor teste')
        for indice in range(35)
    ])
    db.session.commit()
    app.config['PREVIEW_MODE'] = True

    html = _login(app, admin_user).get('/materias-primas/').get_data(
        as_text=True)

    assert 'class="mp-v2"' in html
    assert 'Consulte primeiro' in html
    assert '30' in html and '35' in html
    assert 'id="mp-1-nome"' in html
    assert 'for="mp-1-nome"' in html
    assert 'id="mp-table"' not in html


def test_preview_materias_primas_busca_no_servidor(app, admin_user):
    from app.extensions import db
    from app.models import MateriaPrima

    db.session.add_all([
        MateriaPrima(nome='Farinha Orgânica', unidade='kg', custo_por_kg=12,
                     fornecedor='Moinho Azul'),
        MateriaPrima(nome='Chocolate', unidade='kg', custo_por_kg=40,
                     fornecedor='Cacau Sul'),
    ])
    db.session.commit()
    app.config['PREVIEW_MODE'] = True

    html = _login(app, admin_user).get(
        '/materias-primas/?q=Moinho').get_data(as_text=True)

    assert '<strong>Farinha Orgânica</strong>' in html
    assert '<strong>Chocolate</strong>' not in html
    assert '<strong>1</strong> resultado' in html
    assert 'para “Moinho”' in html


def test_materias_primas_legada_permanece_fora_do_preview(app, admin_user):
    app.config['PREVIEW_MODE'] = False
    html = _login(app, admin_user).get('/materias-primas/').get_data(
        as_text=True)

    assert 'id="mp-table"' in html
    assert 'class="mp-v2"' not in html


def test_preview_contas_vazia_explica_proximo_passo(app, admin_user):
    app.config['PREVIEW_MODE'] = True
    html = _login(app, admin_user).get('/contas-pagar/').get_data(as_text=True)

    assert 'Nenhuma conta em aberto' in html
    assert 'Tudo conferido por aqui' in html
    assert 'aria-current="page"' in html
