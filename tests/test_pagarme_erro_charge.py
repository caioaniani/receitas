"""_erro_da_charge: extrair o motivo REAL da recusa do cartão — nunca o
'gateway 200:' (08/07/2026, cliente confuso no checkout do 09CC273E).

`gateway_response.code` é o HTTP da CHAMADA ao adquirente (200 = a chamada
funcionou, NÃO a autorização). O motivo da recusa vem do EMISSOR
(acquirer_message / acquirer_return_code)."""
from app.services.pagarme import _erro_da_charge


def _charge(**last):
    return {'status': 'failed', 'last_transaction': last}


def test_prefere_acquirer_message_ao_code_200():
    """O caso real: code 200 + mensagem do emissor -> mostra a mensagem."""
    c = _charge(gateway_response={'code': '200'},
                acquirer_message='Transação não autorizada, oriente o portador '
                                 'a contatar o banco/emissor do cartão',
                acquirer_return_code='1000')
    msg = _erro_da_charge(c)
    assert 'não autorizada' in msg
    assert '1000' in msg
    assert 'gateway 200' not in msg


def test_code_200_sem_detalhe_cai_em_mensagem_amigavel():
    c = _charge(gateway_response={'code': '200'})
    msg = _erro_da_charge(c)
    assert msg == 'pagamento não autorizado pelo cartão'
    assert '200' not in msg


def test_gateway_errors_estruturado_vem_primeiro():
    c = _charge(gateway_response={'code': '200',
                                  'errors': [{'message': 'cartão expirado'}]})
    assert _erro_da_charge(c) == 'cartão expirado'


def test_acquirer_return_code_sozinho():
    c = _charge(gateway_response={'code': '200'}, acquirer_return_code='51')
    msg = _erro_da_charge(c)
    assert '51' in msg and 'não autorizada' in msg


def test_gateway_code_nao_2xx_ainda_aparece():
    """Erro real do gateway (5xx) continua sendo mostrado."""
    c = _charge(gateway_response={'code': '500', 'message': 'internal'})
    msg = _erro_da_charge(c)
    assert 'gateway 500' in msg


def test_iniciar_cartao_mostra_msg_amigavel_ao_cliente(app):
    """O cliente vê a mensagem clara; o admin (PagamentoOnline.erro) guarda o
    motivo técnico."""
    from decimal import Decimal
    from unittest.mock import patch

    from app.extensions import db
    from app.models import PagamentoOnline, PedidoOnline
    from app.services import loja_pagamento
    with app.app_context():
        p = PedidoOnline(codigo='CARD0001', nome_cliente='M',
                         email_cliente='m@x.com', modo_entrega='retirada',
                         status='aguardando_pagamento', subtotal=Decimal('50'),
                         valor_total=Decimal('50'))
        db.session.add(p)
        db.session.commit()
        motivo = 'Transação não autorizada... (código 1000)'
        with patch('app.services.pagarme.criar_pedido_cartao',
                   return_value={'ok': False, 'erro': motivo,
                                 'order_id': 'or_x', 'charge_id': 'ch_x'}):
            pag, erros = loja_pagamento.iniciar_cartao(p, 'tok_x')
        assert pag is None
        assert erros == [loja_pagamento._MSG_CARTAO_RECUSADO]
        assert 'banco emissor' in erros[0]
        assert 'gateway 200' not in erros[0]
        # admin guarda o motivo técnico + os ids pra reconciliar
        pg = PagamentoOnline.query.filter_by(pedido_id=p.id).first()
        assert pg.status == 'falhou' and pg.erro == motivo
        assert pg.pagarme_charge_id == 'ch_x'
