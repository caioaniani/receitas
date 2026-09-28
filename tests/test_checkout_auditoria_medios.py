"""Regressão dos defeitos médios da auditoria do checkout (27-28/09/2026) —
parte do núcleo (checkout, pagamento, admin). Frete, CNPJ e kits têm os
próprios arquivos."""
import json
import subprocess
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.loja_host

RAIZ = Path(__file__).resolve().parent.parent


def _produto(db, nome='Box Mimo', preco=20.0):
    from app.models import Produto
    p = Produto(nome=nome, categoria='Cestas', preco_site=preco,
                imagem_dropbox_url='https://x/p.jpg', ativo=True)
    db.session.add(p)
    db.session.commit()
    return p


def _loja(db, nome='Loja Anesio Pinto Rosa'):
    from app.models import AppConfig, Loja
    loja = Loja(nome=nome, ativa=True, endereco='Anésio Pinto Rosa, 78')
    db.session.add(loja)
    db.session.commit()
    AppConfig.set('loja_site_estoque_id', loja.id)
    return loja


def _pedido(db, prod, *, status='aguardando_pagamento', loja=None, qtd=1,
            frete=Decimal('0'), **kw):
    from app.models import PedidoOnline, PedidoOnlineItem
    p = PedidoOnline(nome_cliente='Maria', email_cliente='m@x.com',
                     telefone_cliente='11999998888',
                     modo_entrega=kw.pop('modo', 'retirada'),
                     loja_retirada_id=loja.id if loja else None,
                     status=status, frete_valor=frete,
                     subtotal=Decimal('0'), valor_total=Decimal('0'), **kw)
    db.session.add(p)
    db.session.flush()
    p.itens.append(PedidoOnlineItem(
        kind='produto', produto_id=prod.id, nome=prod.nome,
        preco_unitario=Decimal(str(prod.preco_site)), quantidade=qtd,
        subtotal=Decimal(str(prod.preco_site)) * qtd))
    p.recalcular_total()
    db.session.commit()
    return p


def _pag(db, ped, **kw):
    from app.models import PagamentoOnline
    kw.setdefault('metodo', 'pix')
    kw.setdefault('status', 'pendente')
    pg = PagamentoOnline(pedido_id=ped.id, valor=ped.valor_total, **kw)
    db.session.add(pg)
    db.session.commit()
    return pg


def _cliente_staff(app, user):
    # Rotas do admin só respondem fora dos hosts da loja (o marcador do
    # arquivo põe localhost como host da loja).
    app.config['LOJA_HOSTS'] = 'opao.online'
    c = app.test_client()
    with c.session_transaction() as s:
        s['_user_id'] = str(user.id)
        s['_fresh'] = True
    return c


# ── Carrinho cru normalizado ────────────────────────────────────────────

def test_normalizar_itens_raw_aplica_tetos_e_soma_linhas():
    from app.services.loja_checkout import normalizar_itens_raw
    assert normalizar_itens_raw({'kind': 'x'}) == []
    assert normalizar_itens_raw('lixo') == []
    out = normalizar_itens_raw([
        {'kind': 'produto', 'id': 1, 'qtd': 1000000},
        {'kind': 'produto', 'id': 1, 'qtd': 5},
        {'kind': 'produto', 'id': 1, 'qtd': 2, 'fatiado': True},
        'lixo', {'kind': 'produto', 'id': 'abc', 'qtd': 1},
    ] + [{'kind': 'produto', 'id': 100 + i, 'qtd': 1} for i in range(80)])
    assert out[0]['qtd'] == 99
    assert out[1]['qtd'] == 2 and out[1]['fatiado'] is True
    assert len(out) == 60


def test_checkout_recusa_menu_invalido_em_vez_de_sumir_com_ele(app, monkeypatch):
    """Item descartado pelo servidor recusa o pedido (antes o pedido nascia
    sem ele e o cliente só via o total)."""
    from app.extensions import db
    monkeypatch.setenv('LOJA_VISIVEL', '1')
    from app.models import PedidoOnline
    app.config['WTF_CSRF_ENABLED'] = False
    prod = _produto(db)
    c = app.test_client()
    with c.session_transaction() as s:
        s['carrinho'] = [{'kind': 'produto', 'id': prod.id, 'qtd': 1},
                         {'kind': 'produto', 'id': 999999, 'qtd': 1}]
    r = c.post('/loja/checkout', data={'nome': 'Maria Silva',
                                       'email': 'm@x.com'})
    assert r.status_code == 400
    assert 'saiu de catálogo' in r.get_data(as_text=True)
    assert PedidoOnline.query.count() == 0


