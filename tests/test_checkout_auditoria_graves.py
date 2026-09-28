"""Regressão dos 6 defeitos graves da auditoria do checkout (27/09/2026).

1. Checkout anônimo não altera a conta dona do e-mail (nem abre o Wi-Fi).
2. Plano do dia confere a QUANTIDADE; pago acima do limite reserva e avisa.
3. E-mail digitado no checkout não vira JS no admin.
4. Nova cobrança só depois de encerrar a anterior no Pagar.me.
5. Webhook que falha devolve o claim (500) e a expiração confere o gateway.
6. Janela larga de HOJE (data especial) aparece no JS como no servidor.
"""
import json
import subprocess
from contextlib import contextmanager
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import pytest

FRETE_OK = {'ok': True, 'valor': 15.0, 'gratis': False, 'fora_area': False,
            'distancia_km': 3.0, 'endereco': 'Rua Tal, 77', 'aviso': ''}
BASE = datetime(2026, 6, 17, 10, 0)


def _produto(db, nome='Box Mimo', preco=20.0):
    from app.models import Produto
    p = Produto(nome=nome, categoria='Cestas', preco_site=preco,
                imagem_dropbox_url='https://x/p.jpg', ativo=True)
    db.session.add(p)
    db.session.commit()
    return p


def _form(**kw):
    from app.services import loja_checkout
    data = loja_checkout.datas_disponiveis('agendada', base=BASE)[0]
    base = {'nome': 'Visitante Qualquer', 'email': 'dona@x.com',
            'telefone': '11988887777', 'cpf': '52998224725',
            'aceite_lgpd': '1', 'modo_entrega': 'agendada',
            'logradouro': 'Rua do Visitante', 'numero': '9',
            'bairro': 'Centro', 'cidade': 'São Paulo', 'uf': 'SP',
            'cep': '04077000', 'data_entrega': data.isoformat(),
            'janela_entrega': '12:00–13:00'}
    base.update(kw)
    return base


def _conta(db, email='dona@x.com', telefone='11911112222'):
    from app.models import Cliente
    c = Cliente(nome='Dona da Conta', email=email, telefone=telefone,
                cpf='11144477735')
    c.set_senha('senha-forte-1')
    db.session.add(c)
    db.session.commit()
    return c


@contextmanager
def _sessao_loja(app, cliente_id=None):
    from flask import session
    with app.test_request_context('/loja/checkout', method='POST'):
        if cliente_id:
            session['cliente_id'] = cliente_id
        yield


def _criar(app, prod, form, itens=None, cliente_id=None):
    from app.services import loja_checkout
    itens = itens or [{'kind': 'produto', 'id': prod.id, 'qtd': 1}]
    with patch('app.services.frete.consultar_frete', return_value=FRETE_OK), \
            _sessao_loja(app, cliente_id):
        return loja_checkout.criar_pedido(form, itens, base=BASE)


# ── 1. Conta dona do e-mail ────────────────────────────────────────────

def test_checkout_anonimo_nao_altera_conta_existente(app):
    from app.extensions import db
    from app.models import EnderecoCliente
    prod = _produto(db)
    conta = _conta(db)
    pedido, erros = _criar(app, prod, _form())
    assert erros == [] and pedido is not None
    db.session.refresh(conta)
    assert conta.nome == 'Dona da Conta'
    assert conta.telefone == '11911112222'
    assert conta.cpf == '11144477735'
    assert EnderecoCliente.query.filter_by(cliente_id=conta.id).count() == 0
    # O pedido guarda o que foi digitado (snapshot), ligado à conta.
    assert pedido.cliente_id == conta.id
    assert pedido.telefone_cliente == '11988887777'
    assert pedido.nome_cliente.startswith('Visitante')


def test_checkout_logado_na_propria_conta_atualiza_dados(app):
    from app.extensions import db
    from app.models import EnderecoCliente
    prod = _produto(db)
    conta = _conta(db)
    _, erros = _criar(app, prod, _form(nome='Dona Nova'), cliente_id=conta.id)
    assert erros == []
    db.session.refresh(conta)
    assert conta.telefone == '11988887777'
    assert EnderecoCliente.query.filter_by(cliente_id=conta.id).count() == 1


