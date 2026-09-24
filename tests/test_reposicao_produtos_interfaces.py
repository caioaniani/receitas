"""Produto estocado diretamente usa sua identidade em toda a interface."""
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


def _produto(loja, nome='Iogurte pronto'):
    produto = Produto(nome=nome, ativo=True)
    db.session.add(produto)
    db.session.flush()
    estoque = EstoqueLoja(loja_id=loja.id, produto_id=produto.id,
                          quantidade=0, pedido_minimo_diario=2)
    db.session.add(estoque)
    db.session.commit()
    return produto, estoque


def test_grid_e_api_expoem_produto_sem_confundir_com_receita(app, admin_user, loja):
    produto, _ = _produto(loja)
    receita = Receita(nome='Pão controle', rendimento_qtd=1,
                      rendimento_unidade='un', peso_base=100)
    db.session.add(receita)
    db.session.flush()
    db.session.add(EstoqueLoja(loja_id=loja.id, receita_id=receita.id,
                              quantidade=0, pedido_minimo_diario=3))
    db.session.commit()
    assert receita.id == produto.id  # tabelas distintas, identidades não se misturam
    client = app.test_client()
    _login(client, admin_user)
    response = client.get('/producao/pedidos-semana/estoque?horizonte=2&comparar=1')
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    amanha = (hoje() + timedelta(days=1)).isoformat()
    assert f'name="qtd|{loja.id}|{amanha}|prod:{produto.id}"' in html
    assert f'name="qtd|{loja.id}|{amanha}|{receita.id}"' in html
    assert '>produto</span>' in html

    app.config['CLAUDE_API_TOKEN'] = 'teste-produtos-api'
    response = client.get('/api/claude/pedidos-semana?modo=venda&horizonte=2',
                          headers={'Authorization': 'Bearer teste-produtos-api'})
    assert response.status_code == 200
    itens = response.get_json()['lojas'][0]['produtos']
    item = next(p for p in itens if p.get('produto_id') == produto.id)
    assert item['item_key'] == f'prod:{produto.id}'
    assert item['eh_produto'] is True
    assert item['receita_id'] is None and item['materia_prima_id'] is None
    assert item['por_dia'] == [2, 2]


def test_post_cria_atualiza_e_remove_produto_sem_tocar_receita(
        app, admin_user, loja, pedidos_antes_do_corte):
    produto, _ = _produto(loja)
    receita = Receita(nome='Pão controle', rendimento_qtd=1,
                      rendimento_unidade='un', peso_base=100)
    db.session.add(receita)
    db.session.commit()
    dia = (hoje() + timedelta(days=1)).isoformat()
    client = app.test_client()
    _login(client, admin_user)
    for qtd in (4, 7, 0):
        response = client.post('/producao/pedidos-semana/gerar', data={
            'origem': 'estoque', 'so_loja': str(loja.id),
            f'qtd|{loja.id}|{dia}|prod:{produto.id}': str(qtd),
            f'qtd|{loja.id}|{dia}|{receita.id}': '5',
        })
        assert response.status_code == 302
        pedido = PedidoLoja.query.one()
        item_receita = PedidoItem.query.filter_by(pedido_id=pedido.id,
                                                  receita_id=receita.id).one()
        assert item_receita.quantidade == 5
        item_produto = PedidoItem.query.filter_by(pedido_id=pedido.id,
                                                  produto_id=produto.id).first()
        if qtd:
            assert item_produto.quantidade == qtd
            assert item_produto.receita_id is None
            assert item_produto.materia_prima_id is None
        else:
            assert item_produto is None


def test_cesta_explicada_na_tela_e_regras_antigas_preservadas(app, admin_user, loja):
    simples, saldo_simples = _produto(loja)
    cesta, saldo_cesta = _produto(loja, 'Cesta de iogurte')
    db.session.add(ProdutoItem(produto_id=cesta.id, tipo='produto',
                               produto_componente_id=simples.id,
                               item_nome=simples.nome, quantidade=2))
    saldo_cesta.estoque_minimo = 8
    saldo_cesta.reposicao_por_venda_diaria = True
    db.session.commit()
    client = app.test_client()
    _login(client, admin_user)
    html = client.get(f'/pedidos/estoque-loja?loja={loja.id}').get_data(as_text=True)
    assert 'Cesta montada com componentes: configure a reposição de cada componente.' in html
    # O form conserva a posição da cesta, mas o backend rejeita novas regras
    # sem efeito nela e ainda salva normalmente a linha seguinte.
    response = client.post('/pedidos/estoque-loja/minimos', data=MultiDict([
        ('loja_id', str(loja.id)),
        ('estoque_id[]', str(saldo_cesta.id)), ('estoque_id[]', str(saldo_simples.id)),
        ('minimo[]', '99'), ('minimo[]', '6'),
        ('diario[]', '99'), ('diario[]', '3'),
        ('venda_diaria[]', str(saldo_simples.id)),
    ]))
    assert response.status_code == 302
    db.session.refresh(saldo_cesta)
    db.session.refresh(saldo_simples)
    assert (saldo_cesta.estoque_minimo, saldo_cesta.pedido_minimo_diario) == (8, 2)
    assert saldo_cesta.reposicao_por_venda_diaria is True
    assert (saldo_simples.estoque_minimo, saldo_simples.pedido_minimo_diario) == (6, 3)
    assert saldo_simples.reposicao_por_venda_diaria is True


def test_produto_inativo_nao_aceita_nova_regra_sem_efeito(app, admin_user, loja):
    produto, saldo = _produto(loja)
    produto.ativo = False
    db.session.commit()
    client = app.test_client()
    _login(client, admin_user)
    html = client.get(f'/pedidos/estoque-loja?loja={loja.id}').get_data(as_text=True)
    assert 'Produto inativo: ative o cadastro para configurar a reposição.' in html
    response = client.post('/pedidos/estoque-loja/minimos', data={
        'loja_id': str(loja.id), 'estoque_id[]': str(saldo.id),
        'minimo[]': '20', 'diario[]': '20', 'venda_diaria[]': str(saldo.id),
    })
    assert response.status_code == 302
    db.session.refresh(saldo)
    assert saldo.pedido_minimo_diario == 2
    assert saldo.estoque_minimo is None
    assert saldo.reposicao_por_venda_diaria is False


def test_ia_conserva_token_produto_sanitiza_e_respeita_pedido_existente(
        app, monkeypatch, loja):
    from app.services import planejamento_ia

    produto, _ = _produto(loja)
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
            {'item_key': chave, 'por_dia': [99, -4, 'inválido'], 'motivo': 'teste'},
            {'item_key': 'prod:99999', 'por_dia': [50, 50, 50]},
        ], 'parecer': ''}, None

    monkeypatch.setattr(planejamento_ia, '_chamar_opus', fake_ia)
    saida = planejamento_ia.sugerir_pedido_loja_ia(
        loja.id, modo='venda', horizonte_dias=3, inicio_offset_dias=1)
    assert 'erro' not in saida
    assert len(saida['itens']) == 1
    assert saida['itens'][0]['item_key'] == chave
    assert saida['itens'][0]['por_dia'] == [7, 0, 2]
    contexto = next(p for p in capturado['produtos'] if p['item_key'] == chave)
    assert contexto['por_dia_media'] is None
