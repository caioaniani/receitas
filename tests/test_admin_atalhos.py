"""Os atalhos da home respeitam o perfil e levam a telas acessíveis."""

import re

import pytest


@pytest.mark.parametrize('perfil', ['admin_user', 'owner_user'])
def test_atalhos_apontam_para_telas_permitidas(app, request, perfil):
    usuario = request.getfixturevalue(perfil)
    client = app.test_client()
    with client.session_transaction() as sess:
        sess['_user_id'] = str(usuario.id)
        sess['_fresh'] = True

    resposta = client.get('/')
    assert resposta.status_code == 200
    html = resposta.get_data(as_text=True)
    bloco = re.search(r'<nav class="admin-daily".*?</nav>', html, re.S)
    assert bloco
    links = set(re.findall(r'href="(/[^\"]*)"', bloco.group()))
    assert links == {'/telaindustriateste/', '/pedidos/',
                     '/admin/loja-online/pedidos', '/checklist/conferencia'}
    assert 'admin-workspace' not in html
    sidebar = re.search(r'<nav[^>]+id="sidebar".*?</nav>', html, re.S).group()
    exclusivos = {'/rh/lideranca/preenchimento', '/rh/escala', '/rh/plano-carreira'}
    equipe = re.search(r'<details class="ui-v2-nav-fold">.*?</details>', sidebar, re.S).group()
    links_equipe = set(re.findall(r'href="(/[^\"]*)"', equipe))
    if perfil == 'owner_user':
        assert exclusivos <= links_equipe
    else:
        assert not exclusivos & links_equipe
    # O menu dá acesso às outras funções, mas começa recolhido na Home.
    assert '<details class="home-v2-areas home-v2-areas-fold" id="areas-de-trabalho">' in html
    assert '<details class="ui-v2-nav-fold" open' not in sidebar
    for caminho in links:
        assert client.get(caminho).status_code == 200, caminho
