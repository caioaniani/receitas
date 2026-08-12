"""Cancelamento/reembolso de pedido do site com a expedição já em cima.

Caso real 16/07/2026 (pedido 16CF21D8): reembolso disparado com a entrega
armada — a cliente recebeu o aviso de cancelamento e o time viu "cancelado
× em entrega". Decisão do dono (opção "a"): confirmação explícita quando a
expedição já está com o pedido. Também fecha o buraco de dinheiro achado
na investigação: em_preparo/a_caminho caíam no ramo "só marca cancelado" e
o cliente ficava SEM reembolso.
"""
from decimal import Decimal
from unittest.mock import patch

import pytest

from app.extensions import db


@pytest.fixture
def cliente(app):
    return app.test_client()


def _login(cliente, user):
    with cliente.session_transaction() as sess:
        sess['_user_id'] = str(user.id)
        sess['_fresh'] = True


def _pedido(status='pago', codigo='PED-ROTA-1'):
    from app.models import PedidoOnline
    p = PedidoOnline(codigo=codigo, nome_cliente='Cliente X',
                     email_cliente='x@x.com', modo_entrega='agendada',
                     valor_total=Decimal('100'), status=status)
    db.session.add(p)
    db.session.commit()
    return p


def test_pago_sem_sinal_cancela_direto(app, owner_user, cliente):
    """Pedido pago, expedição ainda nem viu: reembolsa sem exigir confirmação
    extra (comportamento de sempre)."""
    p = _pedido('pago')
    _login(cliente, owner_user)
    with patch('app.services.loja_pagamento.reembolsar_pedido',
               return_value=(True, 'ok')) as rb:
        resp = cliente.post(f'/admin/loja-online/pedidos/{p.codigo}/cancelar',
                            data={})
    assert resp.status_code == 302
    rb.assert_called_once()


def test_a_caminho_sem_confirmacao_recusa(app, owner_user, cliente):
    """EM ROTA sem confirmar_expedicao: NÃO cancela, avisa pra segurar a
    entrega. Fail-closed — POST forjado sem o campo não fura."""
    p = _pedido('a_caminho')
    _login(cliente, owner_user)
    with patch('app.services.loja_pagamento.reembolsar_pedido') as rb:
        resp = cliente.post(f'/admin/loja-online/pedidos/{p.codigo}/cancelar',
                            data={}, follow_redirects=True)
    rb.assert_not_called()
    body = resp.get_data(as_text=True)
    assert 'NÃO cancelado' in body
    assert 'SEGURAR' in body
    with app.app_context():
        from app.models import PedidoOnline
        assert PedidoOnline.query.filter_by(
            codigo=p.codigo).one().status == 'a_caminho'


def test_a_caminho_com_confirmacao_reembolsa(app, owner_user, cliente):
    """Com confirmar_expedicao=1 o reembolso sai — e pelo caminho do
    reembolso REAL (antes em_preparo/a_caminho cancelavam sem devolver o
    dinheiro do cliente)."""
    p = _pedido('a_caminho')
    _login(cliente, owner_user)
    with patch('app.services.loja_pagamento.reembolsar_pedido',
               return_value=(True, 'ok')) as rb:
        cliente.post(f'/admin/loja-online/pedidos/{p.codigo}/cancelar',
                     data={'confirmar_expedicao': '1'})
    rb.assert_called_once()


def test_pago_com_lalamove_ativa_exige_confirmacao(app, owner_user, cliente):
    """Status ainda 'pago' mas motoboy Lalamove já chamado: sinal de
    expedição vale e a confirmação é exigida. Cotação (sem corrida) não."""
    from app.models import LalamoveEntrega
    p = _pedido('pago', codigo='PED-LALA-1')
    db.session.add(LalamoveEntrega(pedido_code=p.codigo,
                                   status='ASSIGNING_DRIVER'))
    db.session.commit()
    _login(cliente, owner_user)
    with patch('app.services.loja_pagamento.reembolsar_pedido') as rb:
        resp = cliente.post(f'/admin/loja-online/pedidos/{p.codigo}/cancelar',
                            data={}, follow_redirects=True)
    rb.assert_not_called()
    assert 'Lalamove' in resp.get_data(as_text=True)


