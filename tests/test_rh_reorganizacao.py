"""Nova entrada RH e revisão segura de acessos, sem integrações reais."""
import re
from html import unescape
from unittest.mock import patch

import pytest
from flask import g
from sqlalchemy import event

from app.extensions import db
from app.models import Cargo, Funcionario, Loja, RhReenvioAcessoExecucao, Usuario
from app.services import rh_acessos_lote, rh_painel


def cliente(app, usuario):
    g.pop('_login_user', None)
    client = app.test_client()
    with client.session_transaction() as sess:
        sess['_user_id'] = str(usuario.id)
        sess['_fresh'] = True
    return client


def equipe():
    nebraska = Loja(nome='Nebraska revisão', ativa=True)
    ribeiro = Loja(nome='Ribeiro revisão', ativa=True)
    cargo = Cargo(nome='Atendente', salario_base=9876.54)
    pessoas = []
    for n, loja in enumerate((nebraska, ribeiro), 1):
        usuario = Usuario(nome=f'Pessoa {n}', login=f'pessoa{n}@example.test',
                          email=f'pessoa{n}@example.test', senha_provisoria=True)
        usuario.set_senha('senha-fixture-inicial')
        pessoa = Funcionario(nome=f'Pessoa {n}', cpf=f'9550000000{n}',
                             email=usuario.email, usuario=usuario, cargo=cargo,
                             ativo=True, lojas=[loja], periodo='Manhã')
        db.session.add(pessoa)
        pessoas.append(pessoa)
    db.session.commit()
    return nebraska, ribeiro, pessoas


def revisao(client, modo='pendentes', **filtros):
    resposta = client.get('/rh/funcionarios/acessos/revisar',
                         query_string={'modo': modo, **filtros})
    assert resposta.status_code == 200
    html = resposta.get_data(as_text=True)
    return unescape(re.search(r'name="revisao" value="([^"]+)"', html)[1]), html


def enviar(client, token, modo='pendentes', **dados):
    return client.post('/rh/funcionarios/acessos/reenviar-' + modo,
                       data={'revisao': token, 'confirmacao': 'PENDENTES'
                             if modo == 'pendentes' else 'REENVIAR', **dados})


def test_hub_canonico_sem_financeiro_e_sem_escrita(app, owner_user):
    nebraska, ribeiro, pessoas = equipe()
    consultas = []
    def registrar(conn, cursor, statement, parameters, context, executemany):
        consultas.append(statement)
    event.listen(db.engine, 'before_cursor_execute', registrar)
    try:
        dados = rh_painel.carregar()
    finally:
        event.remove(db.engine, 'before_cursor_execute', registrar)
    assert dados['total'] == 2
    assert dados['pessoas_pendentes'] == 2
    assert sum(l['total'] for l in dados['estrutura']['lojas']) == 2
    assert all(sql.lstrip().upper().startswith('SELECT') for sql in consultas)
    client = cliente(app, owner_user)
    redir = client.get('/area/rh')
    assert redir.status_code == 302 and redir.location.endswith('/rh/')
    html = client.get('/rh/').get_data(as_text=True)
    for texto in ('Gestão de pessoas', nebraska.nome, ribeiro.nome,
                  'Precisa da sua atenção', 'Lojas e equipes', 'Administrativo'):
        assert texto in html
    assert '9.876,54' not in html
    assert 'Registrar Atestado' not in html
    assert 'Registrar Atestado' in client.get('/rh/administrativo').get_data(as_text=True)


@pytest.mark.parametrize('papel', ['admin', 'gerente', 'funcionario', 'rh', 'relatorio_loja'])
def test_hub_ficha_e_revisao_nao_ampliam_permissoes(app, papel):
    loja, _, pessoas = equipe()
    u = Usuario(nome='Visitante', login='visitante-rh', papel=papel, loja_id=loja.id)
    u.set_senha('fixture')
    db.session.add(u)
    db.session.commit()
    client = cliente(app, u)
    for path in ('/rh/', '/rh/administrativo', f'/rh/funcionarios/{pessoas[0].id}',
                 '/rh/funcionarios/acessos/revisar'):
        assert client.get(path).status_code == 403


