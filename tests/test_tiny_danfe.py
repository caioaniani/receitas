"""DANFE do Tiny: extração do link e propagação da causa REAL da falha
(10/07/2026). Bug do dono: NF autorizada mas 'Ver DANFE'/'Enviar NF por
e-mail' falhava com o genérico "a NF precisa estar autorizada", escondendo
o motivo real. Agora o extrator também lê `link_danfe` e a tela mostra a
causa exata. Tiny SEMPRE mockado.
"""
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from app.extensions import db
from app.models import ClienteB2B, VendaB2B, VendaB2BParcela
from app.services import tiny, tiny_nf
from app.utils import agora, hoje


def _login(client, uid):
    with client.session_transaction() as s:
        s['_user_id'] = str(uid)
        s['_fresh'] = True


def _ok(**extra):
    return {'status': 'OK', **extra}


def test_extrai_prefere_link_danfe():
    """Quando o Tiny manda `link_danfe` (PDF) e `link_nfe` (consulta HTML),
    pega o DANFE."""
    r = _ok(link_danfe='https://tiny/danfe.pdf',
            link_nfe='https://consulta.sefaz/html')
    assert tiny._extrair_link_danfe(r) == 'https://tiny/danfe.pdf'


def test_extrai_cai_pro_link_nfe_quando_so_ele():
    assert tiny._extrair_link_danfe(_ok(link_nfe='https://x/y.pdf')) \
        == 'https://x/y.pdf'


def test_link_com_motivo_sucesso(app):
    with app.app_context():
        app.config['TINY_API_TOKEN'] = 'tok'
        with patch('app.services.tiny._get',
                   return_value=_ok(link_danfe='https://tiny/d.pdf')):
            link, motivo = tiny.obter_link_nota_fiscal_com_motivo('909')
    assert link == 'https://tiny/d.pdf' and motivo is None


def test_link_com_motivo_erro_do_tiny_propaga(app):
    """Tiny devolve status de erro com mensagem — a mensagem REAL sobe (não
    o genérico)."""
    with app.app_context():
        app.config['TINY_API_TOKEN'] = 'tok'
        erro_retorno = {'status': 'Erro',
                        'erros': [{'erro': 'Nota fiscal ainda em processamento'}]}
        with patch('app.services.tiny._get', return_value=erro_retorno):
            link, motivo = tiny.obter_link_nota_fiscal_com_motivo('909')
    assert link is None
    assert 'processamento' in motivo


def test_link_com_motivo_link_vazio(app):
    with app.app_context():
        app.config['TINY_API_TOKEN'] = 'tok'
        with patch('app.services.tiny._get', return_value=_ok()):
            link, motivo = tiny.obter_link_nota_fiscal_com_motivo('909')
    assert link is None and 'devolveu o link' in motivo


def test_link_com_motivo_http_timeout_propaga_causa(app):
    """Quando o `_get` esgota (timeout/HTTP 5xx) e devolve None, a causa
    real vem do thread-local — é o caminho que motivou a mudança."""
    with app.app_context():
        app.config['TINY_API_TOKEN'] = 'tok'

        def _falha(*a, **k):
            tiny._registrar_falha('timeout (12s) (apos 3 tentativas)')
            return None
        with patch('app.services.tiny._get', side_effect=_falha):
            link, motivo = tiny.obter_link_nota_fiscal_com_motivo('909')
    assert link is None and 'timeout' in motivo


def test_link_com_motivo_sem_token(app):
    with app.app_context():
        app.config['TINY_API_TOKEN'] = ''
        link, motivo = tiny.obter_link_nota_fiscal_com_motivo('909')
    assert link is None and 'TINY_API_TOKEN' in motivo


def test_baixar_danfe_nao_pdf_da_motivo(app):
    """O link resolve mas o download vem HTML (página de erro/expiração) —
    o motivo diz que não veio PDF, em vez de sumir."""
    class _Resp:
        status_code = 200
        headers = {'Content-Type': 'text/html'}
        content = b'<html>erro</html>'
    with app.app_context():
        app.config['TINY_API_TOKEN'] = 'tok'
        with patch('app.services.tiny.obter_link_nota_fiscal_com_motivo',
                   return_value=('https://tiny/x', None)), \
             patch('requests.get', return_value=_Resp()):
            pdf, motivo = tiny_nf.baixar_danfe_pdf_com_motivo('909')
    assert pdf is None and 'PDF' in motivo


def _venda_com_nf(email='fin@united.com'):
    cli = ClienteB2B(nome='United Coffee', email=email, ativo=True,
                     cnpj_cpf='44737537000104')
    db.session.add(cli)
    db.session.flush()
    v = VendaB2B(cliente_id=cli.id, valor_total=Decimal('180.00'),
                 tiny_nota_fiscal_id='909358497', nf_numero='011629',
                 nf_emitida_em=agora())
    db.session.add(v)
    db.session.flush()
    db.session.add(VendaB2BParcela(venda_id=v.id, numero=1,
                                   vencimento=hoje() + timedelta(days=11),
                                   valor=Decimal('180.00')))
    db.session.commit()
    return v


def test_rota_email_mostra_motivo_real(app, admin_user):
    """POST enviar-nf-email com DANFE indisponível mostra a CAUSA real
    (não o 'precisa estar autorizada')."""
    with app.app_context():
        v = _venda_com_nf()
        vid = v.id
    c = app.test_client()
    _login(c, admin_user.id)
    with patch('app.services.tiny_nf.baixar_danfe_pdf_com_motivo',
               return_value=(None, 'Tiny fora do ar (HTTP 503)')):
        r = c.post(f'/b2b/vendas/{vid}/enviar-nf-email',
                   follow_redirects=True)
    corpo = r.get_data(as_text=True)
    assert 'Tiny fora do ar' in corpo
    assert 'precisa estar autorizada' not in corpo


def test_rota_ver_danfe_mostra_motivo_real(app, admin_user):
    with app.app_context():
        v = _venda_com_nf()
        vid = v.id
    c = app.test_client()
    _login(c, admin_user.id)
    with patch('app.services.tiny.obter_link_nota_fiscal_com_motivo',
               return_value=(None, 'nota em processamento no Tiny')):
        r = c.get(f'/b2b/vendas/{vid}/danfe', follow_redirects=True)
    assert 'processamento no Tiny' in r.get_data(as_text=True)


def test_debug_tiny_nota_owner(app, owner_user):
    """A rota de diagnóstico mostra os campos de link crus e o motivo."""
    with app.app_context():
        app.config['TINY_API_TOKEN'] = 'tok'
    c = app.test_client()
    _login(c, owner_user.id)
    with patch('app.services.tiny._get',
               return_value=_ok(link_danfe='https://tiny/d.pdf')), \
         patch('app.services.tiny_nf.baixar_danfe_pdf_com_motivo',
               return_value=(b'%PDF', None)):
        r = c.get('/admin/debug-tiny-nota?id=909358497')
    j = r.get_json()
    assert j['nota_id'] == '909358497'
    assert j['campos_link']['link_danfe'] == 'https://tiny/d.pdf'
    assert j['link_resolvido'] == 'https://tiny/d.pdf'
    assert j['pdf_ok'] is True


def test_debug_tiny_nota_sem_id(app, owner_user):
    c = app.test_client()
    _login(c, owner_user.id)
    r = c.get('/admin/debug-tiny-nota')
    assert r.status_code == 400
