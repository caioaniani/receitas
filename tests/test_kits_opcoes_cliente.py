"""Planos de café: uma opção autorizada de croissant/sourdough por entrega."""
from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import delete, update
from test_kits_sucos import BASE, _comprar, _form, _sem_compra
from test_kits_sucos import cenario as cenario
from werkzeug.datastructures import MultiDict

from app.extensions import db
from app.models import EstoqueLoja, EstoqueSitePlano, KitCafeOpcao, Produto, Receita
from app.services import kits_cafe, loja_catalogo


def _chave(item):
    return f'{"receita" if isinstance(item, Receita) else "produto"}:{item.id}'


@pytest.fixture
def plano(cenario, loja):
    kit, laranja, verde, outro, frete = cenario
    almond = Receita(nome='Croissant almond', categoria='Croissants', rendimento_qtd=1,
                     rendimento_unidade='un', peso_base=100, site_ativo=True, preco_site=18)
    nutella = Produto(nome='Croissant Nutella com morango', ativo=True,
                      site_ativo=True, preco_site=22)
    tradicional = Receita(nome='Sourdough tradicional', categoria='Paes', rendimento_qtd=1,
                           rendimento_unidade='un', peso_base=500, site_ativo=True, preco_site=28)
    integral = Produto(nome='Sourdough integral', ativo=True, site_ativo=True, preco_site=30)
    db.session.add_all([almond, nutella, tradicional, integral])
    db.session.flush()
    for grupo, itens in [('croissant', [nutella, almond]), ('sourdough', [integral, tradicional])]:
        for item in itens:
            kind = _chave(item).split(':')[0]
            alvo = {f'{kind}_id': item.id}
            kit.opcoes.append(KitCafeOpcao(grupo=grupo, kind=kind, **alvo))
            db.session.add(EstoqueLoja(loja_id=loja.id, quantidade=20,
                                       quantidade_reservada=0, **alvo))
            for dia in [1, 8]:
                db.session.add(EstoqueSitePlano(
                    kind=kind, item_id=item.id, data=BASE.date() + timedelta(days=dia),
                    qtd_planejada=10, qtd_reservada=0))
    db.session.commit()
    return kit, laranja, verde, almond, nutella, tradicional, integral, frete


def _escolhas(plano):
    return {'croissant': _chave(plano[4]), 'sourdough': _chave(plano[6])}


def _dados(plano):
    return _form(suco_id=str(plano[2].id),
                 **{f'escolha_{grupo}': chave for grupo, chave in _escolhas(plano).items()})


def test_grupos_ordenados_com_catalogos_e_precos_reais(plano):
    kit, _, _, almond, nutella, tradicional, integral, _ = plano
    grupos = kits_cafe.opcoes_grupos(kit)
    assert [grupo['chave'] for grupo in grupos] == ['croissant', 'sourdough']
    assert [grupo['nome'] for grupo in grupos] == ['Croissant', 'Sourdough']
    assert [opcao['chave'] for opcao in grupos[0]['opcoes']] == [_chave(almond), _chave(nutella)]
    assert [opcao['chave'] for opcao in grupos[1]['opcoes']] == [_chave(tradicional), _chave(integral)]
    assert [opcao['preco'] for opcao in grupos[0]['opcoes']] == [Decimal('18'), Decimal('22')]
    assert grupos[0]['opcoes'][0]['kind'] == 'receita'
    assert grupos[0]['opcoes'][1]['kind'] == 'produto'


def test_preview_usa_menor_preco_mas_compra_precisa_escolher(plano):
    kit, _, _, almond, _, tradicional, _, _ = plano
    assert kits_cafe.preco_kit(kit) == Decimal('113.80')
    assert kits_cafe.itens_do_kit(kit)[-2:] == [
        {'kind': 'receita', 'id': almond.id, 'qtd': 1},
        {'kind': 'receita', 'id': tradicional.id, 'qtd': 1},
    ]
    with pytest.raises(ValueError, match='cada grupo'):
        kits_cafe.itens_do_kit(kit, escolhas={})


@pytest.mark.parametrize('escolhas', [
    {}, {'croissant': 'receita:1'}, {'croissant': 'receita:1', 'sobremesa': 'produto:1'},
    {'croissant': 'receita:9999999999', 'sourdough': 'produto:1'},
    {'croissant': True, 'sourdough': False}, [], 'croissant',
])
def test_service_recusa_escolhas_incompletas_ou_estranhas(plano, escolhas):
    itens, erros = kits_cafe.montar(plano[0], plano[2].id, escolhas)
    assert not itens and erros


@pytest.mark.parametrize('modo', ['ausente', 'vazio', 'duplicado', 'outro-grupo', 'fora-kit',
                                  'zero-esquerda', 'kind-invalido', 'grupo-extra'])
