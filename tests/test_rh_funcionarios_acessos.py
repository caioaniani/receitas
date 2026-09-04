"""Acesso ao treinamento gerenciado na própria lista de funcionários do RH."""
from unittest.mock import patch

from app.extensions import db
from app.models import (
    Cargo,
    Funcionario,
    TreinoTrilha,
    TreinoTrilhaCargo,
    Usuario,
)


def _owner(app, owner_user):
    cliente = app.test_client()
    with cliente.session_transaction() as sessao:
        sessao['_user_id'] = str(owner_user.id)
        sessao['_fresh'] = True
    return cliente


def _usuario(nome, login, *, email=None, papel='funcionario',
             senha_provisoria=False):
    usuario = Usuario(nome=nome, login=login, email=email, papel=papel,
                      senha_provisoria=senha_provisoria)
    usuario.set_senha('senha-antiga')
    db.session.add(usuario)
    db.session.commit()
    return usuario


def _funcionario(nome, cpf, *, email=None, cargo=None, usuario=None):
    funcionario = Funcionario(
        nome=nome, cpf=cpf, email=email, ativo=True,
        cargo_id=cargo.id if cargo else None,
        usuario_id=usuario.id if usuario else None,
    )
    db.session.add(funcionario)
    db.session.commit()
    return funcionario


def test_tela_separa_conta_existente_criacao_e_modulos(app, owner_user):
    with app.app_context():
        cargo = Cargo(nome='Atendimento', salario_base=0)
        trilha = TreinoTrilha(nome='Boas-vindas', ativa=True, ordem=1)
        db.session.add_all([cargo, trilha])
        db.session.commit()
        db.session.add(TreinoTrilhaCargo(
            cargo_id=cargo.id, trilha_id=trilha.id))
        db.session.commit()

        conta = _usuario('Ana Souza', 'ana.antiga',
                         email='ana@opao.online')
        _funcionario('Ana Souza', '81000000001',
                     email='ana@opao.online', cargo=cargo)
        vinculada = _usuario('Bia Lima', 'bia.login',
                             email='bia@opao.online')
        _funcionario('Bia Lima', '81000000002',
                     email='bia@opao.online', cargo=cargo,
                     usuario=vinculada)
        _funcionario('Caio Melo', '81000000003',
                     email='caio@opao.online', cargo=cargo)
        _funcionario('Dani Reis', '81000000004', cargo=cargo)
        conta_login = conta.login

    html = _owner(app, owner_user).get(
        '/rh/funcionarios?view=acessos&acesso=todos').get_data(as_text=True)

    assert 'Possível conta existente' in html
    assert 'Sugestão por mesmo e-mail' in html
    assert conta_login in html
    assert 'Pronto para criar' in html
    assert 'Falta e-mail' in html
    assert 'Acesso liberado' in html
    assert 'Boas-vindas' in html
    assert 'Criar e enviar senha' in html
    assert 'Vincular conta' in html


def test_vincular_conta_preserva_login_e_senha(app, owner_user):
    with app.app_context():
        usuario = _usuario('Marina Silva', 'marina.sistema')
        funcionario = _funcionario('Marina Silva', '81000000005')
        uid, fid = usuario.id, funcionario.id
        hash_antes = usuario.senha_hash
        total_antes = Usuario.query.count()

    with patch('app.services.email.enviar_boas_vindas') as enviar:
        resposta = _owner(app, owner_user).post(
            f'/rh/funcionarios/{fid}/acesso',
            data={'acao': 'vincular', 'usuario_id': uid,
                  'email': 'marina@opao.online',
                  'filtro_acesso': 'pendentes', 'apenas_ativos': '1'},
            follow_redirects=True)

    assert resposta.status_code == 200
    assert 'login e a senha continuam os mesmos' in resposta.get_data(
        as_text=True)
    enviar.assert_not_called()
    with app.app_context():
        funcionario = db.session.get(Funcionario, fid)
        usuario = db.session.get(Usuario, uid)
        assert funcionario.usuario_id == uid
        assert funcionario.email == 'marina@opao.online'
        assert usuario.email == 'marina@opao.online'
        assert usuario.login == 'marina.sistema'
        assert usuario.senha_hash == hash_antes
        assert Usuario.query.count() == total_antes


def test_criar_acesso_envia_senha_e_limita_ao_treino(app, owner_user):
    with app.app_context():
        funcionario = _funcionario('Nova Pessoa', '81000000006')
        fid = funcionario.id

    with patch('app.services.email.enviar_boas_vindas',
               return_value={'ok': True}) as enviar:
        resposta = _owner(app, owner_user).post(
            f'/rh/funcionarios/{fid}/acesso',
            data={'acao': 'gerar', 'usuario_id': '',
                  'email': 'nova@opao.online', 'somente_treino': '1',
                  'filtro_acesso': 'pendentes', 'apenas_ativos': '1'},
            follow_redirects=True)

    assert resposta.status_code == 200
    assert 'senha provisória foram enviados' in resposta.get_data(as_text=True)
    enviar.assert_called_once()
    with app.app_context():
        funcionario = db.session.get(Funcionario, fid)
        usuario = funcionario.usuario
        assert funcionario.email == 'nova@opao.online'
        assert usuario.login == 'nova@opao.online'
        assert usuario.email == 'nova@opao.online'
        assert usuario.senha_provisoria is True
        assert usuario.somente_treino is True


