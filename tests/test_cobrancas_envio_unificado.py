"""NF + boleto: cópia oculta, último sucesso e reenvio deliberado, sem rede real."""
from datetime import datetime
from importlib import import_module
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from app.extensions import db
from app.models import EnvioCobranca, Usuario
from app.services import email as email_svc
from app.services.central_cobrancas import painel
from tests.test_central_cobrancas import _client, _mensal, _post

COPIAS = ['caio@opao.online', 'dakson@opao.online', 'contato@opao.online']


def _registro(fatura, cob, **campos):
    dados = dict(chave=str(uuid4()), fatura_id=fatura.id, cobranca_ids=[cob.id],
                 referencia=fatura.codigo, destinatario='financeiro@cliente.com.br',
                 documentos='nf_boleto', nf_id=fatura.tiny_nota_fiscal_id,
                 status='aceito', provedor_id='anterior', anexos=['nf.pdf', 'boleto.pdf'],
                 criado_em=datetime(2026, 9, 1, 9, 29),
                 concluido_em=datetime(2026, 9, 1, 9, 30))
    dados.update(campos)
    e = EnvioCobranca(**dados)
    db.session.add(e)
    db.session.commit()
    return e


def test_payload_conjunto_leva_bcc_privado_e_historico_exato(app, admin_user):
    f, _, c = _mensal()
    app.config['POSTMARK_SERVER_TOKEN'] = 'test-token-falso'
    resposta = SimpleNamespace(status_code=200, json=lambda: {'ErrorCode': 0, 'MessageID': 'm-confirmado'})
    with patch('app.services.tiny_nf.baixar_danfe_pdf_com_motivo', return_value=(b'%PDF-nf', None)), \
            patch('app.services.email.requests.post', return_value=resposta) as post:
        _post(_client(app, admin_user), f, bcc='intruso@example.com', copias_ocultas='intruso@example.com')
    post.assert_called_once()
    payload = post.call_args.kwargs['json']
    assert payload['To'] == 'financeiro@cliente.com.br'
    assert payload['Bcc'] == ','.join(COPIAS)
    assert 'Cc' not in payload
    assert len(payload['Attachments']) == 2
    for email in COPIAS:
        assert email not in payload['HtmlBody']
        assert email not in payload['TextBody']
    e = EnvioCobranca.query.one()
    assert e.copias_ocultas == COPIAS
    assert e.status == 'aceito' and e.provedor_id == 'm-confirmado'
    assert e.concluido_em >= e.criado_em
    assert e.cobranca_ids == [c.id] and e.documentos == 'nf_boleto'


def test_outros_emails_transacionais_nao_recebem_copias(app):
    app.config['POSTMARK_SERVER_TOKEN'] = 'test-token-falso'
    resposta = SimpleNamespace(status_code=200, json=lambda: {'ErrorCode': 0, 'MessageID': 'outro'})
    with patch('app.services.email.requests.post', return_value=resposta) as post:
        email_svc.enviar('usuario@example.com', 'Convite', '<p>Bem-vindo</p>')
    assert 'Bcc' not in post.call_args.kwargs['json']
    assert 'Cc' not in post.call_args.kwargs['json']


def test_nao_duplica_copia_ao_proprio_destinatario(app, admin_user):
    f, _, _ = _mensal()
    with patch('app.services.tiny_nf.baixar_danfe_pdf_com_motivo', return_value=(b'%PDF-nf', None)), \
            patch('app.services.email.enviar', return_value={'ok': True, 'id': 'm'}) as enviar:
        _post(_client(app, admin_user), f, email='CAIO@opao.online')
    assert enviar.call_args.kwargs['bcc'] == COPIAS[1:]
    assert EnvioCobranca.query.one().copias_ocultas == COPIAS[1:]


def test_ultimo_envio_visivel_e_reenvio_cria_registro_sem_apagar_anterior(app, admin_user):
    f, _, c = _mensal()
    antigo = _registro(f, c)
    client = _client(app, admin_user)
    url = f'/cobrancas/fatura/{f.id}/documentos'
    corpo = client.get(url).get_data(as_text=True)
    assert 'NF + boleto enviados em 01/09/2026 às 09:30' in corpo
    assert 'Enviar novamente' in corpo
    assert 'Cópias ocultas não registradas neste envio antigo' in corpo
    assert '01/09/2026 às 09:30' in client.get('/cobrancas/').get_data(as_text=True)
    chave = str(uuid4())
    with patch('app.services.tiny_nf.baixar_danfe_pdf_com_motivo', return_value=(b'%PDF-nf', None)), \
            patch('app.services.email.enviar', return_value={'ok': True, 'id': 'novo'}) as enviar:
        _post(client, f, chave=chave)
        _post(client, f, chave=chave)
    enviar.assert_called_once()
    assert EnvioCobranca.query.count() == 2
    assert antigo.concluido_em == datetime(2026, 9, 1, 9, 30)
    assert antigo.provedor_id == 'anterior' and antigo.copias_ocultas is None
    assert painel()[0].envio_confirmado.provedor_id == 'novo'


@pytest.mark.parametrize('resultado', [{'ok': False, 'erro': 'recusado'}, {'ok': True},
                                     {'ok': False, 'incerto': True}])
