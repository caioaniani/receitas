"""Aviso no WhatsApp do dono quando pedido vira 'recebido na loja' — com link
da pasta de fotos no Dropbox. Best-effort + idempotente.
"""
from unittest.mock import patch


def _setup(app):
    """Loja + pedido entregue + fotos de recebimento (com storage_path)."""
    from app.extensions import db
    from app.models import FotoRecebimento, Loja, PedidoLoja
    loja = Loja(nome='Centro', ativa=True)
    db.session.add(loja)
    db.session.commit()
    p = PedidoLoja(loja_id=loja.id, status='entregue')
    db.session.add(p)
    db.session.flush()
    db.session.add(FotoRecebimento(
        pedido_id=p.id, imagem_url='http://x/a.jpg',
        imagem_storage_path=f'/recebimento/{p.id}/a.jpg'))
    db.session.add(FotoRecebimento(
        pedido_id=p.id, imagem_url='http://x/b.jpg',
        imagem_storage_path=f'/recebimento/{p.id}/b.jpg'))
    db.session.commit()
    app.config['ZAPI_BOT_DONO_NUMERO'] = '5511999990000'
    return p


def test_envia_com_link_da_pasta(app):
    from app.services import pedidos_notificacao
    p = _setup(app)
    with patch('app.services.dropbox_storage.shared_link_pasta',
               return_value='https://www.dropbox.com/scl/fo/abc?dl=0') as lk, \
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
    # Pasta derivada do storage_path real (nao hardcode)
    lk.assert_called_once_with(f'/recebimento/{p.id}')


def test_fotos_de_conferencia_handshake_aparecem(app):
    """Bug de prod 2026-06-10: pedido recebido via handshake QR tem fotos em
    /conferencia/<id> (PedidoItemFoto) — o aviso dizia '(sem fotos)' porque
    so olhava FotoRecebimento. Agora conta as duas e o link vai pra pasta
    onde as fotos realmente estao."""
    from app.extensions import db
    from app.models import (
        Loja,
        PedidoItem,
        PedidoItemFoto,
        PedidoLoja,
        Receita,
    )
    from app.services import pedidos_notificacao
    loja = Loja(nome='Ribeiro do Vale', ativa=True)
    r = Receita(nome='Sourdough', categoria='Paes', rendimento_qtd=1,
                rendimento_unidade='un', peso_base=100.0)
    db.session.add_all([loja, r])
    db.session.flush()
    p = PedidoLoja(loja_id=loja.id, status='entregue')
    db.session.add(p)
    db.session.flush()
    item = PedidoItem(pedido_id=p.id, receita_id=r.id, quantidade=10)
    db.session.add(item)
    db.session.flush()
    db.session.add(PedidoItemFoto(
        pedido_item_id=item.id, etapa='entrega',
        imagem_url='http://x/c.jpg',
        imagem_storage_path=f'/conferencia/{p.id}/{item.id}_entrega.jpg'))
    db.session.commit()
    app.config['ZAPI_BOT_DONO_NUMERO'] = '5511999990000'

    with patch('app.services.dropbox_storage.shared_link_pasta',
               return_value='https://www.dropbox.com/scl/fo/conf?dl=0') as lk, \
         patch('app.services.zapi.enviar_texto',
               return_value={'ok': True}) as send:
        pedidos_notificacao.notificar_pedido_recebido(p)
    send.assert_called_once()
    msg = send.call_args[0][1]
    assert '1 foto(s)' in msg
    assert 'sem fotos' not in msg
    assert 'dropbox.com/scl/fo/conf?dl=0' in msg
    lk.assert_called_once_with(f'/conferencia/{p.id}')


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


# ── Digest das 12:00 (14/08/2026: "acumula até as 12:00, uma mensagem") ──

def _pedido_entregue(loja, com_foto=True):
    from app.extensions import db
    from app.models import FotoRecebimento, PedidoLoja
    from app.utils import hoje
    p = PedidoLoja(loja_id=loja.id, status='entregue', data_entrega=hoje())
    db.session.add(p)
    db.session.flush()
    if com_foto:
        db.session.add(FotoRecebimento(
            pedido_id=p.id, imagem_url='http://x/a.jpg',
            imagem_storage_path=f'/recebimento/{p.id}/a.jpg'))
    db.session.commit()
    return p


def test_digest_junta_todos_numa_mensagem_so(app):
    from app.extensions import db
    from app.models import Loja
    from app.services import pedidos_notificacao
    loja = Loja(nome='Nebraska', ativa=True)
    db.session.add(loja)
    db.session.commit()
    p1 = _pedido_entregue(loja)
    p2 = _pedido_entregue(loja)
    app.config['ZAPI_BOT_DONO_NUMERO'] = '5511999990000'
    with patch('app.services.dropbox_storage.shared_link_pasta',
               return_value='https://dropbox/x?dl=0'), \
         patch('app.services.zapi.enviar_texto',
               return_value={'ok': True}) as send:
        res = pedidos_notificacao.enviar_digest_recebimentos()
    assert res == {'enviado': True, 'pedidos': 2}
    send.assert_called_once()                       # UMA mensagem
    msg = send.call_args[0][1]
    assert f'Pedido #{p1.id}' in msg
    assert f'Pedido #{p2.id}' in msg
    assert 'Pedidos recebidos nas lojas* (2)' in msg
    # marcou os dois — segunda rodada não reenvia
    with patch('app.services.zapi.enviar_texto') as send2:
        res2 = pedidos_notificacao.enviar_digest_recebimentos()
    assert res2['motivo'] == 'sem_pendentes'
    send2.assert_not_called()


