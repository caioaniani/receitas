"""Interface v2 (redesenho promovido do preview em 18/08/2026).

Contrato da chave (app/ui_v2.py): env `UI_V2_ENABLED` liga o shell novo;
cookie `ui_classic` devolve UM usuário à interface anterior; `?v2=1`
força a tela nova numa request. A infra do ambiente de preview
(preview_copy/seed, PREVIEW_MODE, cópia de banco) NÃO existe aqui —
há teste travando isso.
"""


def _login(app, admin_user):
    client = app.test_client()
    client.post('/auth/login', data={
        'login': admin_user.login,
        'senha': '123',
    })
    return client


def test_ui_v2_usa_navegacao_reduzida(app, admin_user):
    app.config['UI_V2_ENABLED'] = True
    html = _login(app, admin_user).get('/area/producao').get_data(as_text=True)

    assert 'ui-v2-sidebar' in html
    assert 'Planejamento da indústria' in html
    assert 'ui-v2.css' in html
    assert 'Fichas Técnicas' not in html


def test_producao_mantem_shell_atual_com_flag_desligada(app, admin_user):
    app.config['UI_V2_ENABLED'] = False
    html = _login(app, admin_user).get('/area/producao').get_data(as_text=True)

    assert 'ui-v2-sidebar' not in html
    assert 'Fichas Técnicas' in html


def test_flag_desligada_e_o_default(app, admin_user):
    """Sem env UI_V2_ENABLED o sistema fica EXATAMENTE como era — a
    promoção do visual não muda nada até o dono ligar a chave."""
    assert app.config['UI_V2_ENABLED'] is False


def test_cookie_ui_classic_devolve_a_interface_anterior(app, admin_user):
    app.config['UI_V2_ENABLED'] = True
    client = _login(app, admin_user)
    client.set_cookie('ui_classic', '1')
    html = client.get('/').get_data(as_text=True)

    assert 'home-v2' not in html
    assert 'menu-grid' in html


def test_rotas_de_alternancia_setam_e_limpam_o_cookie(app, admin_user):
    app.config['UI_V2_ENABLED'] = True
    client = _login(app, admin_user)

    resp = client.get('/ui/classica')
    assert resp.status_code in (302, 303)
    cookies = resp.headers.getlist('Set-Cookie')
    assert any('ui_classic=1' in c for c in cookies)

    resp = client.get('/ui/nova')
    cookies = resp.headers.getlist('Set-Cookie')
    assert any('ui_classic=;' in c or 'ui_classic="";' in c
               for c in cookies)


def test_v2_na_query_forca_a_tela_nova_sem_flag(app, admin_user):
    """`?v2=1` permite validar a interface nova EM PRODUÇÃO antes de
    ligar a env pra equipe inteira."""
    app.config['UI_V2_ENABLED'] = False
    html = _login(app, admin_user).get('/?v2=1').get_data(as_text=True)

    assert 'home-v2' in html
    assert 'ui-v2.css' in html


def test_ui_v2_preserva_contrato_do_modo_embed(app, admin_user):
    app.config['UI_V2_ENABLED'] = True
    html = _login(app, admin_user).get(
        '/admin/loja-online/pedidos/inexistente?embed=1'
    ).get_data(as_text=True)

    assert '<body class="embed-mode">' in html
    assert '<body class="embed-mode ui-v2">' not in html


def test_home_v2_usa_nova_hierarquia_sem_alterar_dados(app, admin_user):
    app.config['UI_V2_ENABLED'] = True
    html = _login(app, admin_user).get('/').get_data(as_text=True)

    assert 'home-v2' in html
    assert 'Precisa de você hoje' in html
    assert 'Áreas de trabalho' in html
    assert 'Planejar produção' in html
    assert 'menu-grid' not in html
    assert 'css/ui-v2.css' in html