def test_tentativa_posterior_falha_nao_apaga_envio_confirmado(app, admin_user, resultado):
    f, _, c = _mensal()
    antigo = _registro(f, c)
    client = _client(app, admin_user)
    with patch('app.services.tiny_nf.baixar_danfe_pdf_com_motivo', return_value=(b'%PDF-nf', None)), \
            patch('app.services.email.enviar', return_value=resultado):
        corpo = _post(client, f).get_data(as_text=True)
    assert 'NF + boleto enviados em 01/09/2026 às 09:30' in corpo
    assert 'Última tentativa:' in corpo and 'Enviar novamente' in corpo
    assert painel()[0].envio_confirmado.id == antigo.id
    assert f.codigo in client.get('/cobrancas/?envio=aceito').get_data(as_text=True)
    assert f.codigo in client.get('/cobrancas/?envio=problema').get_data(as_text=True)


@pytest.mark.parametrize('diferenca', [{'documentos': 'nf'}, {'documentos': 'boleto'},
                                      {'nf_id': 'outra-nf'}, {'cobranca_ids': [999999]},
                                      {'status': 'falha'}, {'status': 'incerto'}])
def test_outro_documento_ou_falha_nao_confirma_conjunto(app, admin_user, diferenca):
    f, _, c = _mensal()
    _registro(f, c, **diferenca)
    client = _client(app, admin_user)
    corpo = client.get(f'/cobrancas/fatura/{f.id}/documentos').get_data(as_text=True)
    assert 'NF + boleto enviados em' not in corpo
    assert 'nenhum envio confirmado deste conjunto' in corpo
    assert painel()[0].envio_confirmado is None
    assert f.codigo not in client.get('/cobrancas/?envio=aceito').get_data(as_text=True)


def test_fatura_e_banco_sem_opcao_de_envio_individual(app, admin_user):
    f, _, c = _mensal()
    client = _client(app, admin_user)
    for layout in (True, False):
        app.config['UI_V2_ENABLED'] = layout
        for url in (f'/b2b/faturas/{f.id}', '/cobrancas/banco'):
            corpo = client.get(url).get_data(as_text=True)
            assert 'NF + boleto / Histórico' in corpo
            assert '/enviar-email' not in corpo
            assert '/enviar-nf-email' not in corpo
    with patch('app.services.email.enviar') as enviar:
        r = client.post(f'/b2b/faturas/{f.id}/enviar-nf-email')
    assert r.status_code == 303
    assert r.location.endswith(f'/cobrancas/fatura/{f.id}/documentos')
    enviar.assert_not_called()
    assert EnvioCobranca.query.count() == 0


@pytest.mark.parametrize('papel', ['treinamento', 'loja', 'producao'])
def test_rotas_legadas_e_atalho_mantem_autorizacao(app, papel):
    f, p, c = _mensal()
    usuario = Usuario(nome='Restrito', login='restrito', papel=papel)
    usuario.set_senha('senha-teste')
    db.session.add(usuario)
    db.session.commit()
    client = _client(app, usuario)
    with patch('app.services.email.enviar') as enviar:
        assert client.get(f'/b2b/vendas/{p.venda_id}/documentos').status_code == 403
        assert client.post(f'/b2b/faturas/{f.id}/enviar-nf-email').status_code == 403
        assert client.post(f'/cobrancas/{c.id}/enviar-email').status_code == 403
    enviar.assert_not_called()


@pytest.mark.parametrize('nf,boletos', [(None, [{'pdf': b'%PDF-b'}]), (b'%PDF-nf', []),
                                     (b'%PDF-nf', [{'pdf': b'erro'}])])
def test_helper_conjunto_nao_aceita_documentos_incompletos(app, nf, boletos):
    f, _, _ = _mensal()
    with patch('app.services.email.enviar') as enviar:
        resultado = email_svc.enviar_nf_e_boleto_b2b(f, 'x@y.com', nf, boletos)
    assert not resultado['ok']
    enviar.assert_not_called()


def test_migracao_copias_idempotente_preserva_historico_existente():
    antiga = import_module('migrations.versions.6d9e3c7a2f10_central_cobrancas_historico')
    nova = import_module('migrations.versions.91b6a7d3c820_copias_ocultas_cobrancas')
    engine = sa.create_engine('sqlite://')
    with engine.begin() as conn:
        ops = Operations(MigrationContext.configure(conn))
        with patch.object(antiga, 'op', ops):
            antiga.upgrade()
        conn.execute(sa.text("""INSERT INTO envio_cobranca
            (chave, cobranca_ids, referencia, destinatario, documentos, anexos, status, criado_em)
            VALUES ('antigo', '[]', 'FAT00001', 'x@y.com', 'nf', '[]', 'aceito', CURRENT_TIMESTAMP)"""))
        with patch.object(nova, 'op', ops):
            nova.upgrade()
            nova.upgrade()
        row = conn.execute(sa.text('SELECT chave, documentos, copias_ocultas FROM envio_cobranca')).one()
        assert tuple(row) == ('antigo', 'nf', None)
        assert len(sa.inspect(conn).get_indexes('envio_cobranca')) == 3
    engine.dispose()
