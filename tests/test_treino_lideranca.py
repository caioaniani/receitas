"""Hierarquia de liderança e observação prática da equipe."""
from datetime import timedelta

import pytest

from app.extensions import db
from app.models import (
    Funcionario,
    Loja,
    TreinoAplicacaoPratica,
    TreinoTemporada,
    TreinoTrilha,
    Usuario,
)
from app.services import treino_lideranca as lideranca
from app.utils import hoje


def _login(app, usuario_id):
    client = app.test_client()
    with client.session_transaction() as sessao:
        sessao['_user_id'] = str(usuario_id)
        sessao['_fresh'] = True
    return client


def _pessoa(nome, cpf, *, papel='funcionario', com_acesso=True):
    usuario = None
    if com_acesso:
        usuario = Usuario(
            nome=nome, login=f'{cpf}@teste.local', papel=papel,
            somente_treino=True)
        usuario.set_senha('senha-forte')
        db.session.add(usuario)
        db.session.flush()
    funcionario = Funcionario(
        nome=nome, cpf=cpf, ativo=True,
        usuario_id=usuario.id if usuario else None)
    db.session.add(funcionario)
    db.session.commit()
    return usuario, funcionario


def _temporada():
    temporada = TreinoTemporada(
        nome='Ciclo atual', inicio=hoje() - timedelta(days=1),
        fim=hoje() + timedelta(days=30), status='ATIVA')
    db.session.add(temporada)
    db.session.commit()
    return temporada


def test_lider_comum_ganha_minha_equipe_e_ve_so_liderados(app):
    with app.app_context():
        usuario, gestor = _pessoa('Lider Ana', '101')
        _, liderado = _pessoa('Bruno Liderado', '102')
        _, outra = _pessoa('Carla Outra Equipe', '103')
        liderado.lider_id = gestor.id
        db.session.commit()
        usuario_id = usuario.id
        liderado_id = liderado.id
        outra_id = outra.id

    client = _login(app, usuario_id)
    resposta = client.get('/treino/gestor/')
    corpo = resposta.get_data(as_text=True)
    assert resposta.status_code == 200
    assert 'Bruno Liderado' in corpo
    assert 'Carla Outra Equipe' not in corpo
    assert 'Minha equipe' in corpo
    assert client.get(
        f'/treino/gestor/observar/{liderado_id}').status_code == 200
    assert client.get(
        f'/treino/gestor/observar/{outra_id}').status_code == 403


def test_hierarquia_recusa_autolideranca_e_ciclo(app):
    with app.app_context():
        _, ana = _pessoa('Ana', '201')
        _, bia = _pessoa('Bia', '202')
        with pytest.raises(lideranca.LiderancaError):
            lideranca.salvar_vinculos([ana, bia], {
                ana.id: ana.id, bia.id: None})
        with pytest.raises(lideranca.LiderancaError):
            lideranca.salvar_vinculos([ana, bia], {
                ana.id: bia.id, bia.id: ana.id})
        assert ana.lider_id is None and bia.lider_id is None


def test_checklist_preserva_historico_ao_editar(app):
    with app.app_context():
        trilha = TreinoTrilha(nome='Atendimento')
        db.session.add(trilha)
        db.session.commit()
        checklist = lideranca.salvar_checklist(
            trilha, 'Atendimento no balcão',
            ['Cumprimenta o cliente', 'Confirma o pedido'])
        ids = [item.id for item in checklist.itens]

        checklist = lideranca.salvar_checklist(
            trilha, 'Atendimento no balcão', ['Recebe bem o cliente'])
        db.session.refresh(checklist)
        antigos = {item.id: item for item in checklist.itens if item.id in ids}
        novo = next(item for item in checklist.itens
                    if item.descricao == 'Recebe bem o cliente')
        assert novo.id not in ids and novo.ativo is True
        assert antigos[ids[0]].ativo is False
        assert antigos[ids[1]].ativo is False


