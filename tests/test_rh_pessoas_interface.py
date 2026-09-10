"""Contratos de navegação e permissões nas listas de pessoas."""
from html.parser import HTMLParser

from flask import g
from werkzeug.datastructures import MultiDict

from app.extensions import db
from app.models import Funcionario, Usuario


class _FormularioEquipe(HTMLParser):
    """Lê os campos reais que o navegador enviaria no filtro de equipe."""

    def __init__(self, html):
        super().__init__()
        self.dentro = False
        self.campos = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'form':
            self.dentro = attrs.get('aria-label') == 'Selecionar equipe'
        elif self.dentro and tag == 'input' and attrs.get('name'):
            self.campos.append(attrs)

    def handle_endtag(self, tag):
        if tag == 'form':
            self.dentro = False

    def dados(self, apenas_ativos):
        return MultiDict(
            (campo['name'], campo.get('value', ''))
            for campo in self.campos
            if campo.get('type') != 'checkbox' or apenas_ativos
        )


def _cliente(app, usuario):
    g.pop('_login_user', None)
    cliente = app.test_client()
    with cliente.session_transaction() as sessao:
        sessao['_user_id'] = str(usuario.id)
        sessao['_fresh'] = True
    return cliente


def test_desmarcar_apenas_ativos_envia_zero_e_inclui_inativos(app, owner_user):
    db.session.add_all([
        Funcionario(nome='Pessoa ativa de teste', cpf='89100000001', ativo=True),
        Funcionario(nome='Pessoa inativa de teste', cpf='89100000002', ativo=False),
    ])
    db.session.commit()
    cliente = _cliente(app, owner_user)
    html = cliente.get('/rh/funcionarios').get_data(as_text=True)
    formulario = _FormularioEquipe(html)

    ativos = cliente.get('/rh/funcionarios', query_string=formulario.dados(True))
    todos = cliente.get('/rh/funcionarios', query_string=formulario.dados(False))

    assert 'Pessoa ativa de teste' in ativos.get_data(as_text=True)
    assert 'Pessoa inativa de teste' not in ativos.get_data(as_text=True)
    assert 'Pessoa ativa de teste' in todos.get_data(as_text=True)
    assert 'Pessoa inativa de teste' in todos.get_data(as_text=True)


def test_busca_local_nao_expoe_salario_ou_atalhos_restritos_ao_dakson(app):
    dakson = Usuario(nome='Dakson de teste', login='dakson', papel='admin',
                     somente_treino=True)
    dakson.set_senha('somente-fixture')
    colega = Funcionario(nome='Pessoa da equipe', cpf='89100000003',
                         ativo=True, salario_base=9876.54)
    db.session.add_all([dakson, colega])
    db.session.flush()
    db.session.add(Funcionario(nome='Dakson de teste', cpf='89100000005',
                               ativo=True, usuario_id=dakson.id))
    db.session.commit()

    cliente = _cliente(app, dakson)
    resposta = cliente.get('/rh/funcionarios')
    html = resposta.get_data(as_text=True)

    assert resposta.status_code == 200
    assert 'Pessoa da equipe' in html
    assert 'Buscar nesta lista' in html
    assert '9.876,54' not in html and '9876.54' not in html
    assert 'data-label="Salário"' not in html
    assert f'href="/rh/funcionarios/{colega.id}"' not in html
    assert 'href="/rh/plano-carreira"' not in html
    assert 'href="/rh/funcionarios?view=acessos"' not in html
    assert cliente.get('/rh/funcionarios?view=acessos').status_code == 403


def test_busca_local_trata_nome_como_dado_e_owner_mantem_ficha(app, owner_user):
    nome = 'Pessoa " data-injetado="sim <script>invadir()</script>'
    colega = Funcionario(nome=nome, cpf='89100000004', ativo=True,
                         salario_base=4321.98)
    db.session.add(colega)
    db.session.commit()

    html = _cliente(app, owner_user).get('/rh/funcionarios').get_data(as_text=True)

    assert '4.321,98' in html
    assert f'href="/rh/funcionarios/{colega.id}"' in html
    assert '<script>invadir()</script>' not in html
    assert '" data-injetado="sim' not in html
    assert 'data-rh-person="Pessoa &#34; data-injetado=&#34;sim' in html
