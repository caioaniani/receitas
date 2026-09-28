"""Destinatários adicionais: cadastro, envio e histórico, sem serviços reais."""
import re
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import pytest

from app.extensions import db
from app.models import ClienteB2B, EnvioCobranca
from app.services.central_cobrancas import carregar
from app.services.cobrancas_destinatarios import versao_copias
from app.services.cobrancas_envio import enviar_automatico
from tests.test_b2b_email_docs import _cenario, _preparar_nf
from tests.test_central_cobrancas import _client, _mensal
from tests.test_cobrancas_automacao import _avulsa, _fila, svc


@pytest.fixture
def documentos(request):
    if request.param == 'fatura':
        doc, _, cob = _mensal()
        ref = doc.id
    else:
        _, doc, parcela, cob = _cenario()
        _preparar_nf(doc)
        ref = parcela.id
    doc.cliente.emails_cobranca = ['compras@example.com', 'fiscal@example.com']
    db.session.commit()
    return doc, cob, f'/cobrancas/{request.param}/{ref}/documentos', carregar(request.param, ref)


@pytest.fixture
def provedor(app):
    app.config['POSTMARK_SERVER_TOKEN'] = 'token-de-teste'
    resposta = SimpleNamespace(status_code=200, json=lambda: {'ErrorCode': 0, 'MessageID': 'teste'})
    with patch('app.services.tiny_nf.baixar_danfe_pdf_com_motivo',
               return_value=(b'%PDF-nf-teste', None)) as baixar, \
            patch('app.services.email.requests.post', return_value=resposta) as post:
        yield baixar, post


def _form(client, url, **campos):
    pagina = client.get(url)
    assert pagina.status_code == 200
    versao = re.search(r'name="versao_destinatarios" value="([^"]*)"', pagina.get_data(as_text=True))
    assert versao
    return {'email': 'compras@bomprato.com.br', 'chave': str(uuid4()),
            'versao_destinatarios': versao.group(1), **campos}


def test_cadastro_valida_normaliza_edita_limpa_sem_envio(app, admin_user):
    client = _client(app, admin_user)
    with patch('app.services.email.enviar') as enviar:
        client.post('/b2b/clientes/novo', data={
            'nome': 'Cliente teste', 'email': 'principal@example.com',
            'emails_cobranca': 'Principal@example.com, Compras@example.com;compras@example.com\nfiscal@example.com'})
        cli = ClienteB2B.query.one()
        assert cli.emails_cobranca == ['Compras@example.com', 'fiscal@example.com']
        url = f'/b2b/clientes/{cli.id}/editar'
        # Um formulário antigo não apaga o novo campo.
        client.post(url, data={'nome': cli.nome, 'email': cli.email, 'ativo': '1'})
        assert cli.emails_cobranca == ['Compras@example.com', 'fiscal@example.com']
        pagina = client.get('/b2b/clientes').get_data(as_text=True)
        assert pagina.count('name="emails_cobranca"') == 2
        assert 'Compras@example.com\nfiscal@example.com' in pagina
        client.post(url, data={'nome': cli.nome, 'email': cli.email, 'emails_cobranca': '', 'ativo': '1'})
        assert cli.emails_cobranca == []
        enviar.assert_not_called()
    assert EnvioCobranca.query.count() == 0


@pytest.mark.parametrize('email,adicionais', [
    ('principal@example.com', 'valido@example.com;invalido'),
    ('', 'adicional@example.com'),
    ('x' * 120 + '@example.com', 'adicional@example.com'),
    ('principal@example.com', ','.join(f'x{i}@example.com' for i in range(11))),
])
def test_cadastro_invalido_nao_salva_parcialmente(app, admin_user, email, adicionais):
    cli, _, _, _ = _cenario()
    cli.emails_cobranca = ['original@example.com']
    db.session.commit()
    client = _client(app, admin_user)
    dados = {'nome': 'Nome alterado', 'email': email, 'emails_cobranca': adicionais}
    client.post(f'/b2b/clientes/{cli.id}/editar', data=dados)
    assert cli.nome == 'Restaurante Bom Prato'
    assert cli.email == 'compras@bomprato.com.br'
    assert cli.emails_cobranca == ['original@example.com']
    client.post('/b2b/clientes/novo', data=dados)
    assert ClienteB2B.query.count() == 1


@pytest.mark.parametrize('documentos', ['fatura', 'parcela'], indirect=True)
def test_envio_manual_payload_snapshot_e_reenvio_explicito(app, admin_user, documentos, provedor):
    doc, cob, url, _ = documentos
    client = _client(app, admin_user)
    dados = _form(client, url, cc='intruso@example.com', bcc='intruso@example.com')
    response = client.post(url, data=dados, follow_redirects=True)
    payload = provedor[1].call_args.kwargs['json']
    assert payload['To'] == doc.cliente.email
    assert payload['Cc'] == 'compras@example.com,fiscal@example.com'
    assert payload['Bcc'] == 'caio@opao.online,dakson@opao.online,contato@opao.online'
    assert len(payload['Attachments']) == 2
    e = EnvioCobranca.query.one()
    assert e.copias == doc.cliente.emails_cobranca
    assert e.cobranca_ids == [cob.id] and e.status == 'aceito'
    assert 'Em cópia (CC): compras@example.com, fiscal@example.com' in response.get_data(as_text=True)
    doc.cliente.emails_cobranca = ['novo@example.com']
    db.session.commit()
    # Repetir uma solicitação já concluída não a transforma num reenvio.
    client.post(url, data=dados)
    provedor[1].assert_called_once()
    assert e.copias == ['compras@example.com', 'fiscal@example.com']
    client.post(url, data=_form(client, url))
    assert provedor[1].call_count == 2
    assert provedor[1].call_args.kwargs['json']['Cc'] == 'novo@example.com'
    assert EnvioCobranca.query.count() == 2


