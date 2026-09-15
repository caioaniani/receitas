"""Adicionais do cliente: mesmos produtos reais e preços em cada entrega."""
import json
import re
from datetime import timedelta
from decimal import Decimal

import pytest
from test_kits_checkout import BASE, TOKEN, _abrir, _agenda, _form, _post, _vazio
from test_kits_checkout import ambiente as ambiente
from test_kits_checkout import frete as frete
from test_kits_checkout import kit as kit
from test_menu_configuravel import _menu, _pis
from werkzeug.datastructures import MultiDict

from app.extensions import db
from app.models import CompraKit, EstoqueLoja, EstoqueSitePlano, PedidoOnline, Produto
from app.services import compra_kits, kits_adicionais, kits_cafe, loja_catalogo, tiny_nf

pytestmark = pytest.mark.loja_host


@pytest.fixture(autouse=True)
def calendario(congela_hoje):
    congela_hoje(2026, 9, 14, 6)


@pytest.fixture
def extra(app, loja):
    produto = Produto(nome='Cookie adicional', categoria='Cookies', ativo=True,
                      site_ativo=True, preco_site=Decimal('7.35'))
    db.session.add(produto)
    db.session.flush()
    db.session.add(EstoqueLoja(loja_id=loja.id, produto_id=produto.id,
                               quantidade=40, quantidade_reservada=0))
    db.session.commit()
    return produto


def _raw(produto, qtd=2, **campos):
    return {'kind': 'produto', 'id': produto.id, 'qtd': qtd, **campos}


def _comprar(kit, adicionais, *, agenda=None, form=None):
    dados = _form(adicionais_json=json.dumps(adicionais))
    dados.update(form or {})
    return compra_kits.criar_compra(
        kit, dados, _agenda() if agenda is None else agenda,
        checkout_token=TOKEN, base=BASE)


def _recusado(kit, adicionais, frete):
    compra, erros = _comprar(kit, adicionais)
    assert compra is None and erros
    _vazio()
    frete.assert_not_called()


def test_tres_entregas_somam_adicional_e_frete_sem_alterar_kit(kit, extra, frete, monkeypatch):
    adicionais = [_raw(extra, preco='0.01', subtotal='0.02')]
    compra, erros = _comprar(kit, adicionais, agenda=_agenda(1, 8, 15),
                             form={'valor_total': '0.01', 'frete_valor': '0'})
    assert not erros and compra is not None
    assert compra.subtotal == Decimal('141.00')
    assert compra.frete_total == Decimal('45.75')
    assert compra.valor_total == Decimal('186.75')
    assert kits_cafe.preco_kit(kit) == Decimal('32.30')
    assert len(kit.itens) == 2
    monkeypatch.setattr(tiny_nf, 'sku_do_item', lambda kind, item_id: f'{kind}-{item_id}')
    for entrega in compra.entregas:
        pedido = entrega.pedido
        assert pedido.subtotal == Decimal('47.00')
        assert pedido.frete_valor == Decimal('15.25')
        item = next(it for it in pedido.itens if it.produto_id == extra.id)
        assert item.quantidade == 2 and item.preco_unitario == Decimal('7.35')
        assert item.subtotal == Decimal('14.70')
        itens_nf, faltantes = tiny_nf._payload_itens(pedido)
        assert not faltantes
        linha = next(it['item'] for it in itens_nf
                     if it['item']['codigo'] == f'produto-{extra.id}')
        assert linha['quantidade'] == 2 and linha['valor_unitario'] == 7.35
    frete.assert_called_once()
    repetida, erros = _comprar(kit, adicionais, agenda=_agenda(1, 8, 15))
    assert not erros and repetida.id == compra.id
    assert CompraKit.query.count() == 1 and PedidoOnline.query.count() == 3
    frete.assert_called_once()


def test_ausencia_do_campo_continua_comprando_apenas_itens_do_owner(kit, frete):
    assert kits_adicionais.ler(_form()) == ([], [])
    compra, erros = compra_kits.criar_compra(
        kit, _form(), _agenda(), checkout_token=TOKEN, base=BASE)
    assert not erros and compra.valor_total == Decimal('95.10')


