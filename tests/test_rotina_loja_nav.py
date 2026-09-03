"""Os atalhos da rotina levam a telas autorizadas para cada pessoa."""
import re

from app.extensions import db
from app.models import Usuario


def _client(app, usuario):
    client = app.test_client()
    with client.session_transaction() as sess:
        sess['_user_id'] = str(usuario.id)
        sess['_fresh'] = True
    return client


def _links_rotina(html):
    bloco = re.search(r'<nav[^>]+aria-label="Rotina da loja e equipe"[^>]*>(.*?)</nav>', html, re.S)
    assert bloco
    return set(re.findall(r'href="([^"]+)"', bloco.group(1)))


def test_owner_navega_entre_checklist_e_equipe(app, owner_user):
    client = _client(app, owner_user)
    caminhos = {'/checklist/', '/rh/lideranca/preenchimento', '/rh/escala',
                '/checklist/config', '/checklist/conferencia', '/checklist/responsaveis'}
    for caminho in caminhos:
        resposta = client.get(caminho)
        assert resposta.status_code == 200
        assert caminhos <= _links_rotina(resposta.get_data(as_text=True))


def test_admin_nao_recebe_atalhos_restritos_ao_owner(app, admin_user):
    client = _client(app, admin_user)
    links = _links_rotina(client.get('/checklist/').get_data(as_text=True))
    assert links == {'/checklist/', '/checklist/config', '/checklist/conferencia',
                     '/checklist/responsaveis'}
    assert client.get('/rh/escala').status_code == 403


def test_funcionario_tem_checklist_no_menu_sem_acesso_ao_rh(app):
    usuario = Usuario(nome='Atendente', login='atendente-nav', papel='funcionario')
    usuario.set_senha('senha-de-teste')
    db.session.add(usuario)
    db.session.commit()
    client = _client(app, usuario)
    html = client.get('/checklist/').get_data(as_text=True)
    sidebar = re.search(r'<nav[^>]+id="sidebar"[^>]*>(.*?)</nav>', html, re.S).group(1)
    assert 'href="/checklist/"' in sidebar
    assert 'href="/rh/escala"' not in sidebar
    assert _links_rotina(html) == {'/checklist/'}
    usuario.somente_treino = True
    db.session.commit()
    html = client.get('/treino/').get_data(as_text=True)
    sidebar = re.search(r'<nav[^>]+id="sidebar"[^>]*>(.*?)</nav>', html, re.S).group(1)
    assert 'href="/checklist/"' not in sidebar