def test_cozinha_visto_exige_confirmacao(app, owner_user, cliente):
    from app.models import PainelPedidoStatus
    p = _pedido('pago', codigo='PED-COZ-1')
    db.session.add(PainelPedidoStatus(pedido_code=p.codigo, status='pronto'))
    db.session.commit()
    _login(cliente, owner_user)
    with patch('app.services.loja_pagamento.reembolsar_pedido') as rb:
        cliente.post(f'/admin/loja-online/pedidos/{p.codigo}/cancelar',
                     data={})
    rb.assert_not_called()


def test_aguardando_pagamento_segue_cancelando_sem_reembolso(app, owner_user,
                                                             cliente):
    """Nada foi cobrado: só marca cancelado (comportamento de sempre)."""
    p = _pedido('aguardando_pagamento', codigo='PED-AGU-1')
    _login(cliente, owner_user)
    with patch('app.services.loja_pagamento.reembolsar_pedido') as rb:
        cliente.post(f'/admin/loja-online/pedidos/{p.codigo}/cancelar',
                     data={})
    rb.assert_not_called()
    with app.app_context():
        from app.models import PedidoOnline
        pp = PedidoOnline.query.filter_by(codigo=p.codigo).one()
        assert pp.status == 'cancelado'
        assert pp.motivo_cancelamento == 'cancelado_admin'


def test_prompt_direciona_prospeccao_para_email(app):
    """Prospecção comercial: o bot direciona pra contato@opao.online e não
    transfere (decisão do dono 17/07/2026)."""
    from app.services.chatbot_prompt import PROMPT
    assert 'contato@opao.online' in PROMPT
    assert 'PROSPECÇÃO COMERCIAL' in PROMPT


# ── Estorno de pedido ENTREGUE (dono 12/08/2026, caso 131B16EA) ──────────

def test_entregue_sem_confirmacao_recusa(app, owner_user, cliente):
    """Entregue continua protegido: sem o gesto explícito, nada acontece."""
    p = _pedido('entregue', codigo='PED-ENT-1')
    _login(cliente, owner_user)
    with patch('app.services.loja_pagamento.reembolsar_pedido') as rb:
        cliente.post(f'/admin/loja-online/pedidos/{p.codigo}/cancelar',
                     data={})
    rb.assert_not_called()
    db.session.refresh(p)
    assert p.status == 'entregue'


def test_entregue_com_confirmacao_reembolsa(app, owner_user, cliente):
    """Com confirmar_entregue=1 (botão próprio), reembolsa total."""
    p = _pedido('entregue', codigo='PED-ENT-2')
    _login(cliente, owner_user)
    with patch('app.services.loja_pagamento.reembolsar_pedido',
               return_value=(True, 'ok')) as rb:
        cliente.post(f'/admin/loja-online/pedidos/{p.codigo}/cancelar',
                     data={'confirmar_entregue': '1'})
    rb.assert_called_once()


def test_reembolso_manda_email_de_estorno_e_nao_recredita_estoque(app):
    """O reembolso de pedido ENTREGUE: (1) dispara o e-mail de comprovante
    do estorno pro cliente; (2) NÃO re-credita estoque (mercadoria saiu —
    estado_anterior != 'pago' no _marcar_estornado)."""
    from unittest.mock import patch as _patch

    from app.services import loja_pagamento
    p = _pedido('entregue', codigo='PED-ENT-3')
    with _patch('app.services.pagarme.cancelar_charge',
                return_value={'ok': True}), \
         _patch('app.services.email.disponivel', return_value=True), \
         _patch('app.services.email.enviar_reembolso_confirmado',
                return_value={'ok': True}) as mail, \
         _patch('app.services.loja_pagamento._estornar_estoque') as est:
        ok, msg = loja_pagamento.reembolsar_pedido(p)
    assert ok is True
    mail.assert_called_once()
    assert mail.call_args[0][0].codigo == 'PED-ENT-3'
    est.assert_not_called()                 # entregue nunca re-credita
    db.session.refresh(p)
    assert p.status == 'cancelado'
    assert p.motivo_cancelamento == 'reembolso'