@pytest.mark.parametrize('campo,valor', [
    ('id', True), ('id', False), ('id', 1.5), ('id', -1), ('id', 0), ('id', '1'),
    ('qtd', True), ('qtd', False), ('qtd', 1.5), ('qtd', -1), ('qtd', 0),
    ('qtd', 1000), ('qtd', '2'), ('kind', 'mp'), ('kind', None),
])
def test_identidade_e_quantidade_adulteradas_nao_geram_compra(kit, extra, frete, campo, valor):
    raw = _raw(extra)
    raw[campo] = valor
    _recusado(kit, [raw], frete)


@pytest.mark.parametrize('valor', ['{quebrado', 'null', '{}', '1', '[null]', '[1]', '[[]]'])
def test_payload_malformado_nao_e_ignorado(kit, frete, valor):
    compra, erros = compra_kits.criar_compra(
        kit, _form(adicionais_json=valor), _agenda(), checkout_token=TOKEN, base=BASE)
    assert compra is None and erros
    _vazio()
    frete.assert_not_called()


def test_campo_repetido_no_post_e_recusado(kit, extra, frete):
    dados = MultiDict(_form(adicionais_json=json.dumps([_raw(extra)])))
    dados.add('adicionais_json', '[]')
    compra, erros = compra_kits.criar_compra(
        kit, dados, _agenda(), checkout_token=TOKEN, base=BASE)
    assert compra is None and erros
    _vazio()
    frete.assert_not_called()


def test_adicional_repetido_e_recusado(kit, extra, frete):
    _recusado(kit, [_raw(extra), _raw(extra, 1)], frete)


def test_mais_de_cinquenta_linhas_e_recusado_antes_de_validar_catalogo(kit, frete):
    _recusado(kit, [{'kind': 'produto', 'id': item_id, 'qtd': 1}
                    for item_id in range(1, 52)], frete)


@pytest.mark.parametrize('campo,valor', [('site_ativo', False), ('ativo', False),
                                        ('preco_site', None), ('preco_site', 0)])
def test_produto_fora_da_vitrine_nao_entra_como_adicional(kit, extra, frete, campo, valor):
    setattr(extra, campo, valor)
    db.session.commit()
    _recusado(kit, [_raw(extra)], frete)
    assert ('produto', extra.id) not in {
        (oferta['kind'], oferta['id']) for oferta in kits_adicionais.catalogo(base=BASE)}


def test_extra_do_mesmo_item_fixo_nao_contorna_capacidade_da_data(kit, frete):
    receita_id = kit.itens[0].receita_id
    db.session.add(EstoqueSitePlano(
        kind='receita', item_id=receita_id, data=BASE.date() + timedelta(days=8),
        qtd_planejada=3, qtd_reservada=0))
    db.session.commit()
    compra, erros = _comprar(kit, [{'kind': 'receita', 'id': receita_id, 'qtd': 2}])
    assert compra is None and erros
    _vazio()
    assert EstoqueSitePlano.query.one().qtd_reservada == 0


def test_adicional_vendavel_so_no_dia_vinte_aparece_e_pode_ser_comprado(kit, extra, frete):
    for indice in range(35):
        db.session.add(EstoqueSitePlano(
            kind='produto', item_id=extra.id, data=BASE.date() + timedelta(days=indice),
            qtd_planejada=5 if indice == 20 else 0, qtd_reservada=0))
    db.session.commit()
    assert not loja_catalogo.tem_estoque_site('produto', extra.id)
    assert ('produto', extra.id) in {
        (oferta['kind'], oferta['id']) for oferta in kits_adicionais.catalogo(base=BASE)}
    compra, erros = _comprar(kit, [_raw(extra)], agenda=_agenda(20))
    assert not erros and compra.valor_total == Decimal('62.25')
    reserva = EstoqueSitePlano.query.filter_by(
        kind='produto', item_id=extra.id, data=BASE.date() + timedelta(days=20)).one()
    assert reserva.qtd_reservada == 2


