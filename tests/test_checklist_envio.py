"""Envio recuperável: recibo atômico, repetição segura e isolamento do autor."""
from datetime import timedelta
from io import BytesIO
from unittest.mock import patch
from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models import ChecklistEnvio, ChecklistPreenchimento, ChecklistResponsavel, ChecklistResposta
from app.services import checklist_loja
from app.utils import agora
from tests.test_checklist_acessos import _cliente, _ClienteIsolado, _pessoa
from tests.test_checklist_loja import _item, _loja, _resp, _user

HEADERS = {'X-Checklist-Request': '1', 'Accept': 'application/json'}


@pytest.fixture
def dropbox_ok():
    with patch('app.services.dropbox_storage.disponivel', return_value=True), \
         patch('app.utils.comprimir_imagem', side_effect=lambda b, **kw: b), \
         patch('app.services.dropbox_storage.upload_publico', return_value={
             'url': 'https://example.invalid/checklist.jpg', 'storage_path': '/teste.jpg',
         }) as upload:
        yield upload


@pytest.fixture
def envio(app):
    loja = _loja()
    usuario = _user(loja_id=loja.id)
    item = _item()
    cliente = _cliente(app, usuario)
    return cliente, loja, usuario, item


def _dados(envio, token=None, **extra):
    _, loja, _, item = envio
    atual = checklist_loja.itens_para(loja.id, 'abertura')[0]
    return {
        'loja': str(loja.id), 'tipo': 'abertura', 'envio_token': token or uuid4().hex,
        'revisao_itens': '1', 'itens_presentes': str(item.id),
        f'versao_{item.id}': atual.versao, f'ok_{item.id}': 'ok', **extra,
    }


def _post(envio, dados):
    return envio[0].post('/checklist/preencher', data=dados, headers=HEADERS)


def _status(envio, token, **extra):
    return envio[0].get('/checklist/envio-status', query_string={
        'loja': envio[1].id, 'tipo': 'abertura', 'token': token, **extra,
    }, headers=HEADERS)


def test_get_fornece_token_novo_e_dia_para_rascunho(envio):
    cliente, loja, _, _ = envio
    with patch('app.blueprints.checklist.routes.render_template', return_value='form') as render:
        assert cliente.get(f'/checklist/preencher?loja={loja.id}&tipo=abertura').status_code == 200
        primeiro = render.call_args.kwargs
        assert checklist_loja.validar_envio_token(primeiro['envio_token'])
        assert primeiro['checklist_dia'] == checklist_loja._data_do_registro('abertura').isoformat()
        cliente.get(f'/checklist/preencher?loja={loja.id}&tipo=abertura')
        assert render.call_args.kwargs['envio_token'] != primeiro['envio_token']


def test_grava_recibo_respostas_e_url_de_confirmacao(envio):
    dados = _dados(envio)
    antes = _status(envio, dados['envio_token'])
    assert antes.json == {'ok': True, 'salvo': False}
    assert ChecklistEnvio.query.count() == 0
    resposta = _post(envio, dados)
    assert resposta.status_code == 200
    assert resposta.json['ok'] and resposta.json['salvo']
    assert resposta.json['duplicado'] is False
    p = ChecklistPreenchimento.query.one()
    assert resposta.json['preenchimento_id'] == p.id
    assert resposta.json['url_confirmacao'] == f'/checklist/comprovante/{p.id}'
    assert 'Checklist salvo' in resposta.json['mensagem']
    assert len(p.respostas) == 1
    recibo = ChecklistEnvio.query.one()
    assert (recibo.token, recibo.usuario_id, recibo.loja_id, recibo.tipo) == (
        dados['envio_token'], envio[2].id, envio[1].id, 'abertura')
    recuperado = _status(envio, dados['envio_token'])
    assert recuperado.json['preenchimento_id'] == p.id
    assert recuperado.json['duplicado'] is True
    assert 'no-store' in recuperado.headers['Cache-Control']


def test_replay_apos_30s_antes_da_revisao_e_upload(envio, dropbox_ok):
    _, _, _, item = envio
    item.exige_foto = True
    db.session.commit()
    dados = _dados(envio)
    dados[f'foto_{item.id}'] = (BytesIO(b'foto'), 'foto.jpg')
    primeiro = _post(envio, dados)
    assert primeiro.status_code == 200
    p = ChecklistPreenchimento.query.one()
    p.criado_em = agora() - timedelta(minutes=15)
    item.texto = 'Checklist alterado depois de salvo'
    item.ativo = False  # Até a remoção dos itens não invalida o recibo.
    db.session.commit()
    del dados[f'foto_{item.id}']
    segundo = _post(envio, dados)
    assert segundo.status_code == 200
    assert segundo.json['duplicado'] is True
    assert segundo.json['preenchimento_id'] == primeiro.json['preenchimento_id']
    assert ChecklistPreenchimento.query.count() == 1
    assert ChecklistResposta.query.count() == 1
    assert dropbox_ok.call_count == 1