def test_post_invalido_recusa_antes_de_frete_e_pedidos(plano, modo):
    kit, _, _, _, _, _, integral, frete = plano
    dados = MultiDict(_dados(plano))
    if modo == 'ausente':
        dados.pop('escolha_croissant')
    elif modo == 'duplicado':
        dados.add('escolha_croissant', dados['escolha_croissant'])
    elif modo == 'outro-grupo':
        dados['escolha_croissant'] = _chave(integral)
    elif modo == 'fora-kit':
        dados['escolha_croissant'] = 'produto:9999999999'
    elif modo == 'zero-esquerda':
        dados['escolha_croissant'] = f'produto:0{plano[4].id}'
    elif modo == 'kind-invalido':
        dados['escolha_croissant'] = f'mp:{plano[4].id}'
    elif modo == 'grupo-extra':
        dados['escolha_granola'] = 'produto:1'
    else:
        dados['escolha_croissant'] = ''
    compra, erros = _comprar(kit, dados)
    assert compra is None and erros
    _sem_compra(frete)


def test_escolhas_geram_mesmos_itens_precos_e_reservas_em_todas_entregas(plano):
    kit, _, verde, _, nutella, _, integral, frete = plano
    dados = _dados(plano)
    dados.update(preco='0.01', qtd_croissant='99', qtd_sourdough='99')
    compra, erros = _comprar(kit, dados)
    assert not erros and compra is not None
    assert compra.subtotal == Decimal('243.60')
    assert compra.valor_total == Decimal('274.10')
    for entrega in compra.entregas:
        assert len(entrega.pedido.itens) == 4
        produtos = {item.produto_id: item for item in entrega.pedido.itens if item.kind == 'produto'}
        assert set(produtos) == {verde.id, nutella.id, integral.id}
        assert all(item.quantidade == 1 for item in produtos.values())
        assert produtos[nutella.id].preco_unitario == Decimal('22.00')
        assert produtos[integral.id].preco_unitario == Decimal('30.00')
        assert entrega.reserva_plano
    for item in [nutella, integral]:
        assert {plano.qtd_reservada for plano in EstoqueSitePlano.query.filter_by(
            kind='produto', item_id=item.id)} == {1}
    assert all(estoque.quantidade == 20 and estoque.quantidade_reservada == 0
               for estoque in EstoqueLoja.query.all())
    frete.assert_called_once()
    repetida, erros = _comprar(kit, dados)
    assert not erros and repetida.id == compra.id
    frete.assert_called_once()


def test_receitas_escolhidas_vao_ao_pedido_com_uma_unidade(plano):
    kit, _, _, almond, _, tradicional, _, _ = plano
    dados = _dados(plano)
    dados.update(escolha_croissant=_chave(almond), escolha_sourdough=_chave(tradicional))
    compra, erros = _comprar(kit, dados)
    assert not erros and compra
    for entrega in compra.entregas:
        escolhidos = {item.receita_id: item for item in entrega.pedido.itens
                      if item.receita_id in {almond.id, tradicional.id}}
        assert set(escolhidos) == {almond.id, tradicional.id}
        assert all(item.quantidade == 1 for item in escolhidos.values())


def test_editar_catalogo_depois_nao_reescreve_compra(plano):
    kit, _, _, _, nutella, _, integral, _ = plano
    compra, erros = _comprar(kit, _dados(plano))
    assert not erros
    kit.opcoes.clear()
    nutella.nome, nutella.preco_site = 'Outra receita', 99
    integral.site_ativo = False
    db.session.commit()
    db.session.expire_all()
    for entrega in compra.entregas:
        item = next(item for item in entrega.pedido.itens if item.produto_id == nutella.id)
        assert item.nome == 'Croissant Nutella com morango'
        assert item.preco_unitario == Decimal('22.00')
    assert compra.valor_total == Decimal('274.10')


def test_catalogo_muda_durante_frete_recusa_compra_inteira(plano):
    kit, _, _, _, nutella, _, _, frete = plano
    retorno = frete.return_value.copy()

    def cotar(*args, **kwargs):
        nutella.preco_site = 99
        db.session.flush()
        return retorno

    frete.side_effect = cotar
    compra, erros = _comprar(kit, _dados(plano))
    assert compra is None
    assert any('mudou durante a compra' in erro for erro in erros)


def test_grupos_carregados_antes_do_lock_sao_relidos(plano):
    kit, _, _, _, nutella, _, _, frete = plano
    assert len(kit.opcoes) == 4
    db.session.execute(delete(KitCafeOpcao).where(KitCafeOpcao.produto_id == nutella.id),
                       execution_options={'synchronize_session': False})
    assert len(kit.opcoes) == 4
    compra, erros = _comprar(kit, _dados(plano))
    assert compra is None and erros
    assert len(kit.opcoes) == 3
    _sem_compra(frete)