# ── Tela de pagamento ───────────────────────────────────────────────────

def test_pagamento_limpa_carrinho_e_discrimina_o_frete(app):
    from app.extensions import db
    prod = _produto(db)
    ped = _pedido(db, prod, frete=Decimal('15'), modo='agendada',
                  endereco_logradouro='Rua A', endereco_numero='10',
                  endereco_cidade='Campinas', endereco_uf='SP')
    h = app.test_client().get(f'/loja/pedido/{ped.codigo}/pagamento').get_data(as_text=True)
    assert 'id="limpar-carrinho"' in h
    assert 'Itens R$ 20,00' in h and 'frete R$ 15,00' in h


def test_pagamento_retirada_usa_endereco_do_cliente_na_cobranca(app):
    from app.extensions import db
    app.config['PAGARME_PUBLIC_KEY'] = 'pk_test'
    loja = _loja(db)
    prod = _produto(db)
    ped = _pedido(db, prod, loja=loja, endereco_entrega='Retirada: Loja X',
                  endereco_logradouro='Rua do Cliente', endereco_numero='77',
                  endereco_cidade='Santos', endereco_uf='SP')
    h = app.test_client().get(f'/loja/pedido/{ped.codigo}/pagamento').get_data(as_text=True)
    assert 'value="Rua do Cliente, 77"' in h
    assert 'value="Santos"' in h and 'Retirada: Loja X' not in h


def test_pix_gerado_nao_prende_o_cliente(app):
    from app.extensions import db
    from app.utils import agora
    app.config['PAGARME_PUBLIC_KEY'] = 'pk_test'
    prod = _produto(db)
    ped = _pedido(db, prod)
    _pag(db, ped, pix_qr_code='EMV', pix_expira_em=agora() - timedelta(minutes=1))
    h = app.test_client().get(f'/loja/pedido/{ped.codigo}/pagamento').get_data(as_text=True)
    assert 'Prefere pagar com cartão?' in h and 'id="form-cartao"' in h
    assert 'Gerar novo código Pix' in h


def test_pagamento_nao_mostra_erro_tecnico(app):
    from app.extensions import db
    prod = _produto(db)
    ped = _pedido(db, prod)
    _pag(db, ped, metodo='cartao', status='falhou',
         erro='Transação não autorizada (código 1000) gateway_id XYZ')
    h = app.test_client().get(f'/loja/pedido/{ped.codigo}/pagamento').get_data(as_text=True)
    assert 'gateway_id' not in h and 'código 1000' not in h
    assert 'recusado pelo banco' in h


def test_troca_de_forma_nao_aparece_como_falha(app):
    from app.extensions import db
    prod = _produto(db)
    ped = _pedido(db, prod)
    _pag(db, ped, status='falhou',
         erro='substituído por nova tentativa (cancelado no Pagar.me)')
    h = app.test_client().get(f'/loja/pedido/{ped.codigo}/pagamento').get_data(as_text=True)
    assert 'Não foi possível processar' not in h


def test_pagamento_com_cartao_em_confirmacao_nao_mostra_formularios(app):
    from app.extensions import db
    prod = _produto(db)
    ped = _pedido(db, prod)
    _pag(db, ped, metodo='cartao', pagarme_order_id='or_1')
    h = app.test_client().get(f'/loja/pedido/{ped.codigo}/pagamento').get_data(as_text=True)
    assert 'está confirmando o seu' in h
    assert 'Gerar Pix' not in h


# ── Cartão sem resposta do gateway ──────────────────────────────────────

def _resp(status, body, headers_log=None):
    class R:
        text = ''
        def json(self):
            return body
    r = R()
    r.status_code = status
    return r


