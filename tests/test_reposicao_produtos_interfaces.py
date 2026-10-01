"""Produto fora das interfaces de reposição (dono 01/10/2026: pedido para a
indústria só do que ela produz). SUBSTITUI o contrato anterior deste arquivo
("Produto estocado diretamente usa sua identidade em toda a interface")."""
import json
from datetime import timedelta

from werkzeug.datastructures import MultiDict

from app.extensions import db
from app.models import EstoqueLoja, PedidoItem, PedidoLoja, Produto, ProdutoItem, Receita
from app.utils import hoje


def _login(client, user):
    with client.session_transaction() as sess:
        sess['_user_id'] = str(user.id)
        sess['_fresh'] = True


def _produto(loja, nome='Água com gás'):
    produto = Produto(nome=nome, ativo=True)
    db.session.add(produto)
    db.session.flush()
    estoque = EstoqueLoja(loja_id=loja.id, produto_id=produto.id,
                          quantidade=0, pedido_minimo_diario=2)
    db.session.add(estoque)
    db.session.commit()
    return produto, estoque


def _receita(loja, nome='Pão controle', diario=3):
    receita = Receita(nome=nome, rendimento_qtd=1, rendimento_unidade='un',
                      peso_base=100)
    db.session.add(receita)
    db.session.flush()
    estoque = EstoqueLoja(loja_id=loja.id, receita_id=receita.id,
                          quantidade=0, pedido_minimo_diario=diario)
    db.session.add(estoque)
    db.session.commit()
    return receita, estoque


def test_grid_e_api_nao_expoem_produto(app, admin_user, loja):
    produto, _ = _produto(loja)
    receita, _ = _receita(loja)
    client = app.test_client()
    _login(client, admin_user)
    response = client.get('/producao/pedidos-semana/estoque?horizonte=2&comparar=1')
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    amanha = (hoje() + timedelta(days=1)).isoformat()
    assert f'|prod:{produto.id}"' not in html
    assert f'name="qtd|{loja.id}|{amanha}|{receita.id}"' in html
    assert '>produto</span>' not in html

    app.config['CLAUDE_API_TOKEN'] = 'teste-produtos-api'
    response = client.get('/api/claude/pedidos-semana?modo=venda&horizonte=2',
                          headers={'Authorization': 'Bearer teste-produtos-api'})
    assert response.status_code == 200
    itens = response.get_json()['lojas'][0]['produtos']
    assert [p for p in itens if p.get('produto_id')] == []
    assert [p['item_key'] for p in itens] == [str(receita.id)]


def test_post_da_grade_recusa_produto_de_revenda_e_nao_toca_receita(
        app, admin_user, loja, pedidos_antes_do_corte):
    produto, _ = _produto(loja)
    receita, _ = _receita(loja)
    dia = (hoje() + timedelta(days=1)).isoformat()
    client = app.test_client()
    _login(client, admin_user)
    response = client.post('/producao/pedidos-semana/gerar', data={
        'origem': 'estoque', 'so_loja': str(loja.id), 'ajax': '1',
        f'qtd|{loja.id}|{dia}|prod:{produto.id}': '4',
        f'qtd|{loja.id}|{dia}|{receita.id}': '5',
    })
    assert response.status_code == 400
    assert 'não produz nem envia' in response.get_json()['msg']
    assert PedidoLoja.query.count() == 0

    # Sem o produto forjado a grade grava normalmente.
    response = client.post('/producao/pedidos-semana/gerar', data={
        'origem': 'estoque', 'so_loja': str(loja.id),
        f'qtd|{loja.id}|{dia}|{receita.id}': '5',
    })
    assert response.status_code == 302
    pedido = PedidoLoja.query.one()
    assert [(i.receita_id, i.produto_id, i.quantidade) for i in pedido.itens] == [
        (receita.id, None, 5)]