def test_data_sem_capacidade_para_extra_desfaz_todas_as_entregas(kit, extra, frete):
    db.session.add(EstoqueSitePlano(
        kind='produto', item_id=extra.id, data=BASE.date() + timedelta(days=8),
        qtd_planejada=1, qtd_reservada=0))
    db.session.commit()
    compra, erros = _comprar(kit, [_raw(extra)])
    assert compra is None and erros
    _vazio()
    assert EstoqueSitePlano.query.one().qtd_reservada == 0


def test_adicional_sob_encomenda_exige_antecedencia_do_pedido_inteiro(kit, extra, frete):
    extra.sob_encomenda = True
    db.session.commit()
    compra, erros = _comprar(kit, [_raw(extra)], agenda=_agenda(1, 8))
    assert compra is None and erros
    _vazio()
    compra, erros = _comprar(kit, [_raw(extra)], agenda=_agenda(2, 8))
    assert not erros and len(compra.entregas) == 2


def test_preco_do_extra_muda_na_cotacao_e_compra_inteira_e_desfeita(kit, extra, frete):
    retorno = frete.return_value.copy()

    def cotar(*args, **kwargs):
        extra.preco_site = Decimal('99.00')
        db.session.flush()
        return retorno

    frete.side_effect = cotar
    compra, erros = _comprar(kit, [_raw(extra)])
    assert compra is None and any('mudou durante a compra' in erro for erro in erros)
    _vazio()


@pytest.fixture
def menu(app):
    produto, _ = _menu(db)
    produto.site_ativo = True
    db.session.commit()
    return produto


def test_menu_adicional_persiste_composicao_explicita_e_preco_por_mini(kit, menu, frete):
    a, b, c = _pis(menu)
    compra, erros = _comprar(kit, [_raw(menu, 2, comp={str(a): 10, str(b): 3, str(c): 2})])
    assert not erros and compra is not None
    assert compra.subtotal == Decimal('212.60')
    assert compra.valor_total == Decimal('243.10')
    for entrega in compra.entregas:
        item = next(it for it in entrega.pedido.itens if it.produto_id == menu.id)
        assert item.quantidade == 2 and item.preco_unitario == Decimal('37.00')
        assert {componente.produto_item_id: componente.quantidade
                for componente in item.componentes} == {a: 10, b: 3, c: 2}
    menu.itens[0].quantidade = 0
    menu.itens[0].preco_menu = Decimal('10.00')
    db.session.commit()
    assert compra.valor_total == Decimal('243.10')
    assert all(next(it for it in entrega.pedido.itens if it.produto_id == menu.id)
               .preco_unitario == Decimal('37.00') for entrega in compra.entregas)


@pytest.mark.parametrize('modo', ['ausente', 'vazia', 'slot-estranho', 'fracionaria', 'acima-teto'])
def test_menu_nao_adota_padrao_ou_corrige_composicao_adulterada(kit, menu, frete, modo):
    a, b, c = _pis(menu)
    raw = _raw(menu)
    if modo == 'vazia':
        raw['comp'] = {}
    elif modo == 'slot-estranho':
        raw['comp'] = {str(a): 5, str(b): 5, str(c): 5, '999999': 1}
    elif modo == 'fracionaria':
        raw['comp'] = {str(a): 5.9, str(b): 5, str(c): 5}
    elif modo == 'acima-teto':
        raw['comp'] = {str(a): 99, str(b): 3, str(c): 2}
    _recusado(kit, [raw], frete)


def test_produto_comum_nao_aceita_composicao_de_menu(kit, extra, frete):
    _recusado(kit, [_raw(extra, comp={'1': 15})], frete)


