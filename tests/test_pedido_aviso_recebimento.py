"""Aviso no WhatsApp do dono quando pedido vira 'recebido na loja' — com link
da pasta de fotos no Dropbox. Best-effort + idempotente.
"""
from unittest.mock import patch


def _setup(app):
    """Loja + pedido entregue + fotos = ambiente mínimo pro aviso."""
    from app.extensions import db
    from app.models import FotoRecebimento, Loja, PedidoLoja
    loja = Loja(nome='Centro', ativa=True)
    db.session.add(loja)
    db.session.commit()
    p = PedidoLoja(loja_id=loja.id, status='entregue')
    db.session.add(p)
    db.session.flush()
    db.session.add(FotoRecebimento(pedido_id=p.id, imagem_url='http://x/a.jpg'))
    db.session.add(FotoRecebimento(pedido_id=p.id, imagem_url='http://x/b.jpg'))
    db.session.commit()
    app.config['ZAPI_BOT_DONO_NUMERO'] = '5511999990000'
    return p


def test_envia_com_link_da_pasta(app):
    from app.services import pedidos_notificacao
    p = _setup(app)
    with patch('app.services.dropbox_storage.shared_link_pasta',
               return_value='https://www.dropbox.com/scl/fo/abc?dl=0'), \
         patch('app.services.zapi.enviar_texto',
               return_value={'ok': True}) as send:
        pedidos_notificacao.notificar_pedido_recebido(p)
    send.assert_called_once()
    numero, msg = send.call_args[0]
    assert numero == '5511999990000'
    assert 'Pedido recebido na loja' in msg
    assert f'Pedido #{p.id}' in msg
    assert 'Centro' in msg
    assert '2 foto(s)' in msg
    assert 'dropbox.com/scl/fo/abc?dl=0' in msg


def test_idempotente_nao_envia_duas_vezes(app):
    from app.services import pedidos_notificacao
    p = _setup(app)
    with patch('app.services.dropbox_storage.shared_link_pasta',
               return_value='https://x/y?dl=0'), \
         patch('app.services.zapi.enviar_texto',
               return_value={'ok': True}) as send:
        pedidos_notificacao.notificar_pedido_recebido(p)
        pedidos_notificacao.notificar_pedido_recebido(p)
    assert send.call_count == 1  # 2a chamada eh no-op


def test_so_avisa_quando_status_entregue(app):
    from app.services import pedidos_notificacao
    p = _setup(app)
    p.status = 'em_transporte'   # ainda nao entregue
    with patch('app.services.dropbox_storage.shared_link_pasta',
               return_value='https://x/y?dl=0'), \
         patch('app.services.zapi.enviar_texto') as send:
        pedidos_notificacao.notificar_pedido_recebido(p)
    send.assert_not_called()


def test_falha_do_dropbox_nao_bloqueia_aviso(app):
    """Sem link da pasta, o aviso ainda sai com o resumo do pedido — o link
    da pasta de fotos eh extra, nao requisito pra avisar."""
    from app.services import pedidos_notificacao
    p = _setup(app)
    with patch('app.services.dropbox_storage.shared_link_pasta',
               side_effect=RuntimeError('dropbox caiu')), \
         patch('app.services.zapi.enviar_texto',
               return_value={'ok': True}) as send:
        pedidos_notificacao.notificar_pedido_recebido(p)
    send.assert_called_once()
    msg = send.call_args[0][1]
    assert f'Pedido #{p.id}' in msg
    assert 'indispon' in msg.lower()   # avisou que o link nao saiu


def test_falha_da_zapi_nao_marca_avisado(app):
    """Z-API devolveu erro: NAO marcamos sentinela; proxima tentativa
    retransmite ao inves de pular como duplicado."""
    from app.services import pedidos_notificacao
    p = _setup(app)
    with patch('app.services.dropbox_storage.shared_link_pasta',
               return_value='https://x/y?dl=0'), \
         patch('app.services.zapi.enviar_texto',
               return_value={'ok': False, 'erro': 'http 500'}) as send:
        pedidos_notificacao.notificar_pedido_recebido(p)
        # segundo chamada deve TENTAR de novo (sem sentinela travando)
        pedidos_notificacao.notificar_pedido_recebido(p)
    assert send.call_count == 2


def test_desligavel_por_config(app):
    from app.services import pedidos_notificacao
    p = _setup(app)
    app.config['ZAPI_BOT_AVISO_RECEBIMENTO'] = False
    with patch('app.services.zapi.enviar_texto') as send:
        pedidos_notificacao.notificar_pedido_recebido(p)
    send.assert_not_called()


def test_sem_dono_nao_envia(app):
    from app.services import pedidos_notificacao
    p = _setup(app)
    app.config['ZAPI_BOT_DONO_NUMERO'] = ''
    with patch('app.services.zapi.enviar_texto') as send:
        pedidos_notificacao.notificar_pedido_recebido(p)
    send.assert_not_called()