def test_token_novo_dentro_de_30s_vincula_mesmo_recibo(envio):
    primeiro = _post(envio, _dados(envio)).json
    novo = _dados(envio)
    segundo = _post(envio, novo)
    assert segundo.json['duplicado'] is True
    assert segundo.json['preenchimento_id'] == primeiro['preenchimento_id']
    assert ChecklistPreenchimento.query.count() == 1
    assert ChecklistEnvio.query.count() == 2
    p = ChecklistPreenchimento.query.one()
    p.criado_em = agora() - timedelta(minutes=2)
    db.session.commit()
    assert _post(envio, novo).json['preenchimento_id'] == p.id
    assert ChecklistPreenchimento.query.count() == 1


def test_preenchimento_legado_recente_tambem_ganha_recibo(envio):
    _, loja, usuario, item = envio
    p = checklist_loja.registrar(loja, 'abertura', usuario.id, _resp([item]))
    dados = _dados(envio)
    assert _post(envio, dados).json['preenchimento_id'] == p.id
    assert ChecklistEnvio.query.one().preenchimento_id == p.id


def test_novo_token_apos_30s_permite_novo_preenchimento_intencional(envio):
    _post(envio, _dados(envio))
    ChecklistPreenchimento.query.one().criado_em = agora() - timedelta(minutes=2)
    db.session.commit()
    resposta = _post(envio, _dados(envio))
    assert resposta.json['duplicado'] is False
    assert ChecklistPreenchimento.query.count() == 2


@pytest.mark.parametrize('token', ['', 'errado', 'a' * 31, 'g' * 32, 'a' * 33,
                                   '12345678-1234-1234-1234-123456789012'])
def test_token_invalido_nao_grava(envio, token):
    dados = _dados(envio)
    dados['envio_token'] = token
    assert _post(envio, dados).status_code == 400
    assert _status(envio, token).status_code == 400
    assert ChecklistPreenchimento.query.count() == 0
    assert ChecklistEnvio.query.count() == 0


def test_post_json_exige_token(envio):
    dados = _dados(envio)
    del dados['envio_token']
    resposta = _post(envio, dados)
    assert resposta.status_code == 400
    assert resposta.json['ok'] is False
    assert ChecklistPreenchimento.query.count() == 0


@pytest.mark.parametrize('escopo', ['usuario', 'loja', 'tipo'])
def test_token_nao_autoriza_outro_usuario_loja_ou_tipo(envio, app, escopo):
    dados = _dados(envio)
    _post(envio, dados)
    cliente, loja, usuario, item = envio
    extras = {}
    if escopo == 'usuario':
        usuario = _user('outro')
        cliente = _cliente(app, usuario)
    elif escopo == 'loja':
        loja = _loja('Outra')
        extras['loja'] = loja.id
    else:
        extras['tipo'] = 'fechamento'
    alvo = (cliente, loja, usuario, item)
    dados.update(extras)
    resposta = _post(alvo, dados)
    status = _status(alvo, dados['envio_token'], **extras)
    assert resposta.status_code == status.status_code == 403
    assert 'preenchimento_id' not in resposta.json
    assert 'preenchimento_id' not in status.json
    assert ChecklistPreenchimento.query.count() == 1
    assert ChecklistEnvio.query.count() == 1


def test_validacao_preserva_form_e_nao_registra_sucesso(envio):
    dados = _dados(envio)
    dados[f'ok_{envio[3].id}'] = 'problema'
    resposta = _post(envio, dados)
    assert resposta.status_code == 422
    assert resposta.json['erro'] == 'validacao'
    assert 'observação' in resposta.json['mensagem']
    assert _status(envio, dados['envio_token']).json['salvo'] is False
    dados[f'obs_{envio[3].id}'] = 'Faltou troco'
    assert _post(envio, dados).json['ok'] is True
    assert ChecklistResposta.query.one().observacao == 'Faltou troco'


def test_conflito_de_revisao_devolve_json_sem_perder_token(envio):
    dados = _dados(envio)
    envio[3].texto = 'Texto modificado'
    db.session.commit()
    resposta = _post(envio, dados)
    assert resposta.status_code == 409
    assert resposta.json['erro'] == 'checklist_alterado'
    assert 'Location' not in resposta.headers
    assert ChecklistPreenchimento.query.count() == 0
    assert _status(envio, dados['envio_token']).json['salvo'] is False