def test_reenviar_individual_so_troca_senha_quando_email_aceito(
        app, owner_user):
    with app.app_context():
        usuario = _usuario('Bia Lima', 'bia.login', email='bia@opao.online')
        funcionario = _funcionario('Bia Lima', '81000000061',
                                   email='bia@opao.online', usuario=usuario)
        fid, uid = funcionario.id, usuario.id
        hash_antes = usuario.senha_hash

    with patch('app.services.email.enviar_boas_vindas',
               return_value={'ok': True, 'id': 'msg-1'}) as enviar:
        resposta = _owner(app, owner_user).post(
            f'/rh/funcionarios/{fid}/acesso',
            data={'acao': 'reenviar', 'email': 'bia@opao.online',
                  'filtro_acesso': 'vinculados', 'apenas_ativos': '1'},
            follow_redirects=True)

    assert 'senha anterior deixou de funcionar' in resposta.get_data(
        as_text=True)
    enviar.assert_called_once()
    assert enviar.call_args.args[:3] == (
        'bia@opao.online', 'Bia Lima', 'bia.login')
    assert enviar.call_args.kwargs['com_chatwoot'] is False
    with app.app_context():
        usuario = db.session.get(Usuario, uid)
        assert usuario.senha_hash != hash_antes
        assert usuario.senha_provisoria is True


def test_reenviar_individual_falha_email_preserva_senha(app, owner_user):
    with app.app_context():
        usuario = _usuario('Cris Luz', 'cris.login', email='cris@opao.online')
        funcionario = _funcionario('Cris Luz', '81000000062',
                                   email='cris@opao.online', usuario=usuario)
        fid, uid = funcionario.id, usuario.id
        hash_antes = usuario.senha_hash

    with patch('app.services.email.enviar_boas_vindas',
               return_value={'ok': False, 'erro': 'recusado'}):
        resposta = _owner(app, owner_user).post(
            f'/rh/funcionarios/{fid}/acesso',
            data={'acao': 'reenviar', 'email': 'cris@opao.online',
                  'filtro_acesso': 'vinculados', 'apenas_ativos': '1'},
            follow_redirects=True)

    assert 'Não alterei a senha' in resposta.get_data(as_text=True)
    with app.app_context():
        usuario = db.session.get(Usuario, uid)
        assert usuario.senha_hash == hash_antes
        assert usuario.senha_provisoria is False


def test_tela_mostra_quantidade_de_primeiros_acessos_pendentes(
        app, owner_user):
    with app.app_context():
        pendente = _usuario(
            'Duda Lima', 'duda.login', email='duda@opao.online',
            senha_provisoria=True)
        concluido = _usuario(
            'Enzo Luz', 'enzo.login', email='enzo@opao.online',
            senha_provisoria=False)
        _funcionario('Duda Lima', '81000000065',
                     email='duda@opao.online', usuario=pendente)
        _funcionario('Enzo Luz', '81000000066',
                     email='enzo@opao.online', usuario=concluido)

    html = _owner(app, owner_user).get(
        '/rh/funcionarios?view=acessos&acesso=vinculados&ativos=1'
    ).get_data(as_text=True)

    assert 'Enviar novo acesso aos pendentes' in html
    assert '1 funcionário(s) ativo(s)' in html
    assert '/rh/funcionarios/acessos/reenviar-pendentes' in html


def test_reenviar_pendentes_so_processa_ativo_com_senha_provisoria(
        app, owner_user):
    with app.app_context():
        pendente_u = _usuario(
            'Fabi Sol', 'fabi.login', email='fabi@opao.online',
            senha_provisoria=True)
        concluido_u = _usuario(
            'Gabi Mar', 'gabi.login', email='gabi@opao.online',
            senha_provisoria=False)
        inativo_u = _usuario(
            'Hugo Reis', 'hugo.login', email='hugo@opao.online',
            senha_provisoria=True)
        _funcionario('Fabi Sol', '81000000067',
                     email='fabi@opao.online', usuario=pendente_u)
        _funcionario('Gabi Mar', '81000000068',
                     email='gabi@opao.online', usuario=concluido_u)
        inativo = _funcionario(
            'Hugo Reis', '81000000069', email='hugo@opao.online',
            usuario=inativo_u)
        inativo.ativo = False
        db.session.commit()
        hashes = {
            pendente_u.id: pendente_u.senha_hash,
            concluido_u.id: concluido_u.senha_hash,
            inativo_u.id: inativo_u.senha_hash,
        }
        ids = pendente_u.id, concluido_u.id, inativo_u.id

    cliente = _owner(app, owner_user)
    with patch('app.services.email.enviar_boas_vindas',
               return_value={'ok': True, 'id': 'msg-pendente'}) as enviar:
        cancelada = cliente.post(
            '/rh/funcionarios/acessos/reenviar-pendentes',
            data={'confirmacao': 'nao'}, follow_redirects=True)
        assert 'Reenvio cancelado' in cancelada.get_data(as_text=True)
        enviar.assert_not_called()

        resposta = cliente.post(
            '/rh/funcionarios/acessos/reenviar-pendentes',
            data={'confirmacao': 'PENDENTES'}, follow_redirects=True)

    html = resposta.get_data(as_text=True)
    assert '1 novo(s) acesso(s) pendente(s)' in html
    enviar.assert_called_once()
    assert enviar.call_args.args[0] == 'fabi@opao.online'
    with app.app_context():
        assert db.session.get(Usuario, ids[0]).senha_hash != hashes[ids[0]]
        assert db.session.get(Usuario, ids[1]).senha_hash == hashes[ids[1]]
        assert db.session.get(Usuario, ids[2]).senha_hash == hashes[ids[2]]


