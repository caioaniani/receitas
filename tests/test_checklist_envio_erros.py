"""Falhas do multipart devem preservar a página, sem alterar outros formulários."""
from io import BytesIO

import pytest
from flask import abort

from app.models import ChecklistEnvio, ChecklistPreenchimento
from tests.test_checklist_acessos import _cliente

HEADERS = {'X-Checklist-Request': '1', 'Accept': 'application/json'}


def test_csrf_checklist_retorna_erro_json_sem_redirecionar(app, admin_user):
    app.config['WTF_CSRF_ENABLED'] = True
    resposta = _cliente(app, admin_user).post(
        '/checklist/preencher', data={'tipo': 'abertura'}, headers=HEADERS)
    assert resposta.status_code == 400
    assert resposta.json['erro'] == 'csrf_expirada'
    assert 'respostas continuam' in resposta.json['mensagem']
    assert 'Location' not in resposta.headers
    assert ChecklistPreenchimento.query.count() == ChecklistEnvio.query.count() == 0


@pytest.mark.parametrize('headers', [{}, {'Accept': 'application/json'},
                                    {'X-Checklist-Request': '1'}])
def test_csrf_legado_sem_opt_in_completo_preserva_redirecionamento(app, admin_user, headers):
    app.config['WTF_CSRF_ENABLED'] = True
    resposta = _cliente(app, admin_user).post(
        '/checklist/preencher', data={}, headers=headers)
    assert resposta.status_code == 302
    assert not resposta.is_json


def test_upload_acima_do_limite_retorna_413_json_sem_apagar_pagina(app, admin_user):
    app.config['MAX_CONTENT_LENGTH'] = 1024
    resposta = _cliente(app, admin_user).post('/checklist/preencher', data={
        'tipo': 'abertura', 'foto_1': (BytesIO(b'x' * 2048), 'foto.jpg'),
    }, headers=HEADERS)
    assert resposta.status_code == 413
    assert resposta.json['erro'] == 'arquivo_grande'
    assert 'respostas e anexos continuam' in resposta.json['mensagem']
    assert 'Location' not in resposta.headers
    assert ChecklistPreenchimento.query.count() == ChecklistEnvio.query.count() == 0


@pytest.mark.parametrize(('status', 'codigo'), [
    (400, 'envio_incompleto'), (403, 'sem_permissao'), (500, 'falha_servidor'),
])
def test_erros_http_do_checklist_sao_json_para_novo_formulario(
        app, admin_user, monkeypatch, status, codigo):
    def falhar():
        abort(status)

    monkeypatch.setitem(app.view_functions, 'checklist.preencher', falhar)
    resposta = _cliente(app, admin_user).post('/checklist/preencher', headers=HEADERS)
    assert resposta.status_code == status
    assert resposta.json['ok'] is False
    assert resposta.json['erro'] == codigo
    assert resposta.json['mensagem']
    assert 'Location' not in resposta.headers


@pytest.mark.parametrize('status', [400, 403, 500])
def test_erros_de_outros_formularios_continuam_html(app, admin_user, monkeypatch, status):
    def falhar():
        abort(status)

    monkeypatch.setitem(app.view_functions, 'checklist.index', falhar)
    resposta = _cliente(app, admin_user).get('/checklist/', headers=HEADERS)
    assert resposta.status_code == status
    assert not resposta.is_json
