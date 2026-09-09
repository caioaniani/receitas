"""Ativar/desativar a venda no site sem apagar preço, estoque ou pedidos."""
from datetime import timedelta
from decimal import Decimal
from io import BytesIO

import pytest


def _criar_item(tipo, **campos):
    from app.extensions import db
    from app.models import Produto, Receita

    dados = dict(nome=f'Item de publicação {tipo}', categoria='Padaria',
                 preco_site=17.50, preco_loja=15.0, preco_interno=6.0,
                 ordem_site=4)
    dados.update(campos)
    if tipo == 'receita':
        obj = Receita(**dados, rendimento_qtd=1, rendimento_unidade='un',
                      peso_base=100.0, preco_venda=12.0)
    else:
        obj = Produto(**dados, preco_atacado=12.0, ativo=True)
    db.session.add(obj)
    db.session.commit()
    return obj


def _cliente(app, usuario):
    client = app.test_client()
    with client.session_transaction() as sessao:
        sessao['_user_id'] = str(usuario.id)
        sessao['_fresh'] = True
    return client


@pytest.fixture
def dono(app, owner_user):
    return _cliente(app, owner_user)


@pytest.fixture(params=['receita', 'produto'])
def item_publicacao(app, request):
    return request.param, _criar_item(request.param)


def _publicar(client, tipo, obj, ativo):
    return client.post(
        f'/admin/loja-online/catalogo/publicacao/{tipo}/{obj.id}',
        json={'ativo': ativo})


def _estado(obj):
    """Snapshot das colunas para detectar qualquer alteração operacional."""
    return {col.name: getattr(obj, col.name) for col in obj.__table__.columns}


def test_desativar_e_reativar_preserva_preco_estoque_plano_e_pedido(
        app, dono, loja, item_publicacao):
    from app.extensions import db
    from app.models import (
        EstoqueLoja,
        EstoqueSiteExcecao,
        EstoqueSitePlano,
        EstoqueSiteRegraSemanal,
        MovEstoqueLoja,
        PedidoOnline,
        PedidoOnlineItem,
    )
    from app.services import loja_catalogo
    from app.utils import hoje

    tipo, obj = item_publicacao
    assert obj.site_ativo is True  # compatibilidade dos cadastros existentes
    dia = hoje() + timedelta(days=2)
    estoque = EstoqueLoja(loja_id=loja.id, quantidade=11,
                          quantidade_reservada=2, **{f'{tipo}_id': obj.id})
    plano = EstoqueSitePlano(kind=tipo, item_id=obj.id, data=dia,
                            qtd_planejada=25, qtd_reservada=3)
    regra = EstoqueSiteRegraSemanal(kind=tipo, item_id=obj.id,
                                   dias_mask=127, qtd_limite=30)
    excecao = EstoqueSiteExcecao(kind=tipo, item_id=obj.id, data=dia,
                               qtd_limite=20)
    pedido = PedidoOnline(nome_cliente='Cliente do pedido existente',
                          email_cliente='publicacao@example.com',
                          modo_entrega='retirada', loja_retirada_id=loja.id,
                          data_entrega=dia, status='pago',
                          subtotal=Decimal('32.00'), valor_total=Decimal('32.00'))
    db.session.add_all([estoque, plano, regra, excecao, pedido])
    db.session.flush()
    item_pedido = PedidoOnlineItem(
        pedido_id=pedido.id, kind=tipo, nome='Nome no momento da compra',
        preco_unitario=Decimal('16.00'), quantidade=2,
        subtotal=Decimal('32.00'), **{f'{tipo}_id': obj.id})
    db.session.add(item_pedido)
    db.session.commit()
    operacao = [estoque, plano, regra, excecao, pedido, item_pedido]
    antes = [_estado(linha) for linha in operacao]
    cadastro_antes = _estado(obj)
    cadastro_antes.pop('site_ativo')
    movimentos_antes = MovEstoqueLoja.query.count()

    for ativo in (False, True):
        resposta = _publicar(dono, tipo, obj, ativo)
        assert resposta.status_code == 200
        dados = resposta.get_json()
        assert dados['ok'] is True
        assert dados['site_ativo'] is ativo
        assert dados['no_site'] is ativo
        assert dados['preco_site'] == 17.50
        db.session.refresh(obj)
        assert obj.site_ativo is ativo
        cadastro_depois = _estado(obj)
        cadastro_depois.pop('site_ativo')
        assert cadastro_depois == cadastro_antes
        for linha in operacao:
            db.session.refresh(linha)
        assert [_estado(linha) for linha in operacao] == antes
        assert MovEstoqueLoja.query.count() == movimentos_antes
        assert (loja_catalogo.por_id_publicado(tipo, obj.id) is not None) is ativo
        assert any(i['kind'] == tipo and i['id'] == obj.id
                   for i in loja_catalogo.produtos_publicados()) is ativo