def test_digest_sem_pendentes_nao_envia(app):
    from app.services import pedidos_notificacao
    app.config['ZAPI_BOT_DONO_NUMERO'] = '5511999990000'
    with patch('app.services.zapi.enviar_texto') as send:
        res = pedidos_notificacao.enviar_digest_recebimentos()
    assert res['motivo'] == 'sem_pendentes'
    send.assert_not_called()


def test_digest_falha_no_envio_nao_marca_ninguem(app):
    """Z-API falhou: nenhum sentinela gravado — o digest do dia seguinte
    (dentro da janela) re-tenta com os mesmos pedidos."""
    from app.extensions import db
    from app.models import Loja
    from app.services import pedidos_notificacao
    loja = Loja(nome='Anesio', ativa=True)
    db.session.add(loja)
    db.session.commit()
    _pedido_entregue(loja)
    app.config['ZAPI_BOT_DONO_NUMERO'] = '5511999990000'
    with patch('app.services.dropbox_storage.shared_link_pasta',
               return_value=None), \
         patch('app.services.zapi.enviar_texto',
               return_value={'ok': False, 'erro': 'http 500'}):
        res = pedidos_notificacao.enviar_digest_recebimentos()
    assert res['motivo'] == 'erro_envio'
    assert pedidos_notificacao.pedidos_pendentes_de_aviso() != []


def test_digest_ignora_ja_avisado_e_fora_da_janela(app):
    from datetime import timedelta

    from app.extensions import db
    from app.models import Loja, PedidoLoja
    from app.services import pedidos_notificacao
    from app.utils import hoje
    loja = Loja(nome='Ribeiro', ativa=True)
    db.session.add(loja)
    db.session.commit()
    avisado = _pedido_entregue(loja)
    avisado.observacao = '[avisado-fotos]'
    antigo = PedidoLoja(loja_id=loja.id, status='entregue',
                        data_entrega=hoje() - timedelta(days=30))
    pendente_real = _pedido_entregue(loja)
    db.session.add(antigo)
    db.session.commit()
    ids = [p.id for p in pedidos_notificacao.pedidos_pendentes_de_aviso()]
    assert pendente_real.id in ids
    assert avisado.id not in ids
    assert antigo.id not in ids


def test_digest_pega_pedido_recebido_com_atraso(app):
    """Achado da revisão 14/08: `data_entrega` é a data PLANEJADA — pedido
    preso e recebido dias depois sairia da janela e nunca seria avisado.
    O executor de recebimento agora carimba `modificado_em`, e a janela
    do digest olha esse carimbo também."""
    from datetime import timedelta

    from app.extensions import db
    from app.models import Loja, PedidoLoja
    from app.services import pedidos_notificacao
    from app.utils import agora, hoje
    loja = Loja(nome='Atrasada', ativa=True)
    db.session.add(loja)
    db.session.commit()
    p = PedidoLoja(loja_id=loja.id, status='entregue',
                   data_entrega=hoje() - timedelta(days=10),
                   modificado_em=agora())      # recebido HOJE, com atraso
    db.session.add(p)
    db.session.commit()
    ids = [x.id for x in pedidos_notificacao.pedidos_pendentes_de_aviso()]
    assert p.id in ids


def test_executor_de_recebimento_carimba_modificado_em(app):
    from datetime import timedelta

    from app.blueprints.pedidos.routes import _executar_recebimento_pedido
    from app.extensions import db
    from app.models import (
        Loja,
        PedidoItem,
        PedidoItemFoto,
        PedidoLoja,
        Receita,
    )
    from app.utils import hoje
    loja = Loja(nome='Carimbo', ativa=True)
    r = Receita(nome='Pao Carimbo', categoria='Paes', rendimento_qtd=1,
                rendimento_unidade='un', peso_base=100.0)
    db.session.add_all([loja, r])
    db.session.flush()
    p = PedidoLoja(loja_id=loja.id, status='em_transporte',
                   data_entrega=hoje() - timedelta(days=10))
    db.session.add(p)
    db.session.flush()
    item = PedidoItem(pedido_id=p.id, receita_id=r.id, quantidade=5)
    db.session.add(item)
    db.session.flush()
    db.session.add(PedidoItemFoto(
        pedido_item_id=item.id, etapa='entrega',
        imagem_url='http://x/c.jpg',
        imagem_storage_path=f'/conferencia/{p.id}/{item.id}_entrega.jpg'))
    db.session.commit()
    assert p.modificado_em is None
    ok, _msg, _div = _executar_recebimento_pedido(p, user=None)
    db.session.commit()
    assert ok is True
    assert p.modificado_em is not None


