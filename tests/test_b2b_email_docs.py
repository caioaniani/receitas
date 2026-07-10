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


def test_rota_enviar_boleto_email_anexa_pdf(app, admin_user):
    """POST /cobrancas/<id>/enviar-email: PDF real do boleto anexado,
    destino = e-mail do cliente B2B da parcela."""
    with app.app_context():
        cli, v, p, cob = _cenario()
        cob.pix_copia_cola = '000201PIXexemplo'
        db.session.commit()
        cid, nosso = cob.id, cob.nosso_numero
    c = app.test_client()
    _login(c, admin_user.id)
    with patch('app.services.email.enviar',
               return_value={'ok': True, 'id': 'msg1'}) as env:
        r = c.post(f'/cobrancas/{cid}/enviar-email', follow_redirects=True)
    assert r.status_code == 200
    env.assert_called_once()
    args, kwargs = env.call_args
    assert args[0] == 'compras@bomprato.com.br'
    assert 'Boleto' in args[1]
    nome, conteudo, ctype = kwargs['anexos'][0]
    assert nome == f'boleto_{nosso}.pdf'
    assert bytes(conteudo).startswith(b'%PDF')      # PDF real do fpdf
    assert ctype == 'application/pdf'
    assert '000201PIXexemplo' in kwargs['texto']    # Pix copia-e-cola no corpo


def test_rota_boleto_email_sem_email_do_cliente_avisa(app, admin_user):
    with app.app_context():
        _, _, _, cob = _cenario(email=None)
        cid = cob.id
    c = app.test_client()
    _login(c, admin_user.id)
    with patch('app.services.email.enviar') as env:
        r = c.post(f'/cobrancas/{cid}/enviar-email', follow_redirects=True)
    assert r.status_code == 200
    assert 'sem e-mail' in r.get_data(as_text=True)
    env.assert_not_called()


def test_rota_boleto_email_aceita_email_avulso_do_form(app, admin_user):
    with app.app_context():
        _, _, _, cob = _cenario(email=None)
        cid = cob.id
    c = app.test_client()
    _login(c, admin_user.id)
    with patch('app.services.email.enviar',
               return_value={'ok': True}) as env:
        c.post(f'/cobrancas/{cid}/enviar-email',
               data={'email': 'outro@cliente.com'}, follow_redirects=True)
    assert env.call_args[0][0] == 'outro@cliente.com'


def test_rota_enviar_nf_email_baixa_danfe_e_anexa(app, admin_user):
    """POST /b2b/vendas/<id>/enviar-nf-email: baixa o DANFE do Tiny (link
    expira — vai anexado) e manda pro e-mail do cliente."""
    from app.utils import agora
    with app.app_context():
        cli, v, _, _ = _cenario()
        v.tiny_nota_fiscal_id = 'nf-9'
        v.nf_numero = '11500'
        v.nf_emitida_em = agora()
        db.session.commit()
        vid = v.id
    c = app.test_client()
    _login(c, admin_user.id)
    with patch('app.services.tiny_nf.baixar_danfe_pdf_com_motivo',
               return_value=(b'%PDF-danfe', None)) as baixar, \
         patch('app.services.email.enviar',
               return_value={'ok': True, 'id': 'msg2'}) as env:
        r = c.post(f'/b2b/vendas/{vid}/enviar-nf-email',
                   follow_redirects=True)
    assert r.status_code == 200
    baixar.assert_called_once_with('nf-9')
    args, kwargs = env.call_args
    assert args[0] == 'compras@bomprato.com.br'
    assert '11500' in args[1]                       # numero da NF no assunto
    assert kwargs['anexos'][0] == ('nfe_11500.pdf', b'%PDF-danfe',
                                   'application/pdf')


def test_rota_nf_email_sem_nf_emitida_avisa(app, admin_user):
    with app.app_context():
        _, v, _, _ = _cenario()
        vid = v.id
    c = app.test_client()
    _login(c, admin_user.id)
    with patch('app.services.email.enviar') as env:
        r = c.post(f'/b2b/vendas/{vid}/enviar-nf-email',
                   follow_redirects=True)
    assert 'não foi emitida' in r.get_data(as_text=True)
    env.assert_not_called()


def test_rota_nf_email_danfe_indisponivel_nao_envia(app, admin_user):
    from app.utils import agora
    with app.app_context():
        _, v, _, _ = _cenario()
        v.tiny_nota_fiscal_id = 'nf-9'
        v.nf_emitida_em = agora()
        db.session.commit()
        vid = v.id
    c = app.test_client()
    _login(c, admin_user.id)
    with patch('app.services.tiny_nf.baixar_danfe_pdf_com_motivo',
               return_value=(None, 'nota em processamento')), \
         patch('app.services.email.enviar') as env:
        r = c.post(f'/b2b/vendas/{vid}/enviar-nf-email',
                   follow_redirects=True)
    assert 'DANFE' in r.get_data(as_text=True)
    env.assert_not_called()
