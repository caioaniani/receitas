"""Chefia de RH: remuneração e treinamento sem administração geral do ERP."""
from datetime import date
from html.parser import HTMLParser
from urllib.parse import urlsplit

import pytest
from flask import g

from app.extensions import db
from app.models import (
    Cargo,
    FolhaPagamento,
    Funcionario,
    MateriaPrima,
    PermissaoPapel,
    Receita,
    RhMovimentacao,
    TreinoTrilha,
    TreinoVideo,
    Usuario,
)
from app.services.acesso_gestao_rh import ENDPOINTS_RH, endpoint_permitido
from app.utils import hoje


def _client(app, user):
    g.pop('_login_user', None)
    client = app.test_client()
    with client.session_transaction() as sessao:
        sessao['_user_id'] = str(user.id)
        sessao['_fresh'] = True
    return client


class _HTML(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.links, self.inputs = [], {}
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'a' and attrs.get('href', '').startswith('/'):
            self.links.append(attrs['href'])
        if tag == 'input' and attrs.get('name'):
            self.inputs[attrs['name']] = attrs.get('value', '')


@pytest.fixture
def chefe(app):
    user = Usuario(nome='Chefia RH teste', login='chefia-rh', papel='gestao_rh',
                   email='chefia@example.test')
    user.set_senha('Senha-teste-123')
    cargo = Cargo(nome='Atendente 1', salario_base=2000, ativo=True)
    destino = Cargo(nome='Atendente 2', salario_base=2500, ativo=True)
    pessoa = Funcionario(nome='Pessoa teste RH', cpf='79100000001', ativo=True,
                         cargo=cargo, salario_base=2000, data_admissao=date(2020, 1, 1))
    db.session.add_all([user, cargo, destino, pessoa])
    db.session.commit()
    return _client(app, user), user, pessoa, destino


@pytest.mark.parametrize('classico', [False, True])
@pytest.mark.parametrize('url', [
    '/rh/', '/rh/administrativo', '/rh/funcionarios', '/rh/funcionarios/novo',
    '/rh/equipe', '/rh/equipe/lojas', '/rh/cargos', '/rh/plano-carreira',
    '/rh/lideranca', '/rh/lideranca/preenchimento', '/rh/lideranca/organograma',
    '/rh/folha', '/rh/folha/importar', '/rh/ferias', '/rh/ponto',
    '/rh/escala', '/rh/pre-cadastros', '/treino/admin/', '/treino/gestor/',
    '/treino/gestor/progressao', '/auth/minha-senha',
])
def test_leituras_navegacao_e_isolamento(app, chefe, url, classico):
    client, user, _, _ = chefe
    if classico:
        client.set_cookie('ui_classic', '1')
    db.session.add_all([
        MateriaPrima(nome='MP_SECRETA_FORA_RH', custo_por_kg=98765.43),
        Receita(nome='RECEITA_SECRETA_FORA_RH', categoria='Pães',
                rendimento_qtd=1, rendimento_unidade='un', peso_base=100),
    ])
    db.session.commit()
    response = client.get(url)
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert 'Gestão de RH' in html
    assert 'MP_SECRETA_FORA_RH' not in html
    assert 'RECEITA_SECRETA_FORA_RH' not in html
    assert '98765.43' not in html
    adapter = app.url_map.bind('localhost')
    for link in _HTML(html).links:
        path = urlsplit(link).path
        endpoint, args = adapter.match(path)
        assert (endpoint_permitido(endpoint)
                or endpoint in {'static', 'auth.logout', 'auth.minha_senha',
                                'main.index', 'main.ui_classica', 'main.ui_nova'}
                or (endpoint == 'main.area' and args['slug'] == 'rh')), link
        assert 'view=acessos' not in link, link
    assert not user.is_admin()
    assert not user.is_dono()


@pytest.mark.parametrize('url', [
    '/pedidos/', '/pedidos/relatorio', '/pedidos/estoque-loja', '/padeiro/',
    '/cobrancas/', '/b2b/', '/auth/usuarios', '/auth/painel', '/admin/permissoes',
    '/admin/loja-online', '/area/financeiro', '/area/fichas', '/area/vendas',
    '/rh/lojas', '/rh/contatos/importar', '/rh/funcionarios/acessos/revisar',
    '/rh/funcionarios?view=acessos',
])
def test_areas_fora_do_escopo_bloqueadas(chefe, url):
    client, _, _, _ = chefe
    assert client.get(url).status_code == 403


@pytest.mark.parametrize('url', [
    '/rh/lojas/salvar', '/rh/lojas/1/fiscal', '/rh/lojas/excluir/1',
    '/rh/contatos/importar/aplicar', '/rh/pre-cadastros/1/vincular',
    '/rh/funcionarios/1/acesso', '/rh/funcionarios/acessos/reenviar-todos',
    '/rh/funcionarios/acessos/reenviar-pendentes',
    '/treino/admin/acessos/gerar-todos', '/auth/usuarios/novo',
])
def test_post_fora_escopo_nao_executa(chefe, url, monkeypatch):
    from app.services import email
    monkeypatch.setattr(email, 'enviar_boas_vindas', lambda *a, **kw: pytest.fail('Não enviar'))
    client, _, _, _ = chefe
    assert client.post(url, data={'acao': 'reenviar', 'usuario_id': '1',
                                  'gerar_acesso': '1'}).status_code == 403


def test_allowlist_nao_tem_endpoints_inexistentes(app):
    assert ENDPOINTS_RH <= set(app.view_functions)


def test_urls_de_credenciais_treino_bloqueadas(app, chefe):
    from app.services.acesso_gestao_rh import ACESSOS_TREINO
    client, _, pessoa, _ = chefe
    for endpoint in ACESSOS_TREINO:
        url = app.url_map.bind('localhost').build(endpoint, {'func_id': pessoa.id})
        assert client.post(url).status_code == 403


def test_matriz_nao_amplia_perfil_fixo(chefe):
    from app.services import permissoes
    client, _, _, _ = chefe
    for cap in permissoes.CAP_DEFAULT:
        db.session.add(PermissaoPapel(papel='gestao_rh', capacidade=cap, permitido=True))
    db.session.commit()
    for path in ('/pedidos/', '/pedidos/relatorio', '/producao/', '/auth/usuarios'):
        assert client.get(path).status_code == 403


def test_salario_folha_e_promocao_com_autoria(chefe):
    client, user, pessoa, destino = chefe
    assert client.post('/rh/cargos/salvar', data={
        'cargo_id[]': [str(destino.id)], 'cargo_nome[]': [destino.nome],
        'cargo_salario[]': ['2750,00'], 'cargo_ativo[]': [str(destino.id)],
    }).status_code == 302
    assert db.session.get(Cargo, destino.id).salario_base == 2750
    assert client.post('/rh/folha/gerar', data={'mes': 9, 'ano': 2026}).status_code == 302
    folha = FolhaPagamento.query.filter_by(funcionario_id=pessoa.id).one()
    assert client.post(f'/rh/folha/{folha.id}/salvar', data={
        'dias_trabalhados': '25', 'premiacao': '300',
    }).status_code == 302
    url = f'/rh/funcionarios/{pessoa.id}/promover'
    revisao = client.post(url, data={
        'etapa': 'revisar', 'cargo_id': str(destino.id),
        'data_efetiva': hoje().isoformat(), 'observacao': 'Promoção confirmada pelo RH',
    })
    assert revisao.status_code == 200
    token = _HTML(revisao.get_data(as_text=True)).inputs['confirmacao']
    assert client.post(url, data={'etapa': 'confirmar', 'confirmacao': token}).status_code == 302
    assert pessoa.cargo_id == destino.id and pessoa.salario_base == 2750
    evento = RhMovimentacao.query.filter_by(funcionario_id=pessoa.id, tipo='promocao').one()
    assert evento.registrado_por_id == user.id
    assert folha.salario_base == 2000  # promoção não reabre folha existente
    assert client.post(url, data={'etapa': 'confirmar', 'confirmacao': token}).status_code == 400
    assert RhMovimentacao.query.filter_by(funcionario_id=pessoa.id, tipo='promocao').count() == 1


def test_edicao_ficha_nao_altera_email_de_conta_vinculada(chefe):
    client, _, pessoa, _ = chefe
    usuario = Usuario(nome='Conta protegida', login='admin-protegido', papel='admin',
                      email='original@example.test', senha_hash='hash-original')
    pessoa.usuario = usuario
    pessoa.email = usuario.email
    db.session.add(usuario)
    db.session.commit()
    resp = client.post(f'/rh/funcionarios/{pessoa.id}/salvar', data={
        'nome': 'Nome adulterado', 'email': 'outro@example.test', 'premiacao': '999',
    })
    assert resp.status_code == 403
    db.session.refresh(pessoa)
    assert pessoa.nome == 'Pessoa teste RH'
    assert pessoa.email == usuario.email == 'original@example.test'
    assert usuario.senha_hash == 'hash-original'
    html = client.get(f'/rh/funcionarios/{pessoa.id}?aba=cadastro').get_data(as_text=True)
    assert 'readonly aria-describedby="email-conta-protegido"' in html
    assert client.post(f'/rh/funcionarios/{pessoa.id}/salvar', data={
        'nome': pessoa.nome, 'cpf': pessoa.cpf, 'cargo_id': pessoa.cargo_id,
        'email': pessoa.email, 'ativo': '1', 'premiacao': '125',
    }).status_code == 302
    assert pessoa.premiacao == 125


def test_criar_e_editar_aulas_sem_criar_credenciais(chefe):
    client, _, _, _ = chefe
    assert client.post('/treino/admin/trilha', data={'nome': 'Treinamento teste RH'}).status_code == 302
    trilha = TreinoTrilha.query.filter_by(nome='Treinamento teste RH').one()
    assert client.post(f'/treino/admin/trilha/{trilha.id}/video', data={
        'titulo': 'Aula teste RH',
    }).status_code == 302
    video = TreinoVideo.query.filter_by(trilha_id=trilha.id).one()
    assert client.post(f'/treino/admin/video/{video.id}/titulo', data={
        'titulo': 'Aula editada pelo RH',
    }).status_code == 302
    assert video.titulo == 'Aula editada pelo RH'


@pytest.mark.parametrize('papel', ['admin', 'rh', 'gerente', 'funcionario'])
def test_perfis_anteriores_nao_ganham_salarios(app, papel):
    user = Usuario(nome='Outro perfil', login='outro-perfil', papel=papel, senha_hash='fixture')
    db.session.add(user)
    db.session.commit()
    client = _client(app, user)
    for url in ('/rh/', '/rh/cargos', '/rh/folha', '/rh/equipe'):
        assert client.get(url).status_code == 403
    assert client.post('/rh/cargos/salvar').status_code == 403


def test_admin_nao_concede_ou_assume_gestao_rh(app, chefe, admin_user, monkeypatch):
    from app.services import email
    monkeypatch.setattr(email, 'enviar_boas_vindas', lambda *a, **kw: pytest.fail('Não enviar'))
    _, user, _, _ = chefe
    client = _client(app, admin_user)
    assert client.post('/auth/usuarios/novo', data={
        'nome': 'Novo RH', 'login': 'novo-rh', 'papel': 'gestao_rh',
        'email': 'novo@example.test',
    }).status_code == 403
    for acao in ('papel', 'reset-senha', 'excluir', 'somente-treino'):
        assert client.post(f'/auth/usuarios/{user.id}/{acao}', data={'papel': 'funcionario'}).status_code == 403
    assert client.post(f'/auth/usuarios/{admin_user.id}/papel', data={'papel': 'gestao_rh'}).status_code == 403
    assert user.papel == 'gestao_rh'
    assert not Usuario.query.filter_by(login='novo-rh').first()


def test_owner_cria_perfil_sem_convite_chatwoot(app, owner_user, monkeypatch):
    from app.services import email
    enviados = []
    monkeypatch.setattr(email, 'enviar_boas_vindas', lambda *a, **kw: enviados.append((a, kw)) or {'ok': True})
    client = _client(app, owner_user)
    assert client.post('/auth/usuarios/novo', data={
        'nome': 'Novo RH teste', 'login': 'novo-rh', 'papel': 'gestao_rh',
        'email': 'novo@example.test',
    }).status_code == 302
    user = Usuario.query.filter_by(login='novo-rh').one()
    assert user.senha_provisoria and not user.is_admin() and not user.is_owner
    assert len(enviados) == 1 and enviados[0][1]['com_chatwoot'] is False


def test_entrada_troca_senha_e_restricao_treino(app, chefe, monkeypatch):
    client, user, _, _ = chefe
    assert client.get('/').location.endswith('/rh/')
    user.senha_provisoria = True
    db.session.commit()
    assert client.get('/rh/').location.endswith('/auth/minha-senha')
    resposta = client.post('/auth/minha-senha', data={
        'senha_atual': 'Senha-teste-123', 'nova_senha': 'Outra-senha-456',
        'confirma_senha': 'Outra-senha-456',
    })
    assert resposta.location.endswith('/rh/')
    assert not user.senha_provisoria
    g.pop('_login_user', None)
    anon = app.test_client()
    resposta = anon.post('/auth/login?next=/auth/usuarios', data={
        'login': user.login, 'senha': 'Outra-senha-456',
    })
    assert resposta.location.endswith('/rh/')
    user.somente_treino = True
    db.session.commit()
    assert client.get('/rh/').location.endswith('/treino/')
    assert client.get('/treino/admin/').status_code == 403
    from app.services.treino_ledger import papel_treino
    monkeypatch.setattr(Usuario, 'lidera_equipe', lambda self: True)
    assert papel_treino(user) == 'FUNCIONARIO'
    assert client.get('/treino/gestor/').status_code == 403
    html = client.get('/treino/').get_data(as_text=True)
    assert '/treino/gestor/' not in _HTML(html).links


def test_copilot_nao_executa_ferramentas_operacionais(chefe, monkeypatch):
    from app.services import copilot
    _, user, _, _ = chefe
    assert copilot.papel_efetivo(user) == 'gestao_rh'
    assert copilot.tools_permitidas(user) == []
    monkeypatch.setitem(copilot._READ_HANDLERS, 'consultar_pedido', lambda *a: pytest.fail('Não ler pedidos'))
    monkeypatch.setitem(copilot._EXEC_HANDLERS, 'criar_pedido', lambda *a: pytest.fail('Não criar pedido'))
    assert 'erro' in copilot._executar_read('consultar_pedido', {}, user)
    assert copilot.executar('criar_pedido', {}, user)['ok'] is False
