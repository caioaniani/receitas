"""Consulta manual da NF existente: acesso, POST e retorno nos detalhes B2B."""
from unittest.mock import patch

import pytest

from app.extensions import db
from app.models import FaturaB2B, Usuario
from app.utils import agora, hoje
from tests.test_b2b_emitir_nf import _cliente_completo, _venda
from tests.test_central_cobrancas import _client


@pytest.fixture(params=['vendas', 'faturas'])
def documento(request, app):
    cliente = _cliente_completo()
    if request.param == 'vendas':
        doc = _venda(cliente)
    else:
        doc = FaturaB2B(cliente_id=cliente.id, data_inicio=hoje(),
                       data_fim=hoje(), vencimento=hoje(), valor_total=100)
        db.session.add(doc)
    doc.tiny_nota_fiscal_id = 'nf-existente'
    doc.nf_status = 'rejeitada'
    db.session.commit()
    return doc, f'/b2b/{request.param}/{doc.id}'


@pytest.mark.parametrize('tipo', ['vendas', 'faturas'])
def test_sincronizar_nf_exige_login(app, tipo):
    with patch('app.services.cobrancas_nf.sincronizar') as sincronizar:
        resposta = app.test_client().post(f'/b2b/{tipo}/1/sincronizar-nf')
    assert resposta.status_code == 302
    assert '/auth/login' in resposta.location
    sincronizar.assert_not_called()


def test_sincronizar_nf_usuario_sem_admin_403(app, documento):
    usuario = Usuario(nome='Produção', login='producao', papel='padeiro')
    usuario.set_senha('123')
    db.session.add(usuario)
    db.session.commit()
    _, detalhe = documento
    with patch('app.services.cobrancas_nf.sincronizar') as sincronizar:
        resposta = _client(app, usuario).post(f'{detalhe}/sincronizar-nf')
    assert resposta.status_code == 403
    sincronizar.assert_not_called()


def test_sincronizar_nf_nao_aceita_get(app, admin_user, documento):
    _, detalhe = documento
    with patch('app.services.cobrancas_nf.sincronizar') as sincronizar:
        resposta = _client(app, admin_user).get(f'{detalhe}/sincronizar-nf')
    assert resposta.status_code == 405
    sincronizar.assert_not_called()


@pytest.mark.parametrize('retorno,categoria', [
    ({'ok': True, 'autorizada': True, 'msg': 'NF autorizada no Tiny.'}, 'success'),
    ({'ok': True, 'autorizada': False, 'msg': 'Nota ainda rejeitada.'}, 'info'),
    ({'ok': False, 'autorizada': False, 'msg': 'Consulta indisponível.'}, 'warning'),
])
def test_sincronizar_nf_admin_consulta_e_volta_ao_detalhe(
        app, admin_user, documento, retorno, categoria):
    doc, detalhe = documento
    client = _client(app, admin_user)
    assert not admin_user.pode_emitir_nf_b2b()
    with patch('app.services.cobrancas_nf.sincronizar',
               return_value=retorno) as sincronizar:
        resposta = client.post(f'{detalhe}/sincronizar-nf')
    assert resposta.status_code == 302
    assert resposta.location.endswith(detalhe)
    sincronizar.assert_called_once_with(doc)
    with client.session_transaction() as sessao:
        assert any(nivel == categoria and retorno['msg'] in mensagem
                   for nivel, mensagem in sessao['_flashes'])


@pytest.mark.parametrize('tipo', ['vendas', 'faturas'])
def test_sincronizar_nf_documento_inexistente_404(app, admin_user, tipo):
    with patch('app.services.cobrancas_nf.sincronizar') as sincronizar:
        resposta = _client(app, admin_user).post(f'/b2b/{tipo}/999/sincronizar-nf')
    assert resposta.status_code == 404
    sincronizar.assert_not_called()


@pytest.mark.parametrize('emitida', [False, True])
def test_detalhe_exibe_consulta_apenas_para_nf_pendente(
        app, admin_user, documento, emitida):
    doc, detalhe = documento
    if emitida:
        doc.nf_emitida_em = agora()
        doc.nf_status = 'autorizada'
        db.session.commit()
    with patch('app.services.cobrancas_nf.sincronizar') as sincronizar:
        resposta = _client(app, admin_user).get(detalhe)
    assert resposta.status_code == 200
    html = resposta.get_data(as_text=True)
    assert ('Atualizar situação do Tiny' in html) is not emitida
    assert (f'action="{detalhe}/sincronizar-nf"' in html) is not emitida
    sincronizar.assert_not_called()
