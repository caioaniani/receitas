"""Pedidos que já saíram não voltam à fila nem ao pré-preparo do padeiro."""
from datetime import timedelta

from app.extensions import db
from app.models import (
    EstoqueProducao,
    PedidoOnline,
    PedidoOnlineItem,
    Receita,
    SaidaProducaoSite,
)
from app.utils import hoje


def _pedido():
    pedido = PedidoOnline(
        codigo='SAIDAPAD', nome_cliente='Cliente', email_cliente='cliente@example.test',
        modo_entrega='agendada', status='pago', data_entrega=hoje() + timedelta(days=1))
    db.session.add(pedido)
    db.session.flush()
    return pedido


def _item(pedido, nome, quantidade, sob_encomenda=True):
    receita = Receita(nome=nome, categoria='Minis', rendimento_qtd=1,
                      rendimento_unidade='un', peso_base=50,
                      estado_padrao='assado', sob_encomenda=sob_encomenda)
    db.session.add(receita)
    db.session.flush()
    item = PedidoOnlineItem(pedido_id=pedido.id, kind='receita', receita_id=receita.id,
                            nome=nome, preco_unitario=5, quantidade=quantidade,
                            subtotal=5 * quantidade)
    db.session.add_all([item, EstoqueProducao(receita_id=receita.id, quantidade=50)])
    db.session.flush()
    return item


def _preparar(app, usuario):
    cliente = app.test_client()
    with cliente.session_transaction() as sessao:
        sessao['_user_id'] = str(usuario.id)
        sessao['_fresh'] = True
    resposta = cliente.get('/padeiro/preparar.json', query_string={'data': hoje().isoformat()})
    assert resposta.status_code == 200
    return resposta.get_json()


def test_saida_remove_pedido_ainda_pago_da_fila_e_pre_preparo(app, admin_user):
    from app.blueprints.padeiro.routes import _dados_listas
    from app.services.saida_producao_site import registrar

    pedido = _pedido()
    _item(pedido, 'Mini já despachado', 12)
    db.session.commit()
    assert any(c['tipo'] == 'online' for c in _dados_listas(hoje(), True)['a_separar'])
    assert _preparar(app, admin_user)['itens']

    registrar(pedido, 'inicio_rota', admin_user.id)
    db.session.commit()

    # Iniciar rota não precisa mudar o status comercial para a fila sumir.
    assert pedido.status == 'pago'
    assert not [c for c in _dados_listas(hoje(), True)['a_separar'] if c['tipo'] == 'online']
    assert _preparar(app, admin_user)['itens'] == []


def test_filtro_por_item_preserva_encomenda_pendente_do_mesmo_pedido(app, admin_user):
    from app.blueprints.padeiro.routes import _card_online, _dados_listas

    pedido = _pedido()
    saiu = _item(pedido, 'Mini que saiu', 12)
    _item(pedido, 'Mini pendente', 8)
    _item(pedido, 'Item da prateleira', 3, sob_encomenda=False)
    db.session.add(SaidaProducaoSite(
        pedido_id=pedido.id, pedido_item_id=saiu.id, origem='inicio_rota',
        componentes_json='[]'))
    db.session.commit()

    cards = [c for c in _dados_listas(hoje(), True)['a_separar'] if c['tipo'] == 'online']
    assert len(cards) == 1 and cards[0]['id'] == pedido.id
    assert [(i['nome'], i['qtd']) for i in cards[0]['itens']] == [('Mini pendente', 8)]
    assert _card_online(pedido)['itens'] == cards[0]['itens']
    preparo = _preparar(app, admin_user)
    assert [(i['nome'], i['qtd']) for i in preparo['itens']] == [('Mini pendente', 8)]
    assert [(i['nome'], i['qtd']) for i in preparo['totais']] == [('Mini pendente', 8)]