def test_logado_em_outra_conta_nao_altera_a_dona_do_email(app):
    from app.extensions import db
    prod = _produto(db)
    conta = _conta(db)
    outra = _conta(db, email='outra@x.com', telefone='11933334444')
    _, erros = _criar(app, prod, _form(), cliente_id=outra.id)
    assert erros == []
    db.session.refresh(conta)
    assert conta.telefone == '11911112222'


def test_wifi_ignora_telefone_de_pedido_nao_pago(app):
    """O telefone de um pedido criado por qualquer visitante (não pago) não
    prova posse da conta de convidado."""
    from app.extensions import db
    from app.models import Cliente, PedidoOnline
    from app.services import wifi_portal
    from app.utils import telefone_chave
    guest = Cliente(nome='Convidada', email='guest@x.com', telefone=None)
    db.session.add(guest)
    db.session.flush()
    db.session.add(PedidoOnline(cliente_id=guest.id, nome_cliente='C',
                                email_cliente='guest@x.com',
                                telefone_cliente='11955556666',
                                modo_entrega='retirada',
                                pago_em=BASE, status='pago',
                                subtotal=Decimal('10'), valor_total=Decimal('10')))
    db.session.add(PedidoOnline(cliente_id=guest.id, nome_cliente='Atacante',
                                email_cliente='guest@x.com',
                                telefone_cliente='11977778888',
                                modo_entrega='retirada',
                                status='aguardando_pagamento',
                                subtotal=Decimal('10'), valor_total=Decimal('10')))
    db.session.commit()
    assert wifi_portal._guest_tem_telefone_divergente(
        guest, telefone_chave('11977778888')) is True
    assert wifi_portal._guest_tem_telefone_divergente(
        guest, telefone_chave('11955556666')) is False


# ── 2. Plano do dia confere a quantidade ───────────────────────────────

def _limite(db, prod, data, qtd):
    from app.models import EstoqueSitePlano
    db.session.add(EstoqueSitePlano(kind='produto', item_id=prod.id, data=data,
                                    qtd_planejada=qtd, qtd_reservada=0))
    db.session.commit()


def test_checkout_recusa_quantidade_acima_do_plano(app):
    from app.extensions import db
    prod = _produto(db)
    form = _form(email='novo@x.com')
    _limite(db, prod, datetime.fromisoformat(form['data_entrega']).date(), 10)
    pedido, erros = _criar(app, prod, form,
                           itens=[{'kind': 'produto', 'id': prod.id, 'qtd': 15}])
    assert pedido is None
    assert any('só 10 unidades' in e for e in erros)


def test_checkout_soma_linhas_do_mesmo_item_contra_o_plano(app):
    """Inteiro + fatiado do mesmo pão disputam o mesmo limite."""
    from app.extensions import db
    from app.models import Receita
    from app.services import loja_checkout
    r = Receita(nome='Sourdough Tradicional', categoria='Paes',
                preco_site=30.0, site_ativo=True, familia='pao_sourdough',
                rendimento_qtd=1, rendimento_unidade='un', peso_base=1000.0,
                imagem_dropbox_url='https://x/s.jpg')
    db.session.add(r)
    db.session.commit()
    form = _form(email='novo@x.com')
    _limite_r = datetime.fromisoformat(form['data_entrega']).date()
    from app.models import EstoqueSitePlano
    db.session.add(EstoqueSitePlano(kind='receita', item_id=r.id,
                                    data=_limite_r, qtd_planejada=10,
                                    qtd_reservada=0))
    db.session.commit()
    itens = [{'kind': 'receita', 'id': r.id, 'qtd': 6},
             {'kind': 'receita', 'id': r.id, 'qtd': 6, 'fatiado': True}]
    with patch('app.services.frete.consultar_frete', return_value=FRETE_OK), \
            _sessao_loja(app):
        pedido, erros = loja_checkout.criar_pedido(form, itens, base=BASE)
    assert pedido is None and any('só 10 unidades' in e for e in erros)


