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
    bloco = re.search(r'<section class="admin-workspace".*?</section>', html, re.S)
    assert bloco
    links = set(re.findall(r'href="(/[^\"]*)"', bloco.group()))
    assert {'/checklist/', '/checklist/conferencia', '/checklist/responsaveis',
            '/checklist/config', '/auth/usuarios', '/contas-pagar/',
            '/cobrancas/painel', '/treino/gestor/'} <= links
    exclusivos = {'/rh/lideranca/preenchimento', '/rh/escala', '/admin/permissoes'}
    if perfil == 'owner_user':
        assert exclusivos <= links
    else:
        assert not exclusivos & links
    for caminho in links:
        assert client.get(caminho).status_code == 200, caminho