def test_home_legada_permanece_com_flag_desligada(app, admin_user):
    app.config['UI_V2_ENABLED'] = False
    html = _login(app, admin_user).get('/').get_data(as_text=True)

    assert 'home-v2' not in html
    assert 'menu-grid' in html
    assert 'Escolha uma área para continuar.' in html


def test_area_v2_usa_mesma_entrada_em_todas_as_equipes(app, admin_user):
    app.config['UI_V2_ENABLED'] = True
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


def test_area_legada_permanece_com_flag_desligada(app, admin_user):
    app.config['UI_V2_ENABLED'] = False
    html = _login(app, admin_user).get('/area/producao').get_data(as_text=True)

    assert 'area-wrap' in html
    assert 'area-v2-heading' not in html
    assert 'O que você quer fazer?' not in html


def test_v2_materias_primas_prioriza_leitura_e_pagina_resultados(
        app, admin_user):
    from app.extensions import db
    from app.models import MateriaPrima

    db.session.add_all([
        MateriaPrima(nome=f'Ingrediente {indice:02d}', unidade='kg',
                     custo_por_kg=indice + 1, fornecedor='Fornecedor teste')
        for indice in range(35)
    ])
    db.session.commit()
    app.config['UI_V2_ENABLED'] = True

    html = _login(app, admin_user).get('/materias-primas/').get_data(
        as_text=True)

    assert 'class="mp-v2"' in html
    assert 'Consulte primeiro' in html
    assert '30' in html and '35' in html
    assert 'id="mp-1-nome"' in html
    assert 'for="mp-1-nome"' in html
    assert 'id="mp-table"' not in html


def test_v2_materias_primas_busca_no_servidor(app, admin_user):
    from app.extensions import db
    from app.models import MateriaPrima

    db.session.add_all([
        MateriaPrima(nome='Farinha Orgânica', unidade='kg', custo_por_kg=12,
                     fornecedor='Moinho Azul'),
        MateriaPrima(nome='Chocolate', unidade='kg', custo_por_kg=40,
                     fornecedor='Cacau Sul'),
    ])
    db.session.commit()
    app.config['UI_V2_ENABLED'] = True

    html = _login(app, admin_user).get(
        '/materias-primas/?q=Moinho').get_data(as_text=True)

    assert '<strong>Farinha Orgânica</strong>' in html
    assert '<strong>Chocolate</strong>' not in html
    assert '<strong>1</strong> resultado' in html
    assert 'para “Moinho”' in html


def test_materias_primas_legada_permanece_com_flag_desligada(app, admin_user):
    app.config['UI_V2_ENABLED'] = False
    html = _login(app, admin_user).get('/materias-primas/').get_data(
        as_text=True)

    assert 'id="mp-table"' in html
    assert 'class="mp-v2"' not in html


def test_contas_vazia_explica_proximo_passo(app, admin_user):
    app.config['UI_V2_ENABLED'] = True
    html = _login(app, admin_user).get('/contas-pagar/').get_data(as_text=True)

    assert 'Nenhuma conta em aberto' in html
    assert 'Tudo conferido por aqui' in html
    assert 'aria-current="page"' in html


def test_infra_de_preview_nao_foi_promovida(app):
    """A promoção trouxe SÓ o visual: preview_copy/preview_seed,
    PREVIEW_MODE e o reset de senha do admin ficaram no branch de
    homologação — nada disso pode existir em produção."""
    import os

    import app as app_pkg
    base = os.path.dirname(app_pkg.__file__)
    assert not os.path.exists(os.path.join(base, 'preview_copy.py'))
    assert not os.path.exists(os.path.join(base, 'preview_seed.py'))
    assert 'PREVIEW_MODE' not in app.config
    assert 'PREVIEW_SOURCE_DATABASE_URL' not in app.config


# ---------------------------------------------------------------------------
# Papéis e dinheiro (lacunas apontadas pela revisão do porte)
# ---------------------------------------------------------------------------