def test_checkout_aceita_quantidade_dentro_do_plano(app):
    from app.extensions import db
    prod = _produto(db)
    form = _form(email='novo@x.com')
    _limite(db, prod, datetime.fromisoformat(form['data_entrega']).date(), 10)
    pedido, erros = _criar(app, prod, form,
                           itens=[{'kind': 'produto', 'id': prod.id, 'qtd': 10}])
    assert erros == [] and pedido is not None


def test_pago_acima_do_limite_reserva_e_avisa_o_dono(app):
    from app.extensions import db
    from app.models import EstoqueSitePlano
    from app.services import loja_pagamento
    prod = _produto(db)
    form = _form(email='novo@x.com')
    dia = datetime.fromisoformat(form['data_entrega']).date()
    pedido, erros = _criar(app, prod, form,
                           itens=[{'kind': 'produto', 'id': prod.id, 'qtd': 4}])
    assert erros == []
    # Outro cliente levou o saldo entre o checkout e o pagamento.
    _limite(db, prod, dia, 2)
    with patch('app.services.loja_alerta.alertar_pedido_pago') as alerta:
        loja_pagamento._marcar_pago(pedido, None, enviar_confirmacao=False)
        db.session.commit()
    linha = EstoqueSitePlano.query.filter_by(item_id=prod.id, data=dia).one()
    assert linha.qtd_reservada == 4          # a demanda paga é registrada
    alerta.assert_called_once()
    assert 'acima do limite' in alerta.call_args[0][0]


def test_api_disponibilidade_confere_quantidade(app):
    from app.extensions import db
    from app.utils import hoje
    app.config['LOJA_HOSTS'] = 'localhost'
    prod = _produto(db)
    dia = hoje() + timedelta(days=2)
    _limite(db, prod, dia, 3)
    c = app.test_client()
    with patch('app.services.loja_catalogo.por_id_publicado',
               return_value={'nome': 'Box Mimo'}):
        r = c.post('/loja/api/disponibilidade-checkout', json={
            'data': dia.isoformat(),
            'itens': [{'kind': 'produto', 'id': prod.id, 'qtd': 2},
                      {'kind': 'produto', 'id': prod.id, 'qtd': 2}]})
    assert r.status_code == 200
    j = r.get_json()
    assert len(j['esgotados']) == 1
    assert j['esgotados'][0]['disponivel'] == 3
    assert j['esgotados'][0]['pedido'] == 4


# ── 3. XSS pelo e-mail no admin ────────────────────────────────────────

def test_email_com_js_e_recusado_no_checkout(app):
    from app.services import loja_checkout
    assert not loja_checkout._email_valido("a');alert(1);//@x.co")
    assert not loja_checkout._email_valido('<b>@x.com')
    assert loja_checkout._email_valido("o'neil@uol.com.br")


def test_detalhe_admin_nao_poe_email_em_handler_inline(app, admin_user):
    from app.extensions import db
    from app.models import PedidoOnline
    ped = PedidoOnline(nome_cliente='X', email_cliente="a');alert(1);//@x.co",
                       telefone_cliente='11999998888', modo_entrega='retirada',
                       status='pago', subtotal=Decimal('10'),
                       valor_total=Decimal('10'))
    db.session.add(ped)
    db.session.commit()
    c = app.test_client()
    with c.session_transaction() as s:
        s['_user_id'] = str(admin_user.id)
        s['_fresh'] = True
    r = c.get(f'/admin/loja-online/pedidos/{ped.codigo}')
    html = r.get_data(as_text=True)
    assert r.status_code == 200
    assert "onsubmit=\"return confirm('Reenviar" not in html
    assert 'data-confirmar="Reenviar os e-mails' in html
    # O apóstrofo chega escapado no atributo e nunca é avaliado como JS.
    assert "a&#39;);alert(1);//@x.co" in html


# ── 4. Tentativa anterior encerrada no gateway ─────────────────────────