@pytest.mark.parametrize('documentos', ['fatura'], indirect=True)
def test_principal_editado_nao_duplica_cc_ou_bcc(app, admin_user, documentos, provedor):
    doc, _, url, _ = documentos
    doc.cliente.emails_cobranca = ['COMPRAS@example.com', 'CAIO@opao.online', 'fiscal@example.com']
    db.session.commit()
    client = _client(app, admin_user)
    client.post(url, data=_form(client, url, email='compras@example.com'))
    payload = provedor[1].call_args.kwargs['json']
    assert payload['To'] == 'compras@example.com'
    assert payload['Cc'] == 'CAIO@opao.online,fiscal@example.com'
    assert payload['Bcc'] == 'dakson@opao.online,contato@opao.online'
    e = EnvioCobranca.query.one()
    assert e.copias == ['CAIO@opao.online', 'fiscal@example.com']
    assert e.copias_ocultas == ['dakson@opao.online', 'contato@opao.online']


@pytest.mark.parametrize('documentos', ['fatura'], indirect=True)
@pytest.mark.parametrize('versao', ['ausente', 'adulterada', 'desatualizada', 'outro_documento'])
def test_destinatarios_nao_conferidos_bloqueiam_sem_efeitos(app, admin_user, documentos, provedor, versao):
    doc, _, url, _ = documentos
    client = _client(app, admin_user)
    dados = _form(client, url)
    if versao == 'ausente':
        dados.pop('versao_destinatarios')
    elif versao == 'adulterada':
        dados['versao_destinatarios'] += 'alterado'
    elif versao == 'outro_documento':
        dados['versao_destinatarios'] = versao_copias(SimpleNamespace(
            __tablename__='venda_b2b', id=doc.id, cliente=doc.cliente))
    else:
        doc.cliente.emails_cobranca = ['outro@example.com']
        db.session.commit()
    response = client.post(url, data=dados, follow_redirects=True)
    assert 'Reabra a tela e confirme os destinatários' in response.get_data(as_text=True)
    for mock in provedor:
        mock.assert_not_called()
    assert EnvioCobranca.query.count() == 0


@pytest.mark.parametrize('documentos', ['fatura'], indirect=True)
def test_mudanca_durante_download_nao_muda_destinatarios_reservados(app, admin_user, documentos, provedor):
    doc, _, url, _ = documentos
    client = _client(app, admin_user)
    dados = _form(client, url)

    def baixar(_):
        assert EnvioCobranca.query.one().copias == ['compras@example.com', 'fiscal@example.com']
        doc.cliente.emails_cobranca = ['alterado@example.com']
        db.session.commit()
        return b'%PDF-nf', None

    provedor[0].side_effect = baixar
    client.post(url, data=dados)
    assert provedor[1].call_args.kwargs['json']['Cc'] == 'compras@example.com,fiscal@example.com'
    assert EnvioCobranca.query.one().copias == ['compras@example.com', 'fiscal@example.com']


@pytest.mark.parametrize('documentos', ['fatura', 'parcela'], indirect=True)
def test_automatico_inclui_copias_sem_reenviar_ao_editar_cliente(app, admin_user, documentos, provedor):
    doc, _, _, r = documentos
    e, novo = enviar_automatico(r, str(uuid4()), admin_user)
    assert novo and e.status == 'aceito'
    assert provedor[1].call_args.kwargs['json']['Cc'] == 'compras@example.com,fiscal@example.com'
    doc.cliente.emails_cobranca = ['novo@example.com']
    db.session.commit()
    anterior, novo = enviar_automatico(r, str(uuid4()), admin_user)
    assert anterior.id == e.id and not novo
    provedor[1].assert_called_once()
    assert e.copias == ['compras@example.com', 'fiscal@example.com']


def test_automacao_barra_copia_invalida_antes_de_emitir(app, owner_user):
    venda = _avulsa()
    venda.cliente.emails_cobranca = ['invalido']
    job = _fila(venda, owner_user)
    with patch('app.services.tiny.incluir_nota_fiscal') as nf, \
            patch('app.services.tiny.emitir_nota_fiscal') as emitir, \
            patch('app.services.email.enviar') as enviar:
        svc.executar()
    for mock in (nf, emitir, enviar):
        mock.assert_not_called()
    assert job.estado == 'erro' and 'adicional inválido' in job.erro
    assert EnvioCobranca.query.count() == 0


@pytest.mark.parametrize('documentos', ['fatura'], indirect=True)
def test_configuracao_invalida_nao_quebra_tela_nem_confirma_banco(app, admin_user, documentos, provedor):
    doc, cob, url, _ = documentos
    doc.cliente.emails_cobranca = ['invalido']
    cob.status, cob.remessa_id = 'remessa', 999
    db.session.commit()
    client = _client(app, admin_user)
    pagina = client.get(url)
    assert pagina.status_code == 200
    assert 'E-mail adicional inválido' in pagina.get_data(as_text=True)
    with patch('app.services.cobrancas_automacao.confirmar_titulo') as confirmar:
        client.post(url, data={'chave': str(uuid4()), 'email': doc.cliente.email, 'banco_confirmado': '1'})
    confirmar.assert_not_called()
    for mock in provedor:
        mock.assert_not_called()
    assert EnvioCobranca.query.count() == 0