def test_ficha_resumo_abas_identificador_atual_e_contexto(app, owner_user):
    _, _, pessoas = equipe()
    pessoa = pessoas[0]
    pessoa.usuario.email = 'corrigido@example.test'
    pessoa.email = 'corrigido@example.test'
    pessoa.usuario.senha_provisoria = False
    db.session.commit()
    client = cliente(app, owner_user)
    base = f'/rh/funcionarios/{pessoa.id}'
    resumo = client.get(base).get_data(as_text=True)
    assert 'Quem é e onde atua' in resumo
    assert pessoa.cpf not in resumo
    assert '9.876,54' not in resumo
    for aba in ('cadastro', 'treinamento', 'acesso'):
        assert client.get(base + '?aba=' + aba).status_code == 200
    acesso = client.get(base + '?aba=acesso').get_data(as_text=True)
    assert 'corrigido@example.test' in acesso
    assert 'pessoa1@example.test' not in acesso
    assert 'Sem troca pendente' in acesso
    assert 'não comprovam a entrega' in acesso
    assert client.get(base + '?aba=desconhecida').status_code == 400
    lista = client.get('/rh/funcionarios', query_string={
        'view': 'acessos', 'acesso': 'todos', 'pessoa': pessoa.id}).get_data(as_text=True)
    assert pessoas[1].nome not in lista
    assert f'name="pessoa" value="{pessoa.id}"' in lista
    assert f'pessoa={pessoa.id}' in lista
    assert 'O envio respeita a unidade e a pessoa selecionadas' in lista
    assert 'independentemente dos filtros' not in lista
    feedback = client.post(base + '/feedback', data={'texto': ''})
    assert 'aba=cadastro' in feedback.location and feedback.location.endswith('#feedback')


def test_classico_conserva_ficha_e_caminhos(app, owner_user):
    _, _, pessoas = equipe()
    app.config['UI_V2_ENABLED'] = False
    client = cliente(app, owner_user)
    assert 'Registrar Atestado' in client.get('/rh/').get_data(as_text=True)
    assert 'Por loja' in client.get('/rh/equipe').get_data(as_text=True)
    assert pessoas[0].cpf in client.get(f'/rh/funcionarios/{pessoas[0].id}').get_data(as_text=True)


def test_acesso_individual_preserva_pessoa_inativa_no_retorno(app, owner_user):
    _, _, pessoas = equipe()
    pessoa = pessoas[0]
    pessoa.ativo = False
    db.session.commit()
    client = cliente(app, owner_user)
    resposta = client.post(f'/rh/funcionarios/{pessoa.id}/acesso', data={
        'acao': 'salvar_email', 'email': pessoa.email,
        'pessoa': pessoa.id, 'apenas_ativos': '0', 'filtro_acesso': 'todos',
    })
    assert resposta.status_code == 302
    assert 'ativos=0' in resposta.location
    assert f'pessoa={pessoa.id}' in resposta.location
    assert pessoa.nome in client.get(resposta.location).get_data(as_text=True)


def test_revisao_apenas_le_e_envia_so_revisados_preservando_unidade(app, owner_user):
    loja, _, pessoas = equipe()
    ids = [p.usuario_id for p in pessoas]
    hashes = [p.usuario.senha_hash for p in pessoas]
    client = cliente(app, owner_user)
    with patch('app.services.email.enviar_boas_vindas', return_value={'ok': True}) as email:
        token, html = revisao(client, loja=loja.id)
        assert pessoas[0].email in html and pessoas[1].email not in html
        assert 'Nada foi enviado' in html
        assert [db.session.get(Usuario, uid).senha_hash for uid in ids] == hashes
        assert RhReenvioAcessoExecucao.query.count() == 0
        email.assert_not_called()
        resposta = enviar(client, token)
        assert f'loja={loja.id}' in resposta.location
        email.assert_called_once()
        assert email.call_args.args[0] == pessoas[0].email
        assert db.session.get(Usuario, ids[1]).senha_hash == hashes[1]
        enviar(client, token)
        email.assert_called_once()
        assert RhReenvioAcessoExecucao.query.count() == 1