def test_editar_preco_desativado_nao_republica(app, dono, item_publicacao):
    from app.extensions import db
    from app.services import loja_catalogo

    tipo, obj = item_publicacao
    obj.site_ativo = False
    db.session.commit()
    resposta = dono.post(f'/admin/loja-online/catalogo/preco/{tipo}/{obj.id}',
                         json={'preco': '21.75'})
    assert resposta.status_code == 200
    assert resposta.get_json()['no_site'] is False
    db.session.refresh(obj)
    assert obj.preco_site == 21.75
    assert obj.site_ativo is False
    assert loja_catalogo.por_id_publicado(tipo, obj.id) is None
    resposta = _publicar(dono, tipo, obj, True)
    assert resposta.status_code == 200
    assert resposta.get_json()['preco_site'] == 21.75
    assert resposta.get_json()['no_site'] is True


def test_desativado_bloqueia_pagina_carrinho_checkout_e_disponibilidade(
        app, dono, item_publicacao):
    from app.blueprints.loja.routes import (
        _resolver_carrinho_sessao,
        _resolver_prefill_carrinho,
        _set_carrinho_sessao,
    )
    from app.services import bot_tools, loja_catalogo, loja_checkout
    from app.utils import hoje

    tipo, obj = item_publicacao
    publicado = loja_catalogo.por_id_publicado(tipo, obj.id)
    assert publicado is not None
    assert dono.get(publicado['href']).status_code == 200
    carrinho = [{'kind': tipo, 'id': obj.id, 'qtd': 1}]
    itens, avisos = loja_checkout.montar_itens(carrinho)
    assert len(itens) == 1 and not avisos
    data = (hoje() + timedelta(days=2)).isoformat()
    parametros = {'kind': tipo, 'item_id': obj.id, 'data': data}
    assert dono.get('/loja/api/disponibilidade-dia', query_string=parametros).get_json()['disponivel'] is True

    assert _publicar(dono, tipo, obj, False).status_code == 200
    assert dono.get(publicado['href']).status_code == 404
    assert bot_tools.consultar_produtos(obj.nome)['produtos'] == []
    with app.test_request_context():
        _set_carrinho_sessao(carrinho)
        assert _resolver_carrinho_sessao() == []
        letra = 'r' if tipo == 'receita' else 'p'
        prefill, _ = _resolver_prefill_carrinho(f'{letra}{obj.id}:1')
        assert prefill == []
    itens, avisos = loja_checkout.montar_itens(carrinho)
    assert itens == []
    assert any('saiu de catálogo' in aviso for aviso in avisos)
    disponibilidade = dono.get('/loja/api/disponibilidade-dia', query_string=parametros)
    assert disponibilidade.get_json()['disponivel'] is False
    resposta = dono.post('/loja/api/disponibilidade-checkout',
                         json={'data': data, 'itens': carrinho})
    assert resposta.status_code == 200
    dados = resposta.get_json()
    assert any(i['kind'] == tipo and i['id'] == obj.id and i['nome'] == obj.nome
               for i in dados['esgotados'])
    assert dados['proxima_disponivel'] is None


def test_nao_ativa_item_sem_preco(app, dono, item_publicacao):
    from app.extensions import db

    tipo, obj = item_publicacao
    obj.site_ativo = False
    obj.preco_site = None
    db.session.commit()
    assert _publicar(dono, tipo, obj, True).status_code == 400
    db.session.refresh(obj)
    assert obj.site_ativo is False
    assert obj.preco_site is None


def test_nao_ativa_menu_sem_preco_por_componente(app, dono):
    from app.extensions import db
    from app.models import ProdutoItem
    from app.services import loja_catalogo

    mini = _criar_item('receita', preco_site=None)
    menu = _criar_item('produto', site_ativo=False, menu_configuravel=True,
                       menu_total_unidades=30)
    db.session.add(ProdutoItem(
        produto_id=menu.id, tipo='receita', receita_id=mini.id,
        item_nome=mini.nome, quantidade=30, preco_menu=None))
    db.session.commit()
    assert _publicar(dono, 'produto', menu, True).status_code == 400
    db.session.refresh(menu)
    assert menu.site_ativo is False
    assert menu.preco_site == 17.50
    assert loja_catalogo.por_id_publicado('produto', menu.id) is None


