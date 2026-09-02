"""Envio de boleto e NF-e do B2B por e-mail (06/07/2026).

Boleto: PDF gerado localmente (sicredi_boleto) + linha digitável + Pix
copia-e-cola quando o retorno já trouxe. NF: DANFE baixado do Tiny na hora
(link temporário → PDF ANEXADO). Postmark mockado no `requests.post`.
"""
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from app.extensions import db
from app.models import ClienteB2B, Cobranca, VendaB2B, VendaB2BParcela
from app.utils import hoje


def _login(client, uid):
    with client.session_transaction() as s:
        s['_user_id'] = str(uid)
        s['_fresh'] = True


def _cenario(email='compras@bomprato.com.br', nosso_numero='252000041'):
    cli = ClienteB2B(nome='Restaurante Bom Prato',
                     cnpj_cpf='11222333000144', email=email, ativo=True)
    db.session.add(cli)
    db.session.flush()
    v = VendaB2B(cliente_id=cli.id, valor_total=Decimal('500.00'))
    db.session.add(v)
    db.session.flush()
    p = VendaB2BParcela(venda_id=v.id, numero=1,
                        vencimento=hoje() + timedelta(days=15),
                        valor=Decimal('500.00'))
    db.session.add(p)
    db.session.flush()
    cob = Cobranca(parcela_id=p.id, pagador_nome=cli.nome,
                   pagador_cnpj_cpf='11.222.333/0001-44',
                   pagador_endereco='Rua das Laranjeiras 100',
                   pagador_cep='04568001', valor=Decimal('500.00'),
                   vencimento=hoje() + timedelta(days=15), emissao=hoje(),
                   seu_numero='V1P1', nosso_numero=nosso_numero,
                   status='registrada')
    db.session.add(cob)
    db.session.commit()
    return cli, v, p, cob


def test_enviar_anexos_vira_attachments_base64(app):
    """`email.enviar(anexos=...)` monta o campo Attachments do Postmark."""
    import base64

    from app.services import email as email_svc
    with app.app_context():
        app.config['POSTMARK_SERVER_TOKEN'] = 'tok'

        class _Resp:
            status_code = 200

            def json(self):
                return {'ErrorCode': 0, 'MessageID': 'abc'}

        with patch('app.services.email.requests.post',
                   return_value=_Resp()) as post:
            res = email_svc.enviar('x@y.com', 'Oi', '<b>oi</b>',
                                   anexos=[('doc.pdf', b'%PDF-fake',
                                            'application/pdf')])
        assert res['ok']
        payload = post.call_args.kwargs['json']
        anexo = payload['Attachments'][0]
        assert anexo['Name'] == 'doc.pdf'
        assert anexo['ContentType'] == 'application/pdf'
        assert base64.b64decode(anexo['Content']) == b'%PDF-fake'


def _preparar_nf(venda):
    from app.utils import agora
    venda.tiny_nota_fiscal_id = 'nf-9'
    venda.nf_numero = '11629'
    venda.nf_emitida_em = agora()
    db.session.commit()


def _post_conjunto(client, parcela, **dados):
    from uuid import uuid4
    return client.post(f'/cobrancas/parcela/{parcela.id}/documentos', data={
        'email': 'compras@bomprato.com.br', 'chave': str(uuid4()), **dados},
        follow_redirects=True)


def test_formularios_antigos_apenas_abrem_confirmacao(app, admin_user):
    from app.models import EnvioCobranca
    _, venda, parcela, cob = _cenario()
    _preparar_nf(venda)
    client = app.test_client()
    _login(client, admin_user.id)
    with patch('app.services.email.enviar') as enviar, \
            patch('app.services.tiny_nf.baixar_danfe_pdf_com_motivo') as baixar:
        for url in (f'/cobrancas/{cob.id}/enviar-email',
                    f'/b2b/vendas/{venda.id}/enviar-nf-email',
                    f'/b2b/vendas/{venda.id}/enviar-nf-boleto-email'):
            response = client.post(url, data={'email': 'outro@cliente.com'})
            assert response.status_code == 303
            response = client.get(response.location, follow_redirects=True)
            assert response.status_code == 200
            assert 'id="cob-send-form"' in response.get_data(as_text=True)
    enviar.assert_not_called()
    baixar.assert_not_called()
    assert EnvioCobranca.query.count() == 0


def test_envio_conjunto_anexa_nf_e_boleto_real_com_pix(app, admin_user):
    _, venda, parcela, cob = _cenario()
    _preparar_nf(venda)
    cob.pix_copia_cola = '000201PIXexemplo'
    db.session.commit()
    client = app.test_client()
    _login(client, admin_user.id)
    with patch('app.services.tiny_nf.baixar_danfe_pdf_com_motivo',
               return_value=(b'%PDF-danfe', None)) as baixar, \
            patch('app.services.email.enviar', return_value={'ok': True, 'id': 'm'}) as enviar:
        response = _post_conjunto(client, parcela)
    assert response.status_code == 200
    baixar.assert_called_once_with('nf-9')
    enviar.assert_called_once()
    args, kwargs = enviar.call_args
    assert args[0] == 'compras@bomprato.com.br'
    assert '11629' in args[1]
    assert kwargs['anexos'][0] == ('nfe_11629.pdf', b'%PDF-danfe', 'application/pdf')
    nome, conteudo, ctype = kwargs['anexos'][1]
    assert nome == f'boleto_{cob.nosso_numero}.pdf'
    assert bytes(conteudo).startswith(b'%PDF') and ctype == 'application/pdf'
    assert '000201PIXexemplo' in kwargs['texto']


