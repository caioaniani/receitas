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


def test_baixar_danfe_conteudo_estranho_da_motivo(app):
    """O link resolve mas o download não é PDF nem HTML (ex: JSON de erro) —
    o motivo diz que não veio PDF, em vez de sumir."""
    class _R:
        status_code = 200
        headers = {'Content-Type': 'application/json'}
        content = b'{"erro":"x"}'
    with app.app_context():
        app.config['TINY_API_TOKEN'] = 'tok'
        with patch('app.services.tiny.obter_link_nota_fiscal_com_motivo',
                   return_value=('https://tiny/x', None)), \
             patch('requests.get', return_value=_R()):
            pdf, motivo = tiny_nf.baixar_danfe_pdf_com_motivo('909')
    assert pdf is None and 'PDF' in motivo


class _Resp:
    def __init__(self, content=b'', ctype='application/pdf', status=200,
                 url='https://erp.olist.com/doc.view?id=x', text=None):
        self.content = content
        self.status_code = status
        self.headers = {'Content-Type': ctype}
        self.url = url
        self.text = text if text is not None else ''


def test_candidatos_pdf_na_pagina_resolve_relativo():
    html = ('<html><body><iframe src="/nfe/danfe_123.pdf"></iframe>'
            '<a href="https://cdn.olist.com/x.pdf">baixar</a>'
            '<img src="/logo.png"></body></html>')
    cands = tiny_nf._candidatos_pdf_na_pagina(
        html, 'https://erp.olist.com/doc.view?id=x')
    assert cands == ['https://erp.olist.com/nfe/danfe_123.pdf',
                     'https://cdn.olist.com/x.pdf']


def test_baixar_html_do_olist_converte_em_pdf(app):
    """Download vem HTML (visualizador Olist, que renderiza o DANFE) → o
    weasyprint converte o HTML em PDF."""
    html = '<html><body>DANFE 011629</body></html>'
    with app.app_context():
        app.config['TINY_API_TOKEN'] = 'tok'
        with patch('app.services.tiny.obter_link_nota_fiscal_com_motivo',
                   return_value=('https://erp.olist.com/doc.view?id=x', None)), \
             patch('requests.get',
                   return_value=_Resp(ctype='text/html; charset=utf-8',
                                      text=html)), \
             patch('app.services.tiny_nf._html_para_pdf',
                   return_value=b'%PDF-convertido') as conv:
            pdf, motivo = tiny_nf.baixar_danfe_pdf_com_motivo('909')
    assert pdf == b'%PDF-convertido' and motivo is None
    args, _ = conv.call_args
    assert args[0] == html                          # HTML do Olist
    assert args[1] == 'https://erp.olist.com/doc.view?id=x'   # base_url


def test_baixar_html_conversao_falha_da_motivo(app):
    """Se a conversão HTML→PDF falhar (weasyprint indisponível/erro) →
    motivo claro, sem derrubar nada."""
    html = '<html><body>DANFE</body></html>'
    with app.app_context():
        app.config['TINY_API_TOKEN'] = 'tok'
        with patch('app.services.tiny.obter_link_nota_fiscal_com_motivo',
                   return_value=('https://erp.olist.com/doc.view?id=x', None)), \
             patch('requests.get',
                   return_value=_Resp(ctype='text/html', text=html)), \
             patch('app.services.tiny_nf._html_para_pdf', return_value=None):
            pdf, motivo = tiny_nf.baixar_danfe_pdf_com_motivo('909')
    assert pdf is None and 'conversão pra PDF falhou' in motivo


def test_html_para_pdf_render_real():
    """Sanidade do conversor com weasyprint de verdade (o PDF sai com o
    magic %PDF)."""
    pytest = __import__('pytest')
    try:
        import weasyprint  # noqa: F401
    except Exception:
        pytest.skip('weasyprint não instalado neste ambiente')
    pdf = tiny_nf._html_para_pdf('<html><body>DANFE 011629</body></html>',
                                 'https://erp.olist.com/doc.view?id=x')
    assert pdf and pdf[:5] == b'%PDF-'


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
    """O envio conjunto mantém a causa real da falha do DANFE."""
    from tests.test_b2b_email_docs import _cenario, _post_conjunto, _preparar_nf
    _, venda, parcela, _ = _cenario()
    _preparar_nf(venda)
    c = app.test_client()
    _login(c, admin_user.id)
    with patch('app.services.tiny_nf.baixar_danfe_pdf_com_motivo',
               return_value=(None, 'Tiny fora do ar (HTTP 503)')):
        r = _post_conjunto(c, parcela)
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