@pytest.mark.parametrize('dados', [
    {}, {'ativo': None}, {'ativo': 0}, {'ativo': 1},
    {'ativo': 'false'}, {'ativo': 'true'}, {'ativo': []}, [],
])
def test_publicacao_exige_booleano_json(app, dono, dados):
    from app.extensions import db

    obj = _criar_item('receita')
    resposta = dono.post(
        f'/admin/loja-online/catalogo/publicacao/receita/{obj.id}', json=dados)
    assert resposta.status_code == 400
    db.session.refresh(obj)
    assert obj.site_ativo is True
    assert obj.preco_site == 17.50


@pytest.mark.parametrize(('tipo', 'status'), [('invalido', 400), ('receita', 404)])
def test_publicacao_rejeita_tipo_ou_item_inexistente(app, dono, tipo, status):
    resposta = dono.post(f'/admin/loja-online/catalogo/publicacao/{tipo}/999999',
                         json={'ativo': False})
    assert resposta.status_code == status


def test_publicacao_nao_altera_cadastro_fora_de_circulacao(app, dono, item_publicacao):
    from app.extensions import db
    from app.utils import agora

    tipo, obj = item_publicacao
    if tipo == 'receita':
        obj.arquivada_em = agora()
    else:
        obj.ativo = False
    db.session.commit()
    antes = _estado(obj)
    assert _publicar(dono, tipo, obj, False).status_code == 404
    db.session.refresh(obj)
    assert _estado(obj) == antes


def test_publicacao_exige_dono(app, admin_user):
    from app.extensions import db

    obj = _criar_item('receita')
    anonimo = app.test_client()
    assert _publicar(anonimo, 'receita', obj, False).status_code in (302, 401, 403)
    admin = _cliente(app, admin_user)
    assert _publicar(admin, 'receita', obj, False).status_code == 403
    db.session.refresh(obj)
    assert obj.site_ativo is True
    assert obj.preco_site == 17.50


def test_exportar_e_importar_preserva_desativacao_com_preco(app, dono):
    """O backup JSON não pode republicar itens desativados ao restaurá-los."""
    from app.extensions import db
    from app.models import Produto, Receita
    from app.services import loja_catalogo

    # Cadastros simples, sem pedidos/estoque/composição: a importação substitui
    # o catálogo inteiro e este cenário não depende de relações operacionais.
    receita = _criar_item('receita', site_ativo=False)
    produto = _criar_item('produto', site_ativo=False)
    assert receita.to_dict()['site_ativo'] is False
    assert produto.to_dict()['site_ativo'] is False
    exportacao = dono.get('/api/exportar')
    assert exportacao.status_code == 200
    dados = exportacao.get_json()
    for colecao in ('receitas', 'produtos'):
        assert len(dados[colecao]) == 1
        assert dados[colecao][0]['site_ativo'] is False
        assert dados[colecao][0]['preco_site'] == 17.50

    # A restauração cria novas instâncias; não reutilizar objetos anteriores.
    db.session.expunge_all()
    resposta = dono.post('/api/importar', data={
        'file': (BytesIO(exportacao.data), 'catalogo-publicacao.json'),
    })
    assert resposta.status_code == 200
    assert resposta.get_json()['success'] is True
    for modelo in (Receita, Produto):
        restaurado = modelo.query.one()
        assert restaurado.site_ativo is False
        assert restaurado.preco_site == 17.50
    assert loja_catalogo.produtos_publicados() == []


def test_duplicar_receita_desativada_preserva_preco_e_desativacao(app, dono):
    from app.models import Receita
    from app.services import loja_catalogo

    original = _criar_item('receita', site_ativo=False)
    resposta = dono.post(f'/receitas/{original.id}/duplicar')
    assert resposta.status_code == 302
    copia = Receita.query.filter_by(nome=f'Cópia de {original.nome}').one()
    assert copia.id != original.id
    assert copia.preco_site == original.preco_site == 17.50
    assert copia.site_ativo is False
    assert loja_catalogo.por_id_publicado('receita', copia.id) is None