def _pedido_pag(db, prod, *, metodo, status='pendente', order='or_1'):
    from app.models import PagamentoOnline, PedidoOnline, PedidoOnlineItem
    ped = PedidoOnline(nome_cliente='M', email_cliente='m@x.com',
                       telefone_cliente='11999998888', modo_entrega='retirada',
                       status='aguardando_pagamento',
                       subtotal=Decimal('0'), valor_total=Decimal('0'))
    db.session.add(ped)
    db.session.flush()
    ped.itens.append(PedidoOnlineItem(
        kind='produto', produto_id=prod.id, nome=prod.nome,
        preco_unitario=Decimal('20'), quantidade=1, subtotal=Decimal('20')))
    ped.recalcular_total()
    pag = PagamentoOnline(pedido_id=ped.id, metodo=metodo, status=status,
                          valor=ped.valor_total, pagarme_order_id=order,
                          pagarme_charge_id='ch_1')
    db.session.add(pag)
    db.session.commit()
    return ped, pag


def test_cartao_em_analise_bloqueia_nova_cobranca(app):
    from app.extensions import db
    from app.services import loja_pagamento
    app.config['PAGARME_API_KEY'] = 'sk_test_abc'
    prod = _produto(db)
    ped, pag = _pedido_pag(db, prod, metodo='cartao')
    pendente = {'ok': True, 'status': 'pending', 'pago': False,
                'charge_status': 'processing'}
    with patch('app.services.pagarme.consultar_order', return_value=pendente), \
            patch('app.services.pagarme.criar_pedido_pix') as novo:
        res, erros = loja_pagamento.iniciar_pix(ped)
    assert res is None and 'sendo confirmado' in erros[0]
    novo.assert_not_called()
    db.session.refresh(pag)
    assert pag.status == 'pendente'


def test_tentativa_ja_paga_no_gateway_confirma_o_pedido(app):
    from app.extensions import db
    from app.services import loja_pagamento
    app.config['PAGARME_API_KEY'] = 'sk_test_abc'
    prod = _produto(db)
    ped, pag = _pedido_pag(db, prod, metodo='cartao')
    pago = {'ok': True, 'status': 'paid', 'pago': True, 'charge_status': 'paid'}
    with patch('app.services.pagarme.consultar_order', return_value=pago), \
            patch('app.services.pagarme.criar_pedido_cartao') as novo:
        res, erros = loja_pagamento.iniciar_cartao(ped, 'tok')
    assert res is None and 'já foi confirmado' in erros[0]
    novo.assert_not_called()
    db.session.refresh(ped)
    assert ped.status == 'pago' and ped.pago_em is not None


def test_pix_que_nao_cancela_no_gateway_bloqueia_novo_pix(app):
    from app.extensions import db
    from app.services import loja_pagamento
    app.config['PAGARME_API_KEY'] = 'sk_test_abc'
    prod = _produto(db)
    ped, _ = _pedido_pag(db, prod, metodo='pix')
    pendente = {'ok': True, 'status': 'pending', 'pago': False,
                'charge_status': 'pending'}
    with patch('app.services.pagarme.consultar_order', return_value=pendente), \
            patch('app.services.pagarme.cancelar_charge',
                  return_value={'ok': False, 'erro': 'HTTP 500'}), \
            patch('app.services.pagarme.criar_pedido_cartao') as novo:
        res, erros = loja_pagamento.iniciar_cartao(ped, 'tok')
    assert res is None and 'Pix gerado antes' in erros[0]
    novo.assert_not_called()


def test_gateway_fora_nao_abre_nova_cobranca(app):
    from app.extensions import db
    from app.services import loja_pagamento
    app.config['PAGARME_API_KEY'] = 'sk_test_abc'
    prod = _produto(db)
    ped, _ = _pedido_pag(db, prod, metodo='pix')
    with patch('app.services.pagarme.consultar_order',
               return_value={'ok': False, 'erro': 'timeout'}), \
            patch('app.services.pagarme.criar_pedido_pix') as novo:
        res, erros = loja_pagamento.iniciar_pix(ped)
    assert res is None and erros
    novo.assert_not_called()