def test_digest_pula_pedido_de_teste_do_owner(app):
    """O pedido sintético da rota /admin/teste-aviso-recebimento carrega
    '[PEDIDO-TESTE-AVISO]' na observacao — se o envio imediato do teste
    falhar, ele NÃO pode vazar pro digest real."""
    from app.extensions import db
    from app.models import Loja
    from app.services import pedidos_notificacao
    loja = Loja(nome='TesteOwner', ativa=True)
    db.session.add(loja)
    db.session.commit()
    p = _pedido_entregue(loja)
    p.observacao = '[PEDIDO-TESTE-AVISO] criado pela rota de teste'
    db.session.commit()
    assert p.id not in [x.id for x in
                        pedidos_notificacao.pedidos_pendentes_de_aviso()]


def test_digest_capa_e_deixa_o_resto_pro_proximo(app):
    from unittest.mock import patch as _patch

    from app.extensions import db
    from app.models import Loja
    from app.services import pedidos_notificacao
    loja = Loja(nome='Volume', ativa=True)
    db.session.add(loja)
    db.session.commit()
    for _ in range(4):
        _pedido_entregue(loja, com_foto=False)
    app.config['ZAPI_BOT_DONO_NUMERO'] = '5511999990000'
    with _patch.object(pedidos_notificacao, '_MAX_PEDIDOS_DIGEST', 3), \
         patch('app.services.zapi.enviar_texto',
               return_value={'ok': True}) as send:
        res = pedidos_notificacao.enviar_digest_recebimentos()
    assert res['pedidos'] == 3
    assert 'e mais 1 no proximo digest' in send.call_args[0][1]
    # o 4º não foi marcado — fica pro digest seguinte
    assert len(pedidos_notificacao.pedidos_pendentes_de_aviso()) == 1


def test_digest_desligavel_por_config(app):
    from app.extensions import db
    from app.models import Loja
    from app.services import pedidos_notificacao
    loja = Loja(nome='Centro2', ativa=True)
    db.session.add(loja)
    db.session.commit()
    _pedido_entregue(loja)
    app.config['ZAPI_BOT_DONO_NUMERO'] = '5511999990000'
    app.config['ZAPI_BOT_AVISO_RECEBIMENTO'] = False
    with patch('app.services.zapi.enviar_texto') as send:
        res = pedidos_notificacao.enviar_digest_recebimentos()
    assert res['motivo'] == 'indisponivel'
    send.assert_not_called()


def test_entrega_na_web_nao_dispara_aviso_imediato(app):
    """Contrato 14/08/2026: receber pedido NÃO manda WhatsApp na hora — o
    digest das 12:00 cobre. Trava contra reintroduzir o envio picado."""
    import inspect

    from app.blueprints.pedidos import routes as pedidos_routes
    fonte = inspect.getsource(pedidos_routes)
    assert 'notificar_pedido_recebido' not in fonte
    from app.services import copilot as copilot_mod
    fonte_cp = inspect.getsource(copilot_mod)
    assert 'notificar_pedido_recebido(p)' not in fonte_cp


def test_rota_teste_cria_dispara_e_limpa(app):
    """Rota owner /admin/teste-aviso-recebimento: cria pedido de teste,
    dispara o aviso, e ?limpar= apaga (mas recusa apagar pedido real)."""
    from app.extensions import db
    from app.models import Loja, PedidoLoja, Usuario
    u = Usuario(nome='Dono', login='dono2', papel='admin', is_owner=True)
    u.set_senha('x' * 8)
    db.session.add_all([u, Loja(nome='Centro', ativa=True)])
    db.session.commit()
    c = app.test_client()
    with c.session_transaction() as s:
        s['_user_id'] = str(u.id)
        s['_fresh'] = True
    app.config['ZAPI_BOT_DONO_NUMERO'] = '5511999990000'

    with patch('app.services.dropbox_storage.disponivel', return_value=False), \
         patch('app.services.dropbox_storage.shared_link_pasta',
               return_value=None), \
         patch('app.services.zapi.enviar_texto',
               return_value={'ok': True}) as send:
        r = c.get('/admin/teste-aviso-recebimento')
    data = r.get_json()
    assert data['ok'] is True
    pid = data['pedido_id']
    send.assert_called_once()

    # pedido REAL (sem marcador) nao pode ser apagado pela rota
    real = PedidoLoja(loja_id=Loja.query.first().id, status='entregue')
    db.session.add(real)
    db.session.commit()
    r2 = c.get(f'/admin/teste-aviso-recebimento?limpar={real.id}')
    assert 'recusado' in r2.get_json()['erro']
    assert PedidoLoja.query.get(real.id) is not None

    # o de TESTE pode
    r3 = c.get(f'/admin/teste-aviso-recebimento?limpar={pid}')
    assert r3.get_json()['ok'] is True
    assert PedidoLoja.query.get(pid) is None