def test_opcao_alterada_antes_do_lock_nao_usa_objeto_antigo(plano, cenario):
    kit, _, _, _, nutella, _, _, frete = plano
    antiga = next(opcao for opcao in kit.opcoes if opcao.produto_id == nutella.id)
    db.session.execute(update(KitCafeOpcao).where(KitCafeOpcao.id == antiga.id)
                       .values(produto_id=cenario[3].id),
                       execution_options={'synchronize_session': False})
    assert antiga.produto_id == nutella.id
    compra, erros = _comprar(kit, _dados(plano))
    assert compra is None and erros
    _sem_compra(frete)


@pytest.mark.parametrize('modo', ['fixo', 'suco', 'grupo', 'repetido'])
def test_owner_recusa_opcao_repetida(plano, modo):
    kit, laranja, _, almond, nutella, tradicional, integral, _ = plano
    selecao = {'croissant': [_chave(almond), _chave(nutella)],
               'sourdough': [_chave(tradicional), _chave(integral)]}
    if modo == 'fixo':
        selecao['croissant'][0] = f'receita:{kit.itens[0].receita_id}'
    elif modo == 'suco':
        selecao['croissant'][0] = _chave(laranja)
    elif modo == 'grupo':
        selecao['sourdough'][0] = _chave(almond)
    else:
        selecao['croissant'][0] = _chave(nutella)
    grupos, erros = kits_cafe.preparar_opcoes(
        selecao, kits_cafe.itens_fixos_do_kit(kit), [suco.produto_id for suco in kit.sucos])
    assert not grupos and any('repetir' in erro for erro in erros)


@pytest.mark.parametrize('selecao', [
    [], {'granola': []}, {'croissant': 'receita:1'}, {'croissant': ['receita:1']},
    {'croissant': ['receita:1'] * 11}, {'croissant': ['receita:01', 'produto:1']},
    {'croissant': ['receita:1', True]}, {'croissant': ['mp:1', 'produto:1']},
])
def test_owner_recusa_cadastro_de_grupo_invalido(cenario, selecao):
    grupos, erros = kits_cafe.preparar_opcoes(selecao, [])
    assert not grupos and erros


def test_owner_recusa_mais_de_64_combinacoes(cenario):
    grupos, erros = kits_cafe.preparar_opcoes(
        {'croissant': [f'receita:{item_id}' for item_id in range(100, 108)],
         'sourdough': [f'receita:{item_id}' for item_id in range(200, 208)]},
        [], [cenario[1].id, cenario[2].id])
    assert not grupos and any('64 combinações' in erro for erro in erros)


def test_owner_recusa_menu_configuravel_como_opcao(plano):
    from test_menu_configuravel import _menu

    menu, _ = _menu(db)
    grupos, erros = kits_cafe.preparar_opcoes(
        {'croissant': [_chave(plano[3]), _chave(menu)]}, [])
    assert not grupos and any('menus configuráveis' in erro for erro in erros)


def test_opcao_fora_do_catalogo_nao_vende_plano_parcial(plano):
    kit, _, _, almond, _, _, _, frete = plano
    almond.site_ativo = False
    db.session.commit()
    compra, erros = _comprar(kit, _dados(plano))
    assert compra is None and erros
    assert kits_cafe.publicados() == []
    _sem_compra(frete)


def test_estoque_da_opcao_na_data_exata_recusa_entrega(plano):
    kit, _, _, _, nutella, _, _, _ = plano
    db.session.execute(update(EstoqueSitePlano).where(
        EstoqueSitePlano.kind == 'produto', EstoqueSitePlano.item_id == nutella.id,
        EstoqueSitePlano.data == BASE.date() + timedelta(days=8)).values(qtd_planejada=0))
    db.session.commit()
    compra, erros = _comprar(kit, _dados(plano))
    assert compra is None and erros


def test_opcao_sob_encomenda_respeita_antecedencia(plano, monkeypatch):
    kit, _, _, _, nutella, _, _, _ = plano
    nutella.sob_encomenda = True
    db.session.commit()
    consultas = []
    original = loja_catalogo.tem_estoque_site

    def estoque(kind, item_id, **kwargs):
        if kind == 'produto' and item_id == nutella.id:
            consultas.append(kwargs.get('datas'))
        return original(kind, item_id, **kwargs)

    monkeypatch.setattr(loja_catalogo, 'tem_estoque_site', estoque)
    compra, erros = _comprar(kit, _dados(plano))
    assert compra is None and erros
    assert consultas and all(datas[0] == BASE.date() + timedelta(days=2) for datas in consultas)