def test_criar_cartao_distingue_incerto_de_recusa(app):
    from app.extensions import db
    from app.services import pagarme
    app.config['PAGARME_API_KEY'] = 'sk_test_abc'
    prod = _produto(db)
    ped = _pedido(db, prod)
    enviados = []

    def post(url, headers=None, json=None, timeout=None):
        enviados.append(headers)
        raise TimeoutError('Read timed out')

    with patch('app.services.pagarme.requests.post', side_effect=post):
        r = pagarme.criar_pedido_cartao(ped, 'tok', idempotency_key='K-1')
    assert r['ok'] is False and r['incerto'] is True
    assert enviados[0]['Idempotency-Key'] == 'K-1'
    with patch('app.services.pagarme.requests.post',
               return_value=_resp(502, {})):
        assert pagarme.criar_pedido_cartao(ped, 'tok')['incerto'] is True
    with patch('app.services.pagarme.requests.post',
               return_value=_resp(422, {'message': 'card_token inválido'})):
        assert pagarme.criar_pedido_cartao(ped, 'tok')['incerto'] is False


def test_cartao_incerto_nao_vira_recusa_e_bloqueia_nova_cobranca(app):
    from app.extensions import db
    from app.models import PagamentoOnline
    from app.services import loja_pagamento
    app.config['PAGARME_API_KEY'] = 'sk_test_abc'
    prod = _produto(db)
    ped = _pedido(db, prod)
    with patch('app.services.pagarme.criar_pedido_cartao',
               return_value={'ok': False, 'incerto': True, 'erro': 'timeout'}):
        res, erros = loja_pagamento.iniciar_cartao(ped, 'tok')
    assert res is None and 'Não tente de novo agora' in erros[0]
    pag = PagamentoOnline.query.filter_by(pedido_id=ped.id).one()
    assert pag.status == 'pendente'
    assert pag.erro.startswith(loja_pagamento.PREFIXO_INCERTO)
    # Nova tentativa: o gateway mostra a order paga pelo código → confirma.
    achou = {'ok': True, 'orders': [{'id': 'or_x', 'status': 'paid',
                                     'pago': True, 'charge_id': 'ch_x',
                                     'charge_status': 'paid'}]}
    with patch('app.services.pagarme.buscar_orders_por_codigo',
               return_value=achou), \
            patch('app.services.pagarme.criar_pedido_pix') as novo:
        res, erros = loja_pagamento.iniciar_pix(ped)
    novo.assert_not_called()
    db.session.refresh(ped)
    assert ped.status == 'pago' and pag.pagarme_order_id == 'or_x'


def test_cartao_incerto_sem_resposta_do_gateway_nunca_libera(app):
    """Contrato substituído na revisão de 28/09/2026: sem conseguir listar
    as orders, a tentativa NUNCA conta como não criada (antes, passados 10
    min, liberava cobrar em dobro). Order ausente só encerra depois da
    janela; dentro dela a criação ainda pode estar a caminho."""
    from app.extensions import db
    from app.services import loja_pagamento
    from app.utils import agora
    prod = _produto(db)
    ped = _pedido(db, prod)
    pag = _pag(db, ped, metodo='cartao',
               erro=f'{loja_pagamento.PREFIXO_INCERTO}: timeout')
    with patch('app.services.pagarme.buscar_orders_por_codigo',
               return_value={'ok': False, 'erro': 'HTTP 400'}):
        assert loja_pagamento._consultar_tentativa(pag) is None
        pag.criado_em = agora() - timedelta(minutes=30)
        assert loja_pagamento._consultar_tentativa(pag) is None
    vazio = {'ok': True, 'orders': []}
    pag.criado_em = agora()
    with patch('app.services.pagarme.buscar_orders_por_codigo', return_value=vazio):
        assert loja_pagamento._consultar_tentativa(pag) == 'pendente'
        pag.criado_em = agora() - timedelta(minutes=30)
        assert loja_pagamento._consultar_tentativa(pag) == 'encerrado'
    assert 'encerrado' in pag.erro and 'recusado' not in pag.erro


def test_order_de_pix_nao_e_adotada_como_cartao_incerto(app):
    from app.extensions import db
    from app.services import loja_pagamento
    prod = _produto(db)
    ped = _pedido(db, prod)
    pag = _pag(db, ped, metodo='cartao',
               erro=f'{loja_pagamento.PREFIXO_INCERTO}: timeout')
    pix_vivo = {'ok': True, 'orders': [{'id': 'or_pix', 'status': 'pending',
                                        'pago': False, 'charge_id': 'ch_pix',
                                        'charge_status': 'pending',
                                        'metodo': 'pix'}]}
    with patch('app.services.pagarme.buscar_orders_por_codigo', return_value=pix_vivo):
        assert loja_pagamento._consultar_tentativa(pag) == 'pendente'
    assert pag.pagarme_order_id is None