def test_segundo_pagamento_avisa_o_dono(app):
    from app.extensions import db
    from app.models import PagamentoOnline
    from app.services import loja_pagamento
    prod = _produto(db)
    ped, pag1 = _pedido_pag(db, prod, metodo='cartao')
    loja_pagamento._marcar_pago(ped, pag1, enviar_confirmacao=False)
    db.session.commit()
    pag2 = PagamentoOnline(pedido_id=ped.id, metodo='pix', status='falhou',
                           valor=ped.valor_total, pagarme_order_id='or_2')
    db.session.add(pag2)
    db.session.commit()
    with patch('app.services.loja_alerta.alertar_pedido_pago') as alerta:
        mudou = loja_pagamento._marcar_pago(ped, pag2, enviar_confirmacao=False)
    assert mudou is False
    alerta.assert_called_once()
    assert 'DUAS VEZES' in alerta.call_args[0][0]


def test_pedido_cancelado_pelo_admin_nao_volta_a_pago(app):
    from app.extensions import db
    from app.services import loja_pagamento
    prod = _produto(db)
    ped, pag = _pedido_pag(db, prod, metodo='pix')
    ped.status = 'cancelado'
    ped.motivo_cancelamento = 'admin'
    db.session.commit()
    with patch('app.services.loja_alerta.alertar_pedido_pago') as alerta:
        mudou = loja_pagamento._marcar_pago(ped, pag, enviar_confirmacao=False)
        db.session.commit()
    assert mudou is False
    db.session.refresh(ped)
    assert ped.status == 'cancelado' and ped.pago_em is None
    assert pag.status == 'pago'
    alerta.assert_called_once()


def test_pix_expirado_pago_depois_volta_limpo(app):
    from app.extensions import db
    from app.services import loja_pagamento
    prod = _produto(db)
    ped, pag = _pedido_pag(db, prod, metodo='pix')
    ped.status = 'cancelado'
    ped.motivo_cancelamento = 'pix_expirado'
    ped.cancelado_em = BASE
    db.session.commit()
    assert loja_pagamento._marcar_pago(ped, pag, enviar_confirmacao=False)
    db.session.commit()
    assert ped.status == 'pago'
    assert ped.cancelado_em is None and ped.motivo_cancelamento is None


def test_confirmacao_nao_convida_a_pagar_de_novo_com_cartao_pendente(app):
    from app.extensions import db
    app.config['LOJA_HOSTS'] = 'localhost'
    prod = _produto(db)
    ped, _ = _pedido_pag(db, prod, metodo='cartao')
    html = app.test_client().get(f'/loja/pedido/{ped.codigo}').get_data(as_text=True)
    assert 'Ir para o pagamento' not in html
    assert 'Não é preciso pagar de' in html


# ── 5. Webhook devolve o claim + expiração confere o gateway ───────────

def test_webhook_que_falha_devolve_claim_e_responde_500(app):
    from app.extensions import db
    from app.models import PagarmeEvento
    app.config['PAGARME_WEBHOOK_SECRET'] = 'seg'
    app.config['LOJA_HOSTS'] = 'localhost'
    prod = _produto(db)
    ped, _ = _pedido_pag(db, prod, metodo='pix')
    evento = {'id': 'evt_falha', 'type': 'order.paid',
              'data': {'id': 'or_1', 'code': ped.codigo}}
    c = app.test_client()
    with patch('app.services.loja_pagamento._marcar_pago',
               side_effect=RuntimeError('banco caiu')):
        r = c.post('/loja/webhook/pagarme?k=seg', json=evento)
    assert r.status_code == 500
    assert PagarmeEvento.query.filter_by(evento_id='evt_falha').count() == 0
    # A reentrega processa de verdade.
    r2 = c.post('/loja/webhook/pagarme?k=seg', json=evento)
    assert r2.status_code == 200 and r2.get_json().get('pago') is True
    db.session.refresh(ped)
    assert ped.status == 'pago'


def _expirado(db, ped, pag, *, qr_valido=False):
    """Pedido vencido, com loja de origem (sem loja o cron só limpa o prazo
    e nunca cancelaria — o teste não distinguiria nada)."""
    from app.models import Loja
    from app.utils import agora
    loja = Loja.query.filter_by(nome='Loja Anesio Pinto Rosa').first()
    if loja is None:
        loja = Loja(nome='Loja Anesio Pinto Rosa', ativa=True)
        db.session.add(loja)
        db.session.flush()
    ped.loja_retirada_id = loja.id
    ped.reserva_expira_em = agora() - timedelta(minutes=1)
    pag.pix_expira_em = (agora() + timedelta(minutes=5) if qr_valido
                         else agora() - timedelta(minutes=5))
    db.session.commit()