def test_falha_upload_nao_grava_recibo_e_permite_tentar_novamente(envio, dropbox_ok):
    item = envio[3]
    item.exige_foto = True
    db.session.commit()
    dados = _dados(envio)
    dados[f'foto_{item.id}'] = (BytesIO(b'foto'), 'foto.jpg')
    with patch('app.services.dropbox_storage.disponivel', return_value=False):
        resposta = _post(envio, dados)
    assert resposta.status_code == 422
    assert ChecklistEnvio.query.count() == ChecklistPreenchimento.query.count() == 0
    dados[f'foto_{item.id}'] = (BytesIO(b'foto'), 'foto.jpg')
    assert _post(envio, dados).json['ok'] is True


def test_erro_na_transacao_reverte_respostas_e_recibo(envio):
    _, loja, usuario, item = envio
    with patch.object(db.session, 'commit', side_effect=RuntimeError('banco indisponível')):
        with pytest.raises(RuntimeError, match='banco indisponível'):
            checklist_loja.registrar(loja, 'abertura', usuario.id, _resp([item]),
                                     envio_token=uuid4().hex)
    assert ChecklistPreenchimento.query.count() == 0
    assert ChecklistResposta.query.count() == 0
    assert ChecklistEnvio.query.count() == 0


def test_colisao_token_reverte_novo_preenchimento_inteiro(envio):
    _, loja, usuario, item = envio
    token = uuid4().hex
    p = checklist_loja.registrar(loja, 'abertura', usuario.id, _resp([item]), envio_token=token)
    with pytest.raises(IntegrityError):
        checklist_loja.registrar(loja, 'abertura', usuario.id, _resp([item]), envio_token=token)
    assert ChecklistPreenchimento.query.one().id == p.id
    assert ChecklistResposta.query.count() == ChecklistEnvio.query.count() == 1


def test_html_legado_sem_token_continua_funcionando(envio):
    dados = _dados(envio)
    del dados['envio_token']
    resposta = envio[0].post('/checklist/preencher', data=dados)
    assert resposta.status_code == 302
    assert ChecklistPreenchimento.query.count() == 1
    assert ChecklistEnvio.query.count() == 0


def test_html_reexibido_preserva_token_e_respostas(envio):
    dados = _dados(envio)
    dados[f'ok_{envio[3].id}'] = 'problema'
    with patch('app.blueprints.checklist.routes.render_template', return_value='form') as render:
        resposta = envio[0].post('/checklist/preencher', data=dados)
    assert resposta.status_code == 422
    assert render.call_args.kwargs['envio_token'] == dados['envio_token']
    assert render.call_args.kwargs['form'][f'ok_{envio[3].id}'] == 'problema'


def test_comprovante_so_autor_ou_admin(envio, app, admin_user):
    dados = _post(envio, _dados(envio)).json
    with patch('app.blueprints.checklist.routes.render_template', return_value='comprovante'):
        resposta = envio[0].get(dados['url_confirmacao'])
        assert resposta.status_code == 200
        assert 'no-store' in resposta.headers['Cache-Control']
        assert _cliente(app, _user('estranho')).get(dados['url_confirmacao']).status_code == 403
        assert _cliente(app, admin_user).get(dados['url_confirmacao']).status_code == 200


def test_status_e_comprovante_exigem_login(envio, app):
    dados = _post(envio, _dados(envio)).json
    cliente = _ClienteIsolado(app, app.response_class)
    assert cliente.get(dados['url_confirmacao']).status_code == 302
    assert cliente.get('/checklist/envio-status', query_string={
        'loja': envio[1].id, 'tipo': 'abertura', 'token': uuid4().hex,
    }).status_code == 302


def test_json_nao_contorna_csrf(envio, app):
    app.config['WTF_CSRF_ENABLED'] = True
    resposta = _post(envio, _dados(envio))
    assert resposta.status_code == 400
    assert ChecklistPreenchimento.query.count() == 0


def test_responsavel_somente_treino_acessa_recibo_sem_liberar_rh(envio, app):
    pessoa = _pessoa('Responsavel vinculado', envio[1], cargo='GERENTE')
    db.session.add(ChecklistResponsavel(funcionario_id=pessoa.id, loja_id=envio[1].id,
                                        periodo='Manhã', criado_por_id=envio[2].id))
    db.session.commit()
    cliente = _cliente(app, pessoa.usuario)
    contexto = (cliente, envio[1], pessoa.usuario, envio[3])
    dados = _dados(contexto)
    resposta = _post(contexto, dados)
    assert resposta.status_code == 200
    assert _status(contexto, dados['envio_token']).json['salvo'] is True
    with patch('app.blueprints.checklist.routes.render_template', return_value='comprovante'):
        assert cliente.get(resposta.json['url_confirmacao']).status_code == 200
    assert cliente.get('/rh/funcionarios').status_code in (302, 403)
