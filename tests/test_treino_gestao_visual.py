"""Busca e navegação visual não ampliam o acesso a pessoas ou salários."""
import re

import pytest

from app.extensions import db
from app.models import Cargo, Funcionario, Loja, Usuario
from app.services.treino_painel import filtrar_equipe


def login(app, usuario):
    client = app.test_client()
    with client.session_transaction() as session:
        session['_user_id'] = str(usuario.id)
        session['_fresh'] = True
    return client


def pessoa(nome, cpf, **kwargs):
    funcionario = Funcionario(nome=nome, cpf=cpf, ativo=True, **kwargs)
    db.session.add(funcionario)
    db.session.flush()
    return funcionario


def test_filtro_ignora_acentos_e_mantem_escopo(app):
    cargo = Cargo(nome='Atendente')
    loja = Loja(nome='São João')
    dentro = pessoa('Júlia Exemplo', '100', cargo=cargo, lojas=[loja])
    pessoa('Júlia Fora', '101', cargo=cargo, lojas=[loja])
    linha = {'funcionario': dentro, 'status': 'nao_iniciou'}
    assert filtrar_equipe([linha], 'julia sao', 'atencao') == [linha]
    assert filtrar_equipe([linha], 'ATENDENTE') == [linha]
    assert filtrar_equipe([linha], 'inexistente') == []
    assert filtrar_equipe([linha], 'julia', 'concluido') == []


def test_busca_gestor_nao_revela_outra_equipe(app):
    user = Usuario(nome='Líder', login='lider-visual', papel='funcionario', somente_treino=True)
    user.set_senha('teste-visual-seguro')
    db.session.add(user)
    db.session.flush()
    lider = pessoa('Líder', '102', usuario_id=user.id)
    liderado = pessoa('Júlia da equipe', '103', lider_id=lider.id)
    fora = pessoa('Pessoa restrita', '104')
    db.session.commit()
    client = login(app, user)
    html = client.get('/treino/gestor/?v2=1&q=julia').get_data(as_text=True)
    assert f'data-funcionario-id="{liderado.id}"' in html
    assert 'Observar na prática' in html
    assert f'data-funcionario-id="{fora.id}"' not in html
    vazio = client.get('/treino/gestor/?v2=1&q=restrita').get_data(as_text=True)
    assert 'Nenhuma pessoa encontrada' in vazio
    assert 'Pessoa restrita' not in vazio
    assert client.get(f'/treino/gestor/progresso/{fora.id}').status_code == 403


@pytest.mark.parametrize('papel,owner,esperados', [
    ('funcionario', False, {'/treino/'}),
    ('gerente', False, {'/treino/', '/treino/gestor/'}),
    ('admin', False, {'/treino/', '/treino/gestor/', '/treino/admin/'}),
    ('admin', True, {'/treino/', '/treino/gestor/', '/treino/admin/', '/rh/equipe'}),
])
def test_navegacao_respeita_papel_e_salarios(app, papel, owner, esperados):
    user = Usuario(nome='Pessoa', login='nav-visual', papel=papel, is_owner=owner,
                   somente_treino=papel == 'funcionario')
    user.set_senha('teste-visual-seguro')
    db.session.add(user)
    db.session.commit()
    client = login(app, user)
    response = client.get('/treino/?v2=1')
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    nav = html.split('aria-label="Treinamento e pessoas">', 1)[1].split('</nav>', 1)[0]
    assert set(re.findall(r'href="([^"]+)"', nav)) == esperados
    if not owner:
        for caminho in ('/rh/equipe', '/rh/cargos', '/rh/plano-carreira'):
            blocked = client.get(caminho)
            if user.somente_treino:
                assert blocked.status_code == 302
                assert blocked.location.endswith('/treino/')
            else:
                assert blocked.status_code == 403


def test_filtro_invalido_nao_esconde_equipe(app, owner_user):
    f = pessoa('Pessoa visível', '105')
    db.session.commit()
    html = login(app, owner_user).get('/treino/gestor/?status=qualquer&q=visivel').get_data(as_text=True)
    assert f'data-funcionario-id="{f.id}"' in html
    assert 'aria-label="Abrir checklist de Pessoa visível"' in html


def test_progressao_nao_oferece_link_de_inativo_a_lider(app):
    user = Usuario(nome='Líder', login='lider-inativo-visual', papel='gerente')
    user.set_senha('teste-visual-seguro')
    db.session.add(user)
    db.session.flush()
    lider = pessoa('Líder', '106', usuario_id=user.id)
    inativo = pessoa('Inativo da equipe', '107', lider_id=lider.id)
    inativo.ativo = False
    db.session.commit()
    html = login(app, user).get('/treino/gestor/progressao?cadastro=inativos&v2=1').get_data(as_text=True)
    assert 'Inativo da equipe' in html
    assert f'/treino/gestor/progresso/{inativo.id}' not in html