def _evento(tipo, data):
    return {'id': 'hook_' + tipo + data['id'], 'type': tipo, 'data': data}


@pytest.mark.parametrize('evento', ['order', 'charge'])
def test_webhook_liga_a_order_a_tentativa_incerta_e_o_reembolso_estorna(app, evento):
    """Revisão 28/09/2026: o webhook de uma order que a resposta perdida não
    registrou deixava o pedido pago SEM cobrança associada, e o Reembolsar
    cancelava só aqui dizendo "estornado" ao cliente."""
    from app.extensions import db
    from app.services import loja_pagamento
    loja = _loja(db)
    prod = _produto(db)
    ped = _pedido(db, prod, loja=loja)
    pag = _pag(db, ped, metodo='cartao',
               erro=f'{loja_pagamento.PREFIXO_INCERTO}: timeout')
    if evento == 'order':
        ev = _evento('order.paid', {
            'id': 'or_nova', 'code': ped.codigo, 'status': 'paid',
            'charges': [{'id': 'ch_nova', 'payment_method': 'credit_card',
                         'status': 'paid'}]})
    else:
        ev = _evento('charge.paid', {
            'id': 'ch_nova', 'status': 'paid', 'payment_method': 'credit_card',
            'order': {'id': 'or_nova', 'code': ped.codigo}})
    with patch('app.services.loja_pagamento._baixar_estoque', return_value={}), \
            patch('app.services.kits_pagamento.apos_confirmacao'):
        loja_pagamento.processar_webhook(ev)
    db.session.refresh(ped)
    db.session.refresh(pag)
    assert ped.status == 'pago'
    assert (pag.status, pag.pagarme_order_id, pag.pagarme_charge_id) == (
        'pago', 'or_nova', 'ch_nova')
    with patch('app.services.pagarme.cancelar_charge',
               return_value={'ok': True}) as cancelar, \
            patch('app.services.email.disponivel', return_value=False), \
            patch('app.services.loja_pagamento._estornar_estoque', return_value=0), \
            patch('app.services.loja_pagamento._devolver_ao_plano_do_dia'):
        ok, _msg = loja_pagamento._reembolsar_pedido(ped)
    assert ok and cancelar.call_args.args[0] == 'ch_nova'


def test_reembolso_recusa_pedido_pago_sem_cobranca_identificada(app):
    from app.extensions import db
    from app.services import loja_pagamento
    from app.utils import agora
    prod = _produto(db)
    ped = _pedido(db, prod, status='pago', pago_em=agora())
    with patch('app.services.pagarme.cancelar_charge') as cancelar:
        ok, msg = loja_pagamento._reembolsar_pedido(ped)
    assert not ok and 'painel do Pagar.me' in msg
    cancelar.assert_not_called()
    db.session.refresh(ped)
    assert ped.status == 'pago'


def test_pagina_esclarece_cartao_incerto_que_nunca_foi_criado(app, monkeypatch):
    """Antes a tela escondia Pix e cartão até o cron cancelar o pedido; agora
    a página pergunta ao gateway (no máximo a cada 30 s) e libera."""
    from app.extensions import db
    from app.models import PagamentoOnline
    from app.services import loja_pagamento
    from app.utils import agora
    monkeypatch.setenv('LOJA_VISIVEL', '1')
    monkeypatch.setattr(loja_pagamento, '_ESCLARECIDO_EM', {})
    prod = _produto(db)
    ped = _pedido(db, prod)
    pag = _pag(db, ped, metodo='cartao',
               erro=f'{loja_pagamento.PREFIXO_INCERTO}: timeout')
    pag.criado_em = agora() - timedelta(minutes=30)
    db.session.commit()
    pag_id = pag.id
    with patch('app.services.pagarme.buscar_orders_por_codigo',
               return_value={'ok': True, 'orders': []}) as busca:
        c = app.test_client()
        r1 = c.get(f'/loja/pedido/{ped.codigo}/status')
        r2 = c.get(f'/loja/pedido/{ped.codigo}/status')
    assert busca.call_count == 1
    assert r1.get_json()['cartao_confirmando'] is False
    assert r2.get_json()['cartao_confirmando'] is False
    assert db.session.get(PagamentoOnline, pag_id).status == 'falhou'