def test_reenviar_todos_exige_confirmacao_e_processa_somente_ativos(
        app, owner_user):
    with app.app_context():
        ativo_u = _usuario('Davi Sol', 'davi.login',
                           email='davi@opao.online')
        ativo = _funcionario('Davi Sol', '81000000063',
                             email='davi@opao.online', usuario=ativo_u)
        inativo_u = _usuario('Eva Mar', 'eva.login',
                             email='eva@opao.online')
        inativo = _funcionario('Eva Mar', '81000000064',
                               email='eva@opao.online', usuario=inativo_u)
        inativo.ativo = False
        db.session.commit()
        ativo_uid, inativo_uid = ativo_u.id, inativo_u.id
        hash_ativo, hash_inativo = ativo_u.senha_hash, inativo_u.senha_hash

    cliente = _owner(app, owner_user)
    with patch('app.services.email.enviar_boas_vindas',
               return_value={'ok': True, 'id': 'msg-lote'}) as enviar:
        cancelada = cliente.post('/rh/funcionarios/acessos/reenviar-todos',
                                 data={'confirmacao': 'nao'},
                                 follow_redirects=True)
        assert 'Reenvio cancelado' in cancelada.get_data(as_text=True)
        enviar.assert_not_called()

        resposta = cliente.post('/rh/funcionarios/acessos/reenviar-todos',
                                data={'confirmacao': 'REENVIAR'},
                                follow_redirects=True)

    html = resposta.get_data(as_text=True)
    assert '1 novo(s) acesso(s) aceito(s)' in html
    enviar.assert_called_once()
    assert enviar.call_args.args[0] == 'davi@opao.online'
    with app.app_context():
        assert db.session.get(Usuario, ativo_uid).senha_hash != hash_ativo
        assert db.session.get(Usuario, inativo_uid).senha_hash == hash_inativo


def test_gerar_recusa_quando_email_ja_pertence_a_conta(app, owner_user):
    with app.app_context():
        existente = _usuario('João Antigo', 'joao.antigo',
                             email='joao@opao.online')
        funcionario = _funcionario('João Antigo', '81000000007')
        fid, uid = funcionario.id, existente.id
        total_antes = Usuario.query.count()

    resposta = _owner(app, owner_user).post(
        f'/rh/funcionarios/{fid}/acesso',
        data={'acao': 'gerar', 'usuario_id': '',
              'email': 'joao@opao.online',
              'filtro_acesso': 'pendentes', 'apenas_ativos': '1'},
        follow_redirects=True)

    html = resposta.get_data(as_text=True)
    assert 'Já existe a conta' in html and 'Vincular conta' in html
    with app.app_context():
        assert db.session.get(Funcionario, fid).usuario_id is None
        assert db.session.get(Usuario, uid) is not None
        assert Usuario.query.count() == total_antes


def test_conta_administrativa_comum_aparece_e_pode_ser_vinculada(
        app, owner_user):
    with app.app_context():
        admin = _usuario('Administrador', 'admin.operacao', papel='admin')
        funcionario = _funcionario('Operador', '81000000008')
        aid, fid = admin.id, funcionario.id
        hash_antes = admin.senha_hash

    cliente = _owner(app, owner_user)
    html = cliente.get(
        '/rh/funcionarios?view=acessos&acesso=todos').get_data(as_text=True)
    assert 'admin.operacao' in html

    resposta = cliente.post(
        f'/rh/funcionarios/{fid}/acesso',
        data={'acao': 'vincular', 'usuario_id': aid,
              'email': 'operador@opao.online',
              'filtro_acesso': 'pendentes', 'apenas_ativos': '1'},
        follow_redirects=True)
    assert 'login e a senha continuam os mesmos' in resposta.get_data(
        as_text=True)
    with app.app_context():
        assert db.session.get(Funcionario, fid).usuario_id == aid
        conta = db.session.get(Usuario, aid)
        assert conta.papel == 'admin'
        assert conta.senha_hash == hash_antes
