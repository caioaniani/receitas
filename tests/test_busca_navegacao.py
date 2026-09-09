"""A busca só entrega destinos acessíveis e mantém o contexto do cadastro."""
import json
from html.parser import HTMLParser
from urllib.parse import urlsplit

import pytest
from flask import render_template
from flask_login import login_user

from app.extensions import db
from app.models import Atribuicao, Funcionario, PermissaoPapel, Receita, Usuario
from app.services.busca_navegacao import itens_para_usuario


class _DadosBusca(HTMLParser):
    def __init__(self):
        super().__init__()
        self.lendo = False
        self.json = ''

    def handle_starttag(self, tag, attrs):
        self.lendo = tag == 'script' and dict(attrs).get('id') == 'busca-navegacao-dados'

    def handle_endtag(self, tag):
        if tag == 'script':
            self.lendo = False

    def handle_data(self, data):
        if self.lendo:
            self.json += data


def _usuario(papel='funcionario', **kwargs):
    usuario = Usuario(nome='Pessoa', login='pessoa', papel=papel, **kwargs)
    usuario.set_senha('123')
    db.session.add(usuario)
    db.session.commit()
    return usuario


def _busca_renderizada(app, usuario):
    with app.test_request_context('/'):
        login_user(usuario)
        parser = _DadosBusca()
        parser.feed(render_template('base.html'))
        return json.loads(parser.json)


def _paths(itens):
    return {urlsplit(item['url']).path for item in itens}


def test_owner_encontra_tarefas_com_destinos_existentes(app, owner_user):
    itens = _busca_renderizada(app, owner_user)
    por_termo = {
        termo: item['url'] for item in itens
        for termo in [item['titulo'].casefold(), *item['aliases']]
    }
    assert por_termo['estoque do site'] == '/admin/loja-online/plano-do-dia'
    assert por_termo['senha'] == '/rh/funcionarios?view=acessos&acesso=todos'
    assert por_termo['responsável'] == '/checklist/responsaveis'
    assert por_termo['desativar produto'] == '/admin/loja-online/catalogo?filtro=no-site'
    assert por_termo['minis'] == '/cardapio?tipo=atacado'
    assert por_termo['vender minis'] == '/b2b/vendas/nova'
    assert por_termo['cargos e salários'] == '/rh/plano-carreira'
    # Nenhuma entrada é um URL inventado ou aponta para ação apenas POST.
    adapter = app.url_map.bind('localhost')
    for item in itens:
        adapter.match(urlsplit(item['url']).path, method='GET')


@pytest.mark.parametrize('papel', ['admin', 'gerente', 'rh', 'producao', 'funcionario'])
def test_busca_nao_expoe_acessos_e_salarios_do_dono(app, papel):
    usuario = _usuario(papel)
    paths = _paths(_busca_renderizada(app, usuario))
    assert '/rh/plano-carreira' not in paths
    assert '/rh/funcionarios' not in paths
    assert '/rh/folha' not in paths
    assert '/rh/escala' not in paths
    assert '/admin/loja-online/plano-do-dia' not in paths
    assert ('/b2b/' in paths) == (papel == 'admin')
    assert ('/checklist/responsaveis' in paths) == (papel == 'admin')


def test_busca_respeita_override_de_capacidade_da_rota(app):
    usuario = _usuario('producao')
    db.session.add_all([
        PermissaoPapel(papel='producao', capacidade='web_estoque_loja', permitido=True),
        PermissaoPapel(papel='producao', capacidade='web_producao', permitido=False),
    ])
    db.session.commit()
    paths = _paths(_busca_renderizada(app, usuario))
    assert '/admin/loja-online/pedidos' in paths
    assert '/pedidos/estoque-loja' in paths
    assert '/pedidos/congelados' not in paths
    assert '/pedidos/separacao' not in paths


@pytest.mark.parametrize('somente_treino,senha_provisoria,papel', [
    (True, False, 'admin'), (False, True, 'admin'), (False, False, 'observador'),
])
def test_gates_da_conta_nao_expoem_tarefas_nem_receitas(
        app, somente_treino, senha_provisoria, papel):
    usuario = _usuario(papel, somente_treino=somente_treino,
                       senha_provisoria=senha_provisoria)
    receita = Receita(nome='Restrita', rendimento_qtd=1, rendimento_unidade='un',
                      peso_base=100)
    db.session.add(receita)
    db.session.commit()
    with app.test_request_context('/'):
        itens = itens_para_usuario(usuario, {'Pães': [receita]})
    paths = _paths(itens)
    assert not any(path.startswith(('/receitas/', '/b2b/', '/admin/', '/rh/'))
                   for path in paths)
    assert '/auth/minha-senha' in paths


@pytest.mark.parametrize('papel', ['admin', 'funcionario'])
def test_receitas_da_busca_usam_ids_e_atribuicoes(app, papel):
    usuario = _usuario(papel)
    nomes = ['Pão <img src=x onerror=alert(1)>', 'Outra ficha']
    receitas = [Receita(nome=nome, rendimento_qtd=1, rendimento_unidade='un',
                        peso_base=100)
                for nome in nomes]
    db.session.add_all(receitas)
    db.session.flush()
    db.session.add(Atribuicao(usuario_id=usuario.id, receita_id=receitas[0].id))
    db.session.commit()

    itens = [item for item in _busca_renderizada(app, usuario)
             if item['categoria'] == 'Receitas']
    esperadas = receitas if papel == 'admin' else receitas[:1]
    assert {(item['titulo'], item['url']) for item in itens} == {
        (receita.nome, f'/receitas/{receita.id}') for receita in esperadas}


@pytest.mark.parametrize('ativo', [True, False])
def test_ver_acesso_da_ficha_encontra_a_mesma_pessoa(app, owner_user, ativo):
    funcionario = Funcionario(nome='Pessoa da ficha', cpf='11111111111', ativo=ativo)
    db.session.add(funcionario)
    db.session.commit()
    cliente = app.test_client()
    with cliente.session_transaction() as sessao:
        sessao['_user_id'] = str(owner_user.id)
        sessao['_fresh'] = True

    class _LinkAcesso(HTMLParser):
        destino = None

        def handle_starttag(self, tag, attrs):
            if tag == 'a':
                href = dict(attrs).get('href', '')
                if href.endswith(f'#acesso-{funcionario.id}'):
                    self.destino = href

    ficha = cliente.get(f'/rh/funcionarios/{funcionario.id}')
    assert ficha.status_code == 200
    parser = _LinkAcesso()
    parser.feed(ficha.get_data(as_text=True))
    assert parser.destino is not None
    destino = cliente.get(parser.destino)
    assert destino.status_code == 200
    assert f'id="acesso-{funcionario.id}"' in destino.get_data(as_text=True)