def test_pagina_confirma_cartao_incerto_pago_no_gateway(app, monkeypatch):
    from app.extensions import db
    from app.services import loja_pagamento
    monkeypatch.setenv('LOJA_VISIVEL', '1')
    monkeypatch.setattr(loja_pagamento, '_ESCLARECIDO_EM', {})
    prod = _produto(db)
    ped = _pedido(db, prod)
    _pag(db, ped, metodo='cartao',
         erro=f'{loja_pagamento.PREFIXO_INCERTO}: timeout')
    pago = {'ok': True, 'orders': [{'id': 'or_ok', 'status': 'paid', 'pago': True,
                                    'charge_id': 'ch_ok', 'charge_status': 'paid',
                                    'metodo': 'credit_card'}]}
    with patch('app.services.pagarme.buscar_orders_por_codigo', return_value=pago), \
            patch('app.services.loja_pagamento._baixar_estoque', return_value={}), \
            patch('app.services.kits_pagamento.apos_confirmacao') as apos:
        r = app.test_client().get(f'/loja/pedido/{ped.codigo}/status')
    assert r.get_json()['status'] == 'pago'
    apos.assert_called_once()


def test_busca_de_orders_com_resposta_fora_do_formato(app):
    from app.services import pagarme
    app.config['PAGARME_API_KEY'] = 'sk_test_abc'
    for corpo in ([1, 2], {'data': 'x'}, {'data': [None, 3]}):
        with patch('app.services.pagarme.requests.get', return_value=_resp(200, corpo)):
            r = pagarme.buscar_orders_por_codigo('ABC')
        assert r.get('ok') is False or r.get('orders') == []


def test_redireciona_para_o_pedido_quando_cartao_fica_incerto(app):
    from app.extensions import db
    app.config['WTF_CSRF_ENABLED'] = False
    app.config['PAGARME_API_KEY'] = 'sk_test_abc'
    prod = _produto(db)
    ped = _pedido(db, prod)
    with patch('app.services.pagarme.criar_pedido_cartao',
               return_value={'ok': False, 'incerto': True, 'erro': 'timeout'}):
        r = app.test_client().post(f'/loja/pedido/{ped.codigo}/cartao',
                                   data={'card_token': 'tok', 'bill_cep': '01001000'})
    assert r.status_code == 302 and r.headers['Location'].endswith(f'/loja/pedido/{ped.codigo}')


# ── Admin: cancelar pedido aguardando pagamento ─────────────────────────

def test_admin_cancela_aguardando_libera_reserva_e_encerra_o_pix(app, owner_user):
    from app.extensions import db
    from app.models import EstoqueLoja
    from app.services import loja_estoque_reserva
    app.config['WTF_CSRF_ENABLED'] = False
    app.config['PAGARME_API_KEY'] = 'sk_test_abc'
    loja = _loja(db)
    prod = _produto(db)
    db.session.add(EstoqueLoja(loja_id=loja.id, produto_id=prod.id, quantidade=10))
    db.session.commit()
    ped = _pedido(db, prod, loja=loja, qtd=3)
    loja_estoque_reserva.reservar(ped, loja_id=loja.id)
    db.session.commit()
    _pag(db, ped, pagarme_order_id='or_1', pagarme_charge_id='ch_1')
    pendente = {'ok': True, 'status': 'pending', 'pago': False,
                'charge_status': 'pending'}
    c = _cliente_staff(app, owner_user)
    with patch('app.services.pagarme.consultar_order', return_value=pendente), \
            patch('app.services.pagarme.cancelar_cobranca_pendente',
                  return_value={'ok': True}) as cancelar:
        c.post(f'/admin/loja-online/pedidos/{ped.codigo}/cancelar')
    cancelar.assert_called_once_with('ch_1')
    db.session.refresh(ped)
    assert ped.status == 'cancelado'
    el = EstoqueLoja.query.filter_by(loja_id=loja.id, produto_id=prod.id).one()
    assert (el.quantidade_reservada or 0) == 0