def test_expiracao_marca_pago_quando_o_gateway_confirma(app):
    from app.extensions import db
    from app.services import loja_estoque_reserva
    prod = _produto(db)
    ped, pag = _pedido_pag(db, prod, metodo='pix')
    _expirado(db, ped, pag)
    pago = {'ok': True, 'status': 'paid', 'pago': True, 'charge_status': 'paid'}
    with patch('app.services.pagarme.consultar_order', return_value=pago):
        assert loja_estoque_reserva.liberar_expirados() == []
    db.session.refresh(ped)
    assert ped.status == 'pago'


def test_expiracao_nao_cancela_com_qr_ainda_valido(app):
    from app.extensions import db
    from app.services import loja_estoque_reserva
    prod = _produto(db)
    ped, pag = _pedido_pag(db, prod, metodo='pix')
    _expirado(db, ped, pag, qr_valido=True)
    with patch('app.services.pagarme.consultar_order') as consulta:
        assert loja_estoque_reserva.liberar_expirados() == []
    consulta.assert_not_called()
    db.session.refresh(ped)
    assert ped.status == 'aguardando_pagamento'


def test_expiracao_com_gateway_fora_adia(app):
    from app.extensions import db
    from app.services import loja_estoque_reserva
    prod = _produto(db)
    ped, pag = _pedido_pag(db, prod, metodo='pix')
    _expirado(db, ped, pag)
    with patch('app.services.pagarme.consultar_order',
               return_value={'ok': False, 'erro': 'timeout'}):
        assert loja_estoque_reserva.liberar_expirados() == []
    db.session.refresh(ped)
    assert ped.status == 'aguardando_pagamento'


def test_expiracao_cancela_pix_vencido_pendente_no_gateway(app):
    from app.extensions import db
    from app.services import loja_estoque_reserva
    prod = _produto(db)
    ped, pag = _pedido_pag(db, prod, metodo='pix')
    _expirado(db, ped, pag)
    pendente = {'ok': True, 'status': 'pending', 'pago': False,
                'charge_status': 'pending'}
    with patch('app.services.pagarme.consultar_order', return_value=pendente), \
            patch('app.services.pagarme.cancelar_charge',
                  return_value={'ok': True}) as cancelar:
        assert loja_estoque_reserva.liberar_expirados() == [ped.codigo]
    cancelar.assert_called_once_with('ch_1')
    db.session.refresh(ped)
    assert ped.status == 'cancelado' and ped.motivo_cancelamento == 'pix_expirado'


# ── 6. Janela larga de HOJE no JS ──────────────────────────────────────

_JS = Path(__file__).resolve().parent.parent / 'app/static/loja/checkout.js'


@pytest.mark.parametrize('janela,min_hora,esperado', [
    ('06:00–10:00', 9, True),     # 07:xx + 2h de lead: faixa ainda serve
    ('06:00–10:00', 10, False),
    ('09:00–10:00', 9, True),     # 1h: mesmo corte de sempre
    ('08:00–09:00', 9, False),
    ('xx', 9, True),              # ilegível: o servidor decide
])
def test_js_filtra_janela_de_hoje_pelo_fim(janela, min_hora, esperado):
    script = f"""
const vm = require('node:vm'), fs = require('node:fs');
const ctx = {{window: {{}}, document: {{addEventListener() {{}}}}}};
vm.createContext(ctx);
vm.runInContext(fs.readFileSync({json.dumps(str(_JS))}, 'utf8'), ctx);
process.stdout.write(String(ctx.window.LojaCheckout.janelaAindaServe(
  {json.dumps(janela)}, {min_hora})));
"""
    out = subprocess.run(['node', '-e', script], capture_output=True,
                         text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    assert out.stdout == ('true' if esperado else 'false')


def test_js_e_servidor_concordam_na_janela_larga():
    from app.services import loja_checkout
    base = datetime(2026, 8, 9, 7, 29)
    assert loja_checkout._sem_janelas_passadas(['06:00–10:00'], base) == [
        '06:00–10:00']