def _login_como(app, login):
    client = app.test_client()
    client.post('/auth/login', data={'login': login, 'senha': '123'})
    return client


def test_home_v2_mostra_vendas_pro_dono(app, owner_user):
    """Metade 1 do contrato de DINHEIRO (funções separadas por usuário —
    armadilha do conftest: g._login_user vaza entre clients do MESMO
    teste). O dono vê a home v2 com o painel de vendas."""
    app.config['UI_V2_ENABLED'] = True
    html = _login_como(app, 'dono').get('/').get_data(as_text=True)
    assert 'home-v2' in html
    assert 'abrir-cd' in html            # drill-down de vendas do cockpit


def test_home_v2_nao_vaza_vendas_pra_admin_comum(app, admin_user):
    """Metade 2: admin comum NÃO vê dinheiro na home v2 (mesma regra da
    home clássica — vendas é cockpit pessoal do dono)."""
    app.config['UI_V2_ENABLED'] = True
    html = _login_como(app, 'admin').get('/').get_data(as_text=True)
    assert 'home-v2' in html
    assert 'abrir-cd' not in html
    assert 'home-v2.js' not in html


def test_shell_v2_nao_muda_permissao_de_rota(app):
    """?v2=1 troca TEMPLATE, nunca permissão: papel producao segue 403
    na /telaindustriateste e a sidebar v2 não mostra o atalho."""
    from app.extensions import db
    from app.models import Usuario
    u = Usuario(nome='prod teste', login='prod', papel='producao')
    u.set_senha('123')
    db.session.add(u)
    db.session.commit()
    app.config['UI_V2_ENABLED'] = True

    client = _login_como(app, 'prod')
    resp = client.get('/telaindustriateste/?v2=1')
    assert resp.status_code == 403


def test_sidebar_v2_esconde_atalho_da_industria_de_nao_admin(app):
    from app.extensions import db
    from app.models import Usuario
    u = Usuario(nome='prod2 teste', login='prod2', papel='producao')
    u.set_senha('123')
    db.session.add(u)
    db.session.commit()
    app.config['UI_V2_ENABLED'] = True

    html = _login_como(app, 'prod2').get('/').get_data(as_text=True)
    assert 'ui-v2-sidebar' in html
    assert 'Planejamento da indústria' not in html   # link daria 403


def test_v2_salvar_mp_nova_com_checkbox_de_pedido_loja(app, admin_user):
    """A tela v2 permite marcar 'disponível pro pedido das lojas' já na
    criação (value 'novo-<i>'); a clássica não emite o checkbox e a MP
    nova continua nascendo False."""
    from app.models import MateriaPrima
    app.config['UI_V2_ENABLED'] = True
    client = _login_como(app, 'admin')

    resp = client.post('/materias-primas/salvar', data={
        'q': 'far', 'page': '2',
        'mp_id[]': [''], 'nome[]': ['Farinha V2'], 'unidade[]': ['kg'],
        'custo_por_kg[]': ['10'], 'peso_unidade[]': [''], 'fornecedor[]': [''],
        'observacoes[]': [''], 'lote_pedido[]': [''], 'minimo_pedido[]': [''],
        'sugerir_loja_ids': ['novo-0'],
    })
    assert resp.status_code in (302, 303)
    assert 'q=far' in resp.location and 'page=2' in resp.location
    mp = MateriaPrima.query.filter_by(nome='Farinha V2').first()
    assert mp is not None and mp.sugerir_pedido_loja is True


def test_contas_classica_preserva_empty_state_antigo(app, admin_user):
    """Flag OFF = tela anterior: o empty-state clássico continua o texto
    antigo (o novo, com <h2>, só aparece na v2)."""
    app.config['UI_V2_ENABLED'] = False
    html = _login_como(app, 'admin').get('/contas-pagar/').get_data(
        as_text=True)
    assert 'Nenhuma conta nesta aba.' in html
    assert 'finance-v2-empty' not in html