def test_admin_nao_cancela_pedido_que_o_gateway_diz_pago(app, owner_user):
    from app.extensions import db
    app.config['WTF_CSRF_ENABLED'] = False
    prod = _produto(db)
    ped = _pedido(db, prod)
    _pag(db, ped, pagarme_order_id='or_1', pagarme_charge_id='ch_1')
    pago = {'ok': True, 'status': 'paid', 'pago': True, 'charge_status': 'paid'}
    c = _cliente_staff(app, owner_user)
    with patch('app.services.pagarme.consultar_order', return_value=pago):
        c.post(f'/admin/loja-online/pedidos/{ped.codigo}/cancelar')
    db.session.refresh(ped)
    assert ped.status == 'pago'


# ── Webhook: chargeback ─────────────────────────────────────────────────

def test_chargeback_avisa_o_dono_sem_mexer_no_pedido(app):
    from app.extensions import db
    from app.services import loja_pagamento
    prod = _produto(db)
    ped = _pedido(db, prod, status='pago')
    pag = _pag(db, ped, metodo='cartao', status='pago',
               pagarme_order_id='or_1', pagarme_charge_id='ch_1')
    with patch('app.services.loja_alerta.alertar_pedido_pago') as alerta:
        r = loja_pagamento.processar_webhook(
            {'id': 'evt_cb', 'type': 'charge.chargedback',
             'data': {'id': 'ch_1', 'code': ped.codigo}})
    assert r.get('chargeback') is True
    alerta.assert_called_once()
    db.session.refresh(ped)
    db.session.refresh(pag)
    assert ped.status == 'pago' and 'chargeback' in pag.erro


# ── Plano do dia ────────────────────────────────────────────────────────

def test_remover_regra_semanal_libera_datas_com_venda(app):
    from app.extensions import db
    from app.services import loja_plano_dia
    from app.utils import hoje
    prod = _produto(db)
    dia = hoje() + timedelta(days=1)
    loja_plano_dia.salvar_regra_semanal('produto', prod.id, range(7), 5)
    assert loja_plano_dia.reservar('produto', prod.id, dia, 5)
    assert loja_plano_dia.saldo('produto', prod.id, dia) == 0
    loja_plano_dia.remover_regra_semanal('produto', prod.id)
    assert loja_plano_dia.saldo('produto', prod.id, dia) > 0
    from app.models import EstoqueSitePlano
    assert EstoqueSitePlano.query.filter_by(item_id=prod.id, data=dia).one().qtd_reservada == 5


def test_editar_data_de_pedido_pago_move_a_reserva_do_plano(app, admin_user):
    from app.extensions import db
    from app.models import EstoqueSitePlano
    from app.services import loja_pagamento
    from app.utils import hoje
    app.config['WTF_CSRF_ENABLED'] = False
    loja = _loja(db)
    prod = _produto(db)
    d1, d2 = hoje() + timedelta(days=2), hoje() + timedelta(days=3)
    ped = _pedido(db, prod, loja=loja, qtd=2, data_entrega=d1)
    loja_pagamento._marcar_pago(ped, None, enviar_confirmacao=False)
    db.session.commit()
    c = _cliente_staff(app, admin_user)
    c.post(f'/admin/loja-online/pedidos/{ped.codigo}/editar', data={
        'nome_cliente': 'Maria', 'email_cliente': 'm@x.com',
        'modo_entrega': 'retirada', 'loja_retirada_id': str(loja.id),
        'data_entrega': d2.isoformat()})
    reservado = {r.data: r.qtd_reservada for r in
                 EstoqueSitePlano.query.filter_by(item_id=prod.id).all()}
    assert reservado.get(d1, 0) == 0 and reservado.get(d2) == 2


# ── Conta / e-mail / avisos ─────────────────────────────────────────────

def test_excluir_conta_com_pedido_pago_em_andamento_e_recusado(app):
    from app.extensions import db
    from app.models import Cliente
    app.config['WTF_CSRF_ENABLED'] = False
    prod = _produto(db)
    cli = Cliente(nome='Ana', email='ana@x.com', telefone='11911112222')
    cli.set_senha('senha-forte-1')
    db.session.add(cli)
    db.session.commit()
    ped = _pedido(db, prod, status='pago', cliente_id=cli.id)
    c = app.test_client()
    with c.session_transaction() as s:
        s['cliente_id'] = cli.id
    r = c.post('/loja/conta/excluir', data={'confirmar': 'EXCLUIR'},
               follow_redirects=True)
    assert 'pedido em andamento' in r.get_data(as_text=True)
    db.session.refresh(ped)
    assert ped.email_cliente == 'm@x.com' and ped.cliente_id == cli.id