def test_lider_registra_checklist_do_liderado(app):
    with app.app_context():
        usuario, gestor = _pessoa('Lider Ana', '301')
        _, liderado = _pessoa('Bruno Liderado', '302')
        liderado.lider_id = gestor.id
        trilha = TreinoTrilha(nome='Cultura')
        db.session.add(trilha)
        db.session.commit()
        checklist = lideranca.salvar_checklist(
            trilha, 'Aplicação no trabalho',
            ['Demonstra respeito', 'Pede ajuda quando necessário'])
        _temporada()
        usuario_id = usuario.id
        gestor_id = gestor.id
        liderado_id = liderado.id
        trilha_id = trilha.id
        item_ids = [item.id for item in checklist.itens]

    client = _login(app, usuario_id)
    pagina = client.get(f'/treino/gestor/observar/{liderado_id}')
    corpo = pagina.get_data(as_text=True)
    assert pagina.status_code == 200
    assert 'Demonstra respeito' in corpo
    assert 'Pede ajuda quando necessário' in corpo

    incompleta = client.post(
        f'/treino/gestor/observar/{liderado_id}/{trilha_id}', data={
            'itens_ok': str(item_ids[0]),
            'evidencia': 'Ainda falta observar uma parte do procedimento.',
        }, follow_redirects=True)
    assert 'Confirme todos os itens' in incompleta.get_data(as_text=True)
    with app.app_context():
        assert TreinoAplicacaoPratica.query.count() == 0

    resposta = client.post(
        f'/treino/gestor/observar/{liderado_id}/{trilha_id}', data={
            'itens_ok': [str(item_id) for item_id in item_ids],
            'evidencia': 'Observei durante o atendimento completo ao cliente.',
        }, follow_redirects=True)
    assert resposta.status_code == 200
    assert 'registrada com sucesso' in resposta.get_data(as_text=True)
    with app.app_context():
        registro = TreinoAplicacaoPratica.query.one()
        assert registro.funcionario_id == liderado_id
        assert registro.gestor_id == gestor_id
        assert registro.itens_ok == item_ids


def test_owner_configura_hierarquia_pelo_rh(app, owner_user):
    with app.app_context():
        _, lider = _pessoa('Lider Conta', '401')
        _, liderado = _pessoa('Pessoa Liderada', '402', com_acesso=False)
        lider_id = lider.id
        liderado_id = liderado.id
        owner_id = owner_user.id

    client = _login(app, owner_id)
    pagina = client.get('/rh/lideranca')
    assert pagina.status_code == 200
    assert 'Quem acompanha quem' in pagina.get_data(as_text=True)
    resposta = client.post('/rh/lideranca/vinculos', data={
        f'lider_{lider_id}': '',
        f'lider_{liderado_id}': str(lider_id),
    }, follow_redirects=True)
    assert resposta.status_code == 200
    with app.app_context():
        assert db.session.get(Funcionario, liderado_id).lider_id == lider_id


def test_dakson_acessa_apenas_formulario_de_organizacao(app):
    with app.app_context():
        dakson, _ = _pessoa('Dakson', '501', papel='admin')
        dakson.login = 'dakson'
        dakson.somente_treino = True
        _, colega = _pessoa('Colega da Equipe', '502', com_acesso=False)
        colega.salario_base = 9876
        db.session.commit()
        dakson_id = dakson.id

    client = _login(app, dakson_id)
    pagina = client.get('/rh/lideranca/preenchimento')
    corpo = pagina.get_data(as_text=True)
    assert pagina.status_code == 200
    assert 'Organizar equipe' in corpo
    assert 'Ver organograma' in corpo
    assert 'Colega da Equipe' in corpo
    assert '9876' not in corpo
    assert 'Checklists de observação' not in corpo
    funcionarios = client.get('/rh/funcionarios')
    lideranca_rh = client.get('/rh/lideranca')
    assert funcionarios.status_code == 302
    assert funcionarios.location.endswith('/treino/')
    assert lideranca_rh.status_code == 302
    assert lideranca_rh.location.endswith('/treino/')
    assert client.get('/rh/lideranca/organograma').status_code == 200
    pdf = client.get('/rh/lideranca/organograma.pdf')
    assert pdf.status_code == 200
    assert pdf.mimetype == 'application/pdf'


def test_outro_admin_nao_acessa_formulario_de_organizacao(app):
    with app.app_context():
        admin, _ = _pessoa('Outro Admin', '511', papel='admin')
        admin.somente_treino = False
        db.session.commit()
        admin_id = admin.id

    client = _login(app, admin_id)
    assert client.get('/rh/lideranca/preenchimento').status_code == 403
    assert client.get('/rh/lideranca/organograma').status_code == 403
    assert client.get('/rh/lideranca/organograma.pdf').status_code == 403
    assert client.post(
        '/rh/lideranca/preenchimento/salvar', data={}).status_code == 403