def test_erro_de_dados_preserva_adicionais_no_formulario(app, kit, extra, frete):
    cliente, token, _ = _abrir(app, kit)
    adicionais = [_raw(extra)]
    resposta = _post(cliente, kit, token, cpf='invalido',
                     adicionais_json=json.dumps(adicionais))
    assert resposta.status_code == 400
    _vazio()

    config = re.search(r'<script id="kits-config"[^>]*>(.*?)</script>',
                       resposta.get_data(as_text=True), re.S)
    assert config is not None
    preservados = json.loads(config[1])['adicionaisSelecionados']
    assert [(it['kind'], it['id'], it['qtd']) for it in preservados] == [
        ('produto', extra.id, 2)]


def _config_resposta(resposta):
    config = re.search(r'<script id="kits-config"[^>]*>(.*?)</script>',
                       resposta.get_data(as_text=True), re.S)
    assert config is not None
    return json.loads(config[1])


def test_erro_preserva_composicao_e_preco_do_menu_apos_owner_mudar_padrao(app, kit, menu, frete):
    cliente, token, html = _abrir(app, kit)
    inicial = json.loads(re.search(r'<script id="kits-config"[^>]*>(.*?)</script>',
                                   html, re.S)[1])
    oferta_inicial = next(it for it in inicial['adicionais']
                          if it['kind'] == 'produto' and it['id'] == menu.id)
    comp_anterior = oferta_inicial['comp']
    adicionais = [_raw(menu, 2, comp=comp_anterior)]
    menu.itens[0].quantidade, menu.itens[1].quantidade, menu.itens[2].quantidade = 10, 5, 0
    db.session.commit()
    oferta_nova = next(it for it in kits_adicionais.catalogo(base=BASE)
                       if it['kind'] == 'produto' and it['id'] == menu.id)
    assert oferta_nova['precoCentavos'] == 3500

    resposta = _post(cliente, kit, token, cpf='invalido',
                     adicionais_json=json.dumps(adicionais))
    assert resposta.status_code == 400
    _vazio()
    config = _config_resposta(resposta)
    assert config['adicionaisSelecionados'] == adicionais
    oferta_recuperada = next(it for it in config['adicionais']
                             if it['kind'] == 'produto' and it['id'] == menu.id)
    assert oferta_recuperada['comp'] == comp_anterior
    assert oferta_recuperada['precoCentavos'] == 4500
    assert oferta_recuperada['descricao'] == oferta_inicial['descricao']
    assert oferta_recuperada['descricao'] != oferta_nova['descricao']
    concluida = _post(cliente, kit, token, adicionais_json=json.dumps(adicionais))
    assert concluida.status_code == 302
    compra = CompraKit.query.one()
    assert compra.valor_total == Decimal('275.10')
    for entrega in compra.entregas:
        item = next(it for it in entrega.pedido.itens if it.produto_id == menu.id)
        assert item.quantidade == 2 and item.preco_unitario == Decimal('45.00')


def test_um_adicional_despublicado_preserva_todos_para_cliente_remover(app, kit, extra, frete):
    outro = Produto(nome='Granola adicional', categoria='Granolas', ativo=True,
                     site_ativo=True, preco_site=Decimal('12.00'))
    db.session.add(outro)
    db.session.commit()
    cliente, token, _ = _abrir(app, kit)
    adicionais = [_raw(extra), _raw(outro, 1)]
    extra.site_ativo = False
    db.session.commit()

    resposta = _post(cliente, kit, token, adicionais_json=json.dumps(adicionais))
    assert resposta.status_code == 400
    _vazio()
    frete.assert_not_called()
    config = _config_resposta(resposta)
    assert config['adicionaisSelecionados'] == adicionais
    ofertas = {(it['kind'], it['id']) for it in config['adicionais']}
    assert ('produto', extra.id) not in ofertas
    assert ('produto', outro.id) in ofertas
    assert 'id="kit-adicionais-lista"' in resposta.get_data(as_text=True)

    concluida = _post(cliente, kit, token, adicionais_json=json.dumps([_raw(outro, 1)]))
    assert concluida.status_code == 302
    compra = CompraKit.query.one()
    assert compra.valor_total == Decimal('119.10')
    assert all(any(item.produto_id == outro.id for item in entrega.pedido.itens)
               for entrega in compra.entregas)