def test_conjunto_requer_email_valido_e_aceita_destino_conferido(app, admin_user):
    _, venda, parcela, _ = _cenario(email=None)
    _preparar_nf(venda)
    client = app.test_client()
    _login(client, admin_user.id)
    with patch('app.services.tiny_nf.baixar_danfe_pdf_com_motivo',
               return_value=(b'%PDF-danfe', None)), \
            patch('app.services.email.enviar', return_value={'ok': True, 'id': 'm'}) as enviar:
        corpo = _post_conjunto(client, parcela, email='').get_data(as_text=True)
        assert 'e-mail válido' in corpo
        enviar.assert_not_called()
        _post_conjunto(client, parcela, email='outro@cliente.com')
    assert enviar.call_args.args[0] == 'outro@cliente.com'


def test_conjunto_sem_nf_nao_baixa_nem_envia(app, admin_user):
    _, _, parcela, _ = _cenario()
    client = app.test_client()
    _login(client, admin_user.id)
    with patch('app.services.email.enviar') as enviar, \
            patch('app.services.tiny_nf.baixar_danfe_pdf_com_motivo') as baixar:
        corpo = _post_conjunto(client, parcela).get_data(as_text=True)
    assert 'autorização da NF' in corpo
    enviar.assert_not_called()
    baixar.assert_not_called()


def test_conjunto_danfe_indisponivel_mostra_causa_sem_enviar(app, admin_user):
    _, venda, parcela, _ = _cenario()
    _preparar_nf(venda)
    client = app.test_client()
    _login(client, admin_user.id)
    with patch('app.services.tiny_nf.baixar_danfe_pdf_com_motivo',
               return_value=(None, 'Tiny fora do ar (HTTP 503)')), \
            patch('app.services.email.enviar') as enviar:
        corpo = _post_conjunto(client, parcela).get_data(as_text=True)
    assert 'Tiny fora do ar' in corpo and 'Nada foi enviado' in corpo
    enviar.assert_not_called()


def test_conjunto_sem_boleto_nao_envia_pela_metade(app, admin_user):
    _, venda, parcela, _ = _cenario(nosso_numero=None)
    _preparar_nf(venda)
    client = app.test_client()
    _login(client, admin_user.id)
    with patch('app.services.email.enviar') as enviar:
        corpo = _post_conjunto(client, parcela).get_data(as_text=True)
    assert 'Prepare o boleto' in corpo
    enviar.assert_not_called()


def test_venda_tem_apenas_atalho_conjunto_e_historico(app, admin_user):
    _, venda, _, _ = _cenario()
    _preparar_nf(venda)
    client = app.test_client()
    _login(client, admin_user.id)
    for layout in (True, False):
        app.config['UI_V2_ENABLED'] = layout
        corpo = client.get(f'/b2b/vendas/{venda.id}').get_data(as_text=True)
        assert f'/b2b/vendas/{venda.id}/documentos' in corpo
        assert 'NF + boleto / Histórico' in corpo
        assert 'enviar-nf-email' not in corpo
        assert 'enviar-nf-boleto-email' not in corpo
        assert '/enviar-email' not in corpo


def test_venda_multi_parcela_escolhe_boleto_sem_misturar_historicos(app, admin_user):
    from app.services.central_cobrancas import carregar, historico, painel
    _, venda, p1, cob = _cenario()
    _preparar_nf(venda)
    p2 = VendaB2BParcela(venda_id=venda.id, numero=2, valor=Decimal('100'),
                        vencimento=p1.vencimento + timedelta(days=30))
    db.session.add(p2)
    db.session.commit()
    client = app.test_client()
    _login(client, admin_user.id)
    with patch('app.services.email.enviar') as enviar:
        response = client.get(f'/b2b/vendas/{venda.id}/documentos')
        assert response.location.endswith(f'/b2b/vendas/{venda.id}#boletos')
        enviar.assert_not_called()
    with patch('app.services.tiny_nf.baixar_danfe_pdf_com_motivo',
               return_value=(b'%PDF-danfe', None)), \
            patch('app.services.email.enviar', return_value={'ok': True, 'id': 'm'}) as enviar:
        _post_conjunto(client, p1)
    assert len(enviar.call_args.kwargs['anexos']) == 2
    assert len(historico(carregar('parcela', p1.id))) == 1
    assert not historico(carregar('parcela', p2.id))
    por_id = {r.id: r for r in painel()}
    assert por_id[p1.id].envio_confirmado
    assert por_id[p2.id].envio_confirmado is None