def test_estoque_loja_explica_revenda_e_cesta_e_nao_grava_regra(app, admin_user, loja):
    revenda, saldo_revenda = _produto(loja)
    receita, saldo_receita = _receita(loja)
    cesta, saldo_cesta = _produto(loja, 'Cesta de pão')
    db.session.add(ProdutoItem(produto_id=cesta.id, tipo='receita',
                               receita_id=receita.id, item_nome=receita.nome,
                               quantidade=2))
    saldo_cesta.estoque_minimo = 8
    db.session.commit()
    client = app.test_client()
    _login(client, admin_user)
    html = client.get(f'/pedidos/estoque-loja?loja={loja.id}').get_data(as_text=True)
    assert 'Produto de revenda: a indústria não produz nem envia' in html
    assert 'Cesta montada com componentes: configure a reposição de cada componente.' in html
    # O form conserva a posição das linhas de Produto, mas o backend recusa
    # regra nova nelas e ainda salva normalmente a linha da receita.
    response = client.post('/pedidos/estoque-loja/minimos', data=MultiDict([
        ('loja_id', str(loja.id)),
        ('estoque_id[]', str(saldo_revenda.id)), ('estoque_id[]', str(saldo_cesta.id)),
        ('estoque_id[]', str(saldo_receita.id)),
        ('minimo[]', '99'), ('minimo[]', '99'), ('minimo[]', '6'),
        ('diario[]', '99'), ('diario[]', '99'), ('diario[]', '4'),
        ('venda_diaria[]', str(saldo_receita.id)),
    ]))
    assert response.status_code == 302
    for s in (saldo_revenda, saldo_cesta, saldo_receita):
        db.session.refresh(s)
    assert (saldo_revenda.estoque_minimo, saldo_revenda.pedido_minimo_diario) == (None, 2)
    assert (saldo_cesta.estoque_minimo, saldo_cesta.pedido_minimo_diario) == (8, 2)
    assert (saldo_receita.estoque_minimo, saldo_receita.pedido_minimo_diario) == (6, 4)
    assert saldo_receita.reposicao_por_venda_diaria is True


def test_produto_inativo_explicado_e_sem_regra_nova(app, admin_user, loja):
    produto, saldo = _produto(loja)
    produto.ativo = False
    db.session.commit()
    client = app.test_client()
    _login(client, admin_user)
    html = client.get(f'/pedidos/estoque-loja?loja={loja.id}').get_data(as_text=True)
    assert 'Produto inativo: a reposição não se aplica.' in html
    response = client.post('/pedidos/estoque-loja/minimos', data={
        'loja_id': str(loja.id), 'estoque_id[]': str(saldo.id),
        'minimo[]': '20', 'diario[]': '20', 'venda_diaria[]': str(saldo.id),
    })
    assert response.status_code == 302
    db.session.refresh(saldo)
    assert saldo.pedido_minimo_diario == 2
    assert saldo.estoque_minimo is None
    assert saldo.reposicao_por_venda_diaria is False


def test_ia_nao_recebe_produto_e_descarta_token_prod(app, monkeypatch, loja):
    from app.services import planejamento_ia

    produto, _ = _produto(loja)
    receita, _ = _receita(loja)
    pedido = PedidoLoja(loja_id=loja.id, data_entrega=hoje() + timedelta(days=1),
                        status='confirmado')
    db.session.add(pedido)
    db.session.flush()
    db.session.add(PedidoItem(pedido_id=pedido.id, produto_id=produto.id, quantidade=7))
    db.session.commit()
    chave = f'prod:{produto.id}'
    capturado = {}

    def fake_ia(system, payload, funcao):
        capturado.update(json.loads(payload))
        return {'itens': [
            {'item_key': chave, 'por_dia': [9, 9, 9], 'motivo': 'teste'},
            {'item_key': str(receita.id), 'por_dia': [0, 4, 4], 'motivo': 'ok'},
        ], 'parecer': ''}, None

    monkeypatch.setattr(planejamento_ia, '_chamar_opus', fake_ia)
    saida = planejamento_ia.sugerir_pedido_loja_ia(
        loja.id, modo='venda', horizonte_dias=3, inicio_offset_dias=1)
    assert 'erro' not in saida
    assert [p['item_key'] for p in capturado['produtos']] == [str(receita.id)]
    assert [it['item_key'] for it in saida['itens']] == [str(receita.id)]