def test_dakson_salva_lider_unidade_e_periodo_sem_apagar_unidade_secundaria(
        app):
    with app.app_context():
        dakson, _ = _pessoa('Dakson', '521', papel='admin')
        dakson.login = 'dakson'
        dakson.somente_treino = True
        _, lider = _pessoa('Lider da Loja', '522')
        _, liderado = _pessoa('Pessoa Liderada', '523', com_acesso=False)
        principal = Loja(nome='Loja Principal', ativa=True)
        secundaria = Loja(nome='Loja Secundaria', ativa=True)
        db.session.add_all([principal, secundaria])
        db.session.flush()
        liderado.lojas.append(secundaria)
        db.session.commit()
        ids = {
            'dakson': dakson.id, 'lider': lider.id,
            'liderado': liderado.id, 'principal': principal.id,
            'secundaria': secundaria.id,
        }

    client = _login(app, ids['dakson'])
    resposta = client.post('/rh/lideranca/preenchimento/salvar', data={
        f"lider_{ids['liderado']}": str(ids['lider']),
        f"loja_{ids['liderado']}": str(ids['principal']),
        f"periodo_{ids['liderado']}": 'Manhã',
    }, follow_redirects=True)
    assert resposta.status_code == 200
    assert 'Organização salva' in resposta.get_data(as_text=True)
    with app.app_context():
        pessoa = db.session.get(Funcionario, ids['liderado'])
        assert pessoa.lider_id == ids['lider']
        assert pessoa.periodo == 'Manhã'
        assert {loja.id for loja in pessoa.lojas} == {
            ids['principal'], ids['secundaria']}
        assert lideranca.unidades_principais([pessoa]) == {
            pessoa.id: ids['principal']}


def test_estrutura_invalida_nao_salva_nenhum_campo(app):
    with app.app_context():
        _, lider = _pessoa('Lider Valido', '531')
        _, liderado = _pessoa('Pessoa da Tarde', '532', com_acesso=False)
        loja = Loja(nome='Loja Teste', ativa=True)
        db.session.add(loja)
        db.session.commit()
        ids = {'lider': lider.id, 'liderado': liderado.id, 'loja': loja.id}

        with pytest.raises(lideranca.LiderancaError):
            lideranca.salvar_estrutura(
                [lider, liderado],
                {lider.id: None, liderado.id: lider.id},
                {lider.id: None, liderado.id: loja.id},
                {lider.id: None, liderado.id: 'Noite'})
        db.session.rollback()

        pessoa = db.session.get(Funcionario, ids['liderado'])
        assert pessoa.lider_id is None
        assert pessoa.periodo is None
        assert pessoa.lojas == []


def test_organograma_mostra_hierarquia_unidade_e_periodo(app, owner_user):
    with app.app_context():
        _, lider = _pessoa('Lider Geral', '541')
        _, supervisora = _pessoa('Supervisora Loja', '542')
        _, atendente = _pessoa('Atendente Equipe', '543', com_acesso=False)
        loja = Loja(nome='Loja Organograma', ativa=True)
        db.session.add(loja)
        db.session.flush()
        lider.lojas.append(loja)
        supervisora.lojas.append(loja)
        atendente.lojas.append(loja)
        lider.periodo = 'Manhã'
        supervisora.periodo = 'Manhã'
        atendente.periodo = 'Tarde'
        supervisora.lider_id = lider.id
        atendente.lider_id = supervisora.id
        db.session.commit()
        owner_id = owner_user.id

    client = _login(app, owner_id)
    resposta = client.get('/rh/lideranca/organograma')
    corpo = resposta.get_data(as_text=True)
    assert resposta.status_code == 200
    assert 'Organograma da equipe' in corpo
    assert 'Exportar PDF' in corpo
    assert 'Loja Organograma' in corpo
    assert 'Manhã' in corpo and 'Tarde' in corpo
    assert 'data-org-level="0"' in corpo
    assert 'data-org-level="1"' in corpo
    assert 'data-org-level="2"' in corpo
    assert corpo.index('Lider Geral') < corpo.index('Supervisora Loja')
    assert corpo.index('Supervisora Loja') < corpo.index('Atendente Equipe')

    pdf = client.get('/rh/lideranca/organograma.pdf')
    assert pdf.status_code == 200
    assert pdf.mimetype == 'application/pdf'
    assert pdf.data.startswith(b'%PDF-')
    assert 'attachment; filename="organograma-equipe-' in (
        pdf.headers['Content-Disposition'])