def test_email_escapa_o_complemento(app):
    from app.extensions import db
    from app.services import email as email_svc
    prod = _produto(db)
    ped = _pedido(db, prod, modo='agendada',
                  endereco_entrega='Rua X, 10, <a href="https://evil">PAGUE</a>')
    html = email_svc._template_pedido_recebido(ped, 'https://opao.online')
    assert '<a href="https://evil">' not in html
    assert '&lt;a href=' in html


# ── Front: sincronização do carrinho e remoção por item ─────────────────

_HARNESS = r"""
const vm = require('node:vm'), fs = require('node:fs');
const posts = []; const pendentes = [];
const listeners = {};
const doc = {
  addEventListener(ev, fn) { (listeners[ev] = listeners[ev] || []).push(fn); },
  getElementById() { return null; },
  querySelector() { return null; }, querySelectorAll() { return []; },
};
const store = {};
const ctx = {
  window: {}, document: doc, console,
  localStorage: { getItem: k => store[k] || null, setItem: (k, v) => { store[k] = v; },
                  removeItem: k => { delete store[k]; } },
  fetch: (url, opts) => new Promise(res => { posts.push(JSON.parse(opts.body)); pendentes.push(res); }),
  Promise, setTimeout, JSON,
};
ctx.window = ctx; ctx.window.gtag = undefined;
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(process.argv[2], 'utf8'), ctx);
const C = ctx.Carrinho;
C.salvar([{kind: 'produto', id: 1, qtd: 1}]);
C.salvar([{kind: 'produto', id: 1, qtd: 2}]);
C.salvar([{kind: 'produto', id: 1, qtd: 3},
          {kind: 'receita', id: 5, qtd: 1, fatiado: true},
          {kind: 'receita', id: 5, qtd: 2}]);
const r = {emVoo1: posts.length, emSincronia: C.emSincronia()};
(async () => {
  pendentes.shift()({});            // termina o 1º POST
  await new Promise(s => setTimeout(s, 0));
  r.emVoo2 = posts.length;
  r.ultimo = posts[posts.length - 1].itens.map(i => i.qtd);
  pendentes.shift()({});
  await new Promise(s => setTimeout(s, 0));
  r.sincronizado = !C.emSincronia();
  C.removerItem('receita', '5');
  r.depoisRemover = C.ler().map(i => i.kind + i.id);
  process.stdout.write(JSON.stringify(r));
})();
"""


def test_carrinho_uma_gravacao_por_vez_com_o_ultimo_estado(tmp_path):
    h = tmp_path / 'h.js'
    h.write_text(_HARNESS)
    out = subprocess.run(['node', str(h), str(RAIZ / 'app/static/loja/carrinho.js')],
                         capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    r = json.loads(out.stdout)
    assert r['emVoo1'] == 1 and r['emSincronia'] is True
    # O 2º POST leva só o ÚLTIMO estado (o intermediário qtd=2 nunca vai).
    assert r['emVoo2'] == 2 and r['ultimo'] == [3, 1, 2]
    assert r['sincronizado'] is True
    # removerItem tira TODAS as linhas do item (inteiro e fatiado).
    assert r['depoisRemover'] == ['produto1']


def test_rota_do_checkout_aplica_os_tetos_do_carrinho(app, monkeypatch):
    """Os tetos (99 un., linhas iguais somadas) valem na ROTA do site — nos
    dois caminhos (sessão e itens_json). Não ficam no `criar_pedido`, que os
    kits usam com adicionais legítimos acima de 99 (revisão 28/09/2026)."""
    monkeypatch.setenv('LOJA_VISIVEL', '1')
    app.config['WTF_CSRF_ENABLED'] = False
    vistos = []

    def _criar(form, itens_raw, **kw):
        vistos.append(itens_raw)
        return None, ['parou aqui']

    monkeypatch.setattr('app.services.loja_checkout.criar_pedido', _criar)
    c = app.test_client()
    c.post('/loja/checkout', data={'itens_json': json.dumps([
        {'kind': 'produto', 'id': 7, 'qtd': 1000000},
        {'kind': 'produto', 'id': 7, 'qtd': 5}])})
    assert vistos and vistos[-1] == [{'kind': 'produto', 'id': 7, 'qtd': 99}]