@pytest.mark.parametrize('mudanca', ['email', 'senha', 'ativo', 'owner', 'loja', 'provisoria'])
def test_revisao_mudanca_exige_revisar_sem_enviar(app, owner_user, mudanca):
    loja, _, pessoas = equipe()
    p = pessoas[0]
    client = cliente(app, owner_user)
    token, _ = revisao(client, loja=loja.id)
    if mudanca == 'email':
        p.email = 'novo@example.test'
    elif mudanca == 'senha':
        p.usuario.set_senha('outra-fixture')
    elif mudanca == 'ativo':
        p.ativo = False
    elif mudanca == 'owner':
        p.usuario.is_owner = True
    elif mudanca == 'loja':
        p.lojas = []
    else:
        p.usuario.senha_provisoria = False
    db.session.commit()
    with patch('app.services.email.enviar_boas_vindas') as email:
        enviar(client, token)
        email.assert_not_called()
    assert RhReenvioAcessoExecucao.query.count() == 0


def test_duas_revisoes_confirmadas_antes_do_envio_nao_enviam_duas_senhas(app, owner_user):
    loja, _, _ = equipe()
    previa = rh_acessos_lote.prever('pendentes', loja_id=loja.id)
    token1 = rh_acessos_lote.assinar(previa, owner_user.id)
    token2 = rh_acessos_lote.assinar(previa, owner_user.id)
    lote1 = rh_acessos_lote.confirmar(token1, 'pendentes', owner_user.id)
    lote2 = rh_acessos_lote.confirmar(token2, 'pendentes', owner_user.id)
    with patch('app.services.email.enviar_boas_vindas', return_value={'ok': True}) as email:
        assert rh_acessos_lote.enviar_revisado(lote1['incluidos'][0])['ok']
        assert not rh_acessos_lote.enviar_revisado(lote2['incluidos'][0])['ok']
        email.assert_called_once()


def test_assinatura_autor_modo_expiracao_e_destinatario_novo(app, owner_user):
    loja, _, pessoas = equipe()
    previa = rh_acessos_lote.prever('pendentes', loja_id=loja.id)
    token = rh_acessos_lote.assinar(previa, owner_user.id)
    for t, modo, autor in ((token + 'alterado', 'pendentes', owner_user.id),
                          (token, 'todos', owner_user.id),
                          (token, 'pendentes', pessoas[0].usuario_id)):
        with pytest.raises(ValueError):
            rh_acessos_lote.confirmar(t, modo, autor)
    with patch('itsdangerous.timed.TimestampSigner.get_timestamp', return_value=1):
        expirado = rh_acessos_lote.assinar(previa, owner_user.id)
    with pytest.raises(ValueError):
        rh_acessos_lote.confirmar(expirado, 'pendentes', owner_user.id)
    pessoas[1].lojas.append(loja)
    db.session.commit()
    lote = rh_acessos_lote.confirmar(token, 'pendentes', owner_user.id)
    assert [i['pessoa'].id for i in lote['incluidos']] == [pessoas[0].id]


def test_revisao_exclui_email_invalido_owner_e_sem_conta(app, owner_user):
    _, _, pessoas = equipe()
    pessoas[0].email = pessoas[0].usuario.email = 'invalido'
    pessoas[1].usuario.is_owner = True
    db.session.add(Funcionario(nome='Sem conta', cpf='95500000009', ativo=True))
    db.session.commit()
    previa = rh_acessos_lote.prever('todos')
    assert not previa['incluidos']
    assert len(previa['excluidos']) == 2
    assert any('proprietário' in i['motivo'] for i in previa['excluidos'])


def test_erro_provedor_nao_troca_senha_e_nao_permite_replay(app, owner_user):
    loja, _, pessoas = equipe()
    senha = pessoas[0].usuario.senha_hash
    client = cliente(app, owner_user)
    token, _ = revisao(client, loja=loja.id)
    with patch('app.services.email.enviar_boas_vindas', return_value={'ok': False, 'erro': 'teste'}) as email:
        enviar(client, token)
        enviar(client, token)
        email.assert_called_once()
    assert pessoas[0].usuario.senha_hash == senha
