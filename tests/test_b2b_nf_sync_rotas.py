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
def test_detalhe_exibe_consulta_para_nf_pendente_ou_emitida(
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
    assert 'Atualizar situação do Tiny' in html
    assert html.count(f'action="{detalhe}/sincronizar-nf"') == 1
    sincronizar.assert_not_called()


@pytest.mark.parametrize('numero', [None, '012693'])
def test_detalhe_sem_id_oferece_localizacao_quando_tem_numero(
        app, admin_user, documento, numero):
    doc, detalhe = documento
    doc.tiny_nota_fiscal_id = None
    doc.nf_numero = numero
    db.session.commit()
    with patch('app.services.cobrancas_nf.sincronizar') as sincronizar:
        resposta = _client(app, admin_user).get(detalhe)
    assert resposta.status_code == 200
    html = resposta.get_data(as_text=True)
    assert ('Localizar NF existente no Tiny' in html) is bool(numero)
    assert (f'action="{detalhe}/sincronizar-nf"' in html) is bool(numero)
    assert 'Atualizar situação do Tiny' not in html
    if numero:
        assert f'Localize a NF nº <strong>{numero}</strong>' in html
    sincronizar.assert_not_called()


def test_detalhe_duplicidade_sem_id_oferece_localizacao_em_vez_de_emissao(
        app, owner_user, documento):
    doc, detalhe = documento
    doc.tiny_nota_fiscal_id = None
    doc.nf_numero = '012693'
    doc.status = 'fechada' if isinstance(doc, FaturaB2B) else 'confirmada'
    db.session.commit()
    assert owner_user.pode_emitir_nf_b2b()
    erro = 'Registro em duplicidade - Nota fiscal já cadastrada'
    with patch('app.services.cobrancas_nf.ultimo_erro', return_value=erro):
        resposta = _client(app, owner_user).get(detalhe)
    assert resposta.status_code == 200
    html = resposta.get_data(as_text=True)
    assert erro in html
    assert 'Localizar NF existente no Tiny' in html
    assert f'action="{detalhe}/emitir-nf"' not in html


def test_detalhe_primeira_emissao_continua_disponivel(
        app, owner_user, documento):
    doc, detalhe = documento
    doc.tiny_nota_fiscal_id = None
    doc.nf_numero = None
    doc.status = 'fechada' if isinstance(doc, FaturaB2B) else 'confirmada'
    db.session.commit()
    resposta = _client(app, owner_user).get(detalhe)
    assert resposta.status_code == 200
    assert f'action="{detalhe}/emitir-nf"' in resposta.get_data(as_text=True)


def test_localizar_nf_sem_id_usa_mesma_rota_de_consulta(
        app, admin_user, documento):
    doc, detalhe = documento
    doc.tiny_nota_fiscal_id = None
    doc.nf_numero = '012693'
    db.session.commit()
    retorno = {'ok': True, 'autorizada': True, 'msg': 'NF localizada no Tiny.'}
    with patch('app.services.cobrancas_nf.sincronizar',
               return_value=retorno) as sincronizar, \
         patch('app.services.tiny_nf_b2b.emitir_nf') as emitir_venda, \
         patch('app.services.tiny_nf_b2b.emitir_nf_fatura') as emitir_fatura:
        resposta = _client(app, admin_user).post(f'{detalhe}/sincronizar-nf')
    assert resposta.status_code == 302
    assert resposta.location.endswith(detalhe)
    sincronizar.assert_called_once_with(doc)
    emitir_venda.assert_not_called()
    emitir_fatura.assert_not_called()
