"""Compra de kits: calendário, cobrança única e entregas persistidas juntas."""
import json
import re
from datetime import datetime, timedelta
from decimal import Decimal
from unittest.mock import Mock

import pytest
from sqlalchemy import event
from sqlalchemy.exc import SQLAlchemyError

from app.extensions import db
from app.models import (
    AppConfig,
    Cliente,
    CompraKit,
    EntregaKit,
    EstoqueLoja,
    EstoqueSitePlano,
    KitCafe,
    KitCafeItem,
    PedidoOnline,
    PedidoOnlineItem,
    Produto,
    Receita,
)
from app.services import compra_kits, kits_cafe, loja_checkout

pytestmark = pytest.mark.loja_host
BASE = datetime(2026, 9, 14, 6, 0)
TOKEN = 'a' * 64


@pytest.fixture(autouse=True)
def ambiente(app, monkeypatch):
    from app.blueprints.loja import kits_routes
    monkeypatch.setenv('LOJA_VISIVEL', '1')
    monkeypatch.setattr(compra_kits, 'agora', lambda: BASE)
    monkeypatch.setattr(kits_routes, 'agora', lambda: BASE)
    monkeypatch.setattr('app.services.email.disponivel', lambda: False)
    monkeypatch.setattr('app.services.loja_alerta.alertar_esgotado', Mock())
    monkeypatch.setattr('app.services.loja_alerta.alertar_endereco_falho', Mock())


@pytest.fixture
def frete(monkeypatch):
    retorno = {'ok': True, 'valor': 15.25, 'gratis': False, 'fora_area': False,
               'distancia_km': 3.4, 'endereco': 'Rua das Flores, 10', 'aviso': ''}
    consultar = Mock(return_value=retorno)
    monkeypatch.setattr('app.services.frete.consultar_frete', consultar)
    return consultar


@pytest.fixture
def kit(app, owner_user, loja):
    receita = Receita(nome='Croissant simples', categoria='Croissants', rendimento_qtd=1,
                      rendimento_unidade='un', peso_base=100, site_ativo=True,
                      preco_site=Decimal('9.90'))
    produto = Produto(nome='Suco de laranja 300 ml', ativo=True, site_ativo=True,
                      preco_site=Decimal('12.50'))
    db.session.add_all([receita, produto])
    db.session.flush()
    novo = KitCafe(nome='Café da manhã', descricao='Um kit por dia escolhido',
                   ativo=True, usuario_id=owner_user.id)
    novo.itens.extend([
        KitCafeItem(kind='receita', receita_id=receita.id, quantidade=2),
        KitCafeItem(kind='produto', produto_id=produto.id, quantidade=1),
    ])
    db.session.add(novo)
    AppConfig.set('loja_site_estoque_id', loja.id)
    db.session.add_all([
        EstoqueLoja(loja_id=loja.id, receita_id=receita.id, quantidade=20, quantidade_reservada=0),
        EstoqueLoja(loja_id=loja.id, produto_id=produto.id, quantidade=20, quantidade_reservada=0),
    ])
    db.session.commit()
    return novo


def _form(**mudancas):
    dados = {'nome': 'Maria', 'sobrenome': 'Cliente', 'email': 'maria@example.com',
             'telefone': '11999998888', 'cpf': '52998224725', 'aceite_lgpd': '1',
             'logradouro': 'Rua das Flores', 'numero': '10', 'complemento': 'Apto 2',
             'bairro': 'Moema', 'cidade': 'São Paulo', 'uf': 'SP', 'cep': '04077000'}
    dados.update(mudancas)
    return dados


def _agenda(*dias):
    return [{'data': (BASE.date() + timedelta(days=dia)).isoformat(),
             'janela': '09:00–10:00'} for dia in (dias or (1, 8))]


def _comprar(kit, *, agenda=None, token=TOKEN, form=None):
    compra, erros = compra_kits.criar_compra(
        kit, form or _form(), agenda if agenda is not None else _agenda(),
        checkout_token=token, base=BASE)
    assert not erros, erros
    assert compra is not None
    return compra


def _vazio():
    assert CompraKit.query.count() == 0
    assert EntregaKit.query.count() == 0
    assert PedidoOnline.query.count() == 0
    assert PedidoOnlineItem.query.count() == 0


def _abrir(app, kit):
    cliente = app.test_client()
    resposta = cliente.get(f'/loja/kits-cafe/{kit.id}')
    assert resposta.status_code == 200
    with cliente.session_transaction() as sessao:
        token = sessao['kits_checkout_tokens'][str(kit.id)]
    return cliente, token, resposta.get_data(as_text=True)


def _post(cliente, kit, token, agenda=None, **mudancas):
    dados = _form(checkout_token=token, agenda_json=json.dumps(
        _agenda() if agenda is None else agenda))
    dados.update(mudancas)
    return cliente.post(f'/loja/kits-cafe/{kit.id}', data=dados)


def test_duas_datas_criam_dois_pedidos_uma_compra_e_frete_por_entrega(kit, frete):
    compra = _comprar(kit, agenda=_agenda(8, 1))
    assert compra.subtotal == Decimal('64.60')
    assert compra.frete_total == Decimal('30.50')
    assert compra.valor_total == Decimal('95.10')
    assert compra.pago_em is None
    assert compra.expira_em == BASE + timedelta(minutes=35)
    assert len(compra.entregas) == 2
    assert [e.ordem for e in compra.entregas] == [1, 2]
    assert [e.pedido.data_entrega.isoformat() for e in compra.entregas] == [
        item['data'] for item in _agenda(1, 8)]
    assert compra.pedido_principal is compra.entregas[0].pedido
    for entrega in compra.entregas:
        pedido = entrega.pedido
        assert pedido.status == 'aguardando_pagamento'
        assert pedido.modo_entrega == 'agendada' and pedido.loja_retirada_id is None
        assert pedido.subtotal == Decimal('32.30')
        assert pedido.frete_valor == Decimal('15.25')
        assert pedido.valor_total == Decimal('47.55')
        assert pedido.endereco_numero == '10' and pedido.endereco_complemento == 'Apto 2'
        assert [it.quantidade for it in pedido.itens] == [2, 1]
        assert entrega.coletado_em is None
    frete.assert_called_once()
    assert Cliente.query.count() == 1
    assert all(e.quantidade == 20 and e.quantidade_reservada == 0
               for e in EstoqueLoja.query.all())


def test_precos_frete_quantidades_e_modo_do_navegador_nao_definem_cobranca(kit, frete):
    compra = _comprar(kit, form=_form(preco='0.01', valor_total='0.01', subtotal='0.01',
                                      frete_valor='0', qtd='99', modo_entrega='retirada',
                                      itens_json='[{"kind":"produto","id":999,"qtd":999}]'))
    assert compra.valor_total == Decimal('95.10')
    assert all(e.pedido.modo_entrega == 'agendada' for e in compra.entregas)
    assert all(len(e.pedido.itens) == 2 for e in compra.entregas)


def test_mesmo_nonce_nao_repete_compra_nem_cotacao(kit, frete):
    primeira = _comprar(kit)
    segunda = _comprar(kit)
    assert primeira.id == segunda.id
    assert CompraKit.query.count() == 1 and PedidoOnline.query.count() == 2
    frete.assert_called_once()


def test_nonce_de_outro_kit_nao_reutiliza_compra(kit, frete):
    _comprar(kit)
    outro = KitCafe(nome='Outro kit', ativo=True, usuario_id=kit.usuario_id)
    outro.itens.append(KitCafeItem(kind='receita', receita_id=kit.itens[0].receita_id,
                                   quantidade=1))
    db.session.add(outro)
    db.session.commit()
    compra, erros = compra_kits.criar_compra(outro, _form(), _agenda(),
                                            checkout_token=TOKEN, base=BASE)
    assert compra is None and any('outro kit' in erro for erro in erros)
    assert CompraKit.query.count() == 1


@pytest.mark.parametrize('token', ['', None, 'x' * 64, 'a' * 63, 'a' * 65, 'á' * 64])
def test_nonce_invalido_no_servico_nao_cria(kit, frete, token):
    compra, erros = compra_kits.criar_compra(kit, _form(), _agenda(),
                                            checkout_token=token, base=BASE)
    assert compra is None and erros
    _vazio()
    frete.assert_not_called()


@pytest.mark.parametrize('agenda', [None, {}, [], [None], ['2026-09-15'],
    [{'data': '2026-02-30', 'janela': '09:00–10:00'}],
    [{'data': '20260915', 'janela': '09:00–10:00'}],
    [{'data': '2026-09-15', 'janela': ''}],
    [{'data': '2026-09-15', 'janela': 9}],
    [{'data': '2026-09-15', 'janela': 'x' * 41}], _agenda(1, 1), _agenda(*range(32))])
def test_agenda_malformada_ou_repetida_nao_cria(kit, frete, agenda):
    compra, erros = compra_kits.criar_compra(kit, _form(), agenda,
                                            checkout_token=TOKEN, base=BASE)
    assert compra is None and erros
    _vazio()
    frete.assert_not_called()


def test_data_apos_quatorze_dias_dentro_do_mes_e_aceita(kit, frete):
    compra = _comprar(kit, agenda=_agenda(1, 28))
    assert compra.entregas[1].pedido.data_entrega == BASE.date() + timedelta(days=28)


@pytest.mark.parametrize('agenda', [_agenda(-1), _agenda(40),
    [{'data': '2026-09-15', 'janela': '04:00–05:00'}],
    [{'data': '2026-09-15', 'janela': '09:00-10:00'}]])
def test_datas_e_janelas_fora_das_regras_sao_recusadas(kit, frete, agenda):
    compra, erros = compra_kits.criar_compra(kit, _form(), agenda,
                                            checkout_token=TOKEN, base=BASE)
    assert compra is None and erros
    _vazio()


def test_segunda_data_invalida_desfaz_primeiro_pedido_e_dados_do_cliente(kit, frete):
    cliente = Cliente(nome='Nome anterior', email='maria@example.com', telefone='11888887777',
                       cpf='52998224725', origem='site')
    db.session.add(cliente)
    db.session.commit()
    agenda = _agenda()
    agenda[1]['janela'] = '00:00–01:00'
    compra, erros = compra_kits.criar_compra(kit, _form(), agenda,
                                            checkout_token=TOKEN, base=BASE)
    assert compra is None and erros
    _vazio()
    assert db.session.get(Cliente, cliente.id).nome == 'Nome anterior'
    assert cliente.telefone == '11888887777'


def test_falha_ao_gravar_grupo_desfaz_todos_os_pedidos(kit, frete):
    def falhar(_mapper, _connection, _target):
        raise SQLAlchemyError('Falha simulada ao gravar grupo')
    event.listen(CompraKit, 'before_insert', falhar)
    try:
        compra, erros = compra_kits.criar_compra(kit, _form(), _agenda(),
                                                checkout_token=TOKEN, base=BASE)
    finally:
        event.remove(CompraKit, 'before_insert', falhar)
    assert compra is None and erros
    _vazio()
    assert Cliente.query.count() == 0


def test_kit_pausado_e_item_pausado_nao_geram_compra(kit, frete):
    kit.ativo = False
    db.session.commit()
    compra, erros = compra_kits.criar_compra(kit, _form(), _agenda(),
                                            checkout_token=TOKEN, base=BASE)
    assert compra is None and erros
    kit.ativo = True
    receita = db.session.get(Receita, kit.itens[0].receita_id)
    receita.site_ativo = False
    db.session.commit()
    compra, erros = compra_kits.criar_compra(kit, _form(), _agenda(),
                                            checkout_token=TOKEN, base=BASE)
    assert compra is None and erros
    _vazio()


def test_quantidade_insuficiente_no_plano_de_uma_data_recusa_grupo_inteiro(kit, frete):
    db.session.add(EstoqueSitePlano(kind='receita', item_id=kit.itens[0].receita_id,
                                   data=BASE.date() + timedelta(days=8),
                                   qtd_planejada=1, qtd_reservada=0))
    db.session.commit()
    compra, erros = compra_kits.criar_compra(kit, _form(), _agenda(),
                                            checkout_token=TOKEN, base=BASE)
    assert compra is None and any('quantidade' in erro for erro in erros)
    _vazio()


def test_regiao_distante_recusa_primeira_janela_em_qualquer_data(kit, frete):
    frete.return_value['distancia_km'] = 12.0
    agenda = _agenda()
    agenda[1]['janela'] = '08:00–09:00'
    compra, erros = compra_kits.criar_compra(kit, _form(), agenda,
                                            checkout_token=TOKEN, base=BASE)
    assert compra is None and any('09:00' in erro for erro in erros)
    _vazio()


def test_compra_mantem_snapshot_apos_edicao_do_kit_e_reajuste(kit, frete):
    compra = _comprar(kit)
    kit.nome = 'Nome novo'
    kit.itens[0].quantidade = 5
    db.session.get(Receita, kit.itens[0].receita_id).preco_site = 99
    db.session.commit()
    assert compra.kit_nome == 'Café da manhã'
    assert compra.valor_total == Decimal('95.10')
    assert all(e.pedido.itens[0].quantidade == 2 for e in compra.entregas)
    assert all(e.pedido.itens[0].preco_unitario == Decimal('9.90') for e in compra.entregas)
    assert kits_cafe.preco_kit(kit) == Decimal('507.50')


def test_contexto_oferece_calendario_do_mes_e_nonce_da_sessao(app, kit):
    _, token, html = _abrir(app, kit)
    assert re.fullmatch('[0-9a-f]{64}', token)
    cfg = json.loads(re.search(r'<script id="kits-config"[^>]*>(.*?)</script>',
                               html, re.S).group(1))
    assert cfg['precoCentavos'] == 3230
    assert '2026-10-12' in cfg['janelas']
    assert cfg['janelas']['2026-09-15']
    assert 'sem renovação automática' in html.lower()
    assert 'Fretes das entregas' in html


def test_catalogo_publico_nao_exibe_rascunho_e_escapa_nome(app, kit):
    kit.nome = '<script>alert(1)</script>'
    db.session.commit()
    html = app.test_client().get('/loja/kits-cafe').get_data(as_text=True)
    assert '<script>alert(1)</script>' not in html
    assert '&lt;script&gt;alert(1)&lt;/script&gt;' in html
    kit.ativo = False
    db.session.commit()
    assert app.test_client().get(f'/loja/kits-cafe/{kit.id}').status_code == 404


def test_post_valido_dois_envios_mesma_compra_e_pagina_pagamento_com_total(app, kit, frete):
    cliente, token, _ = _abrir(app, kit)
    resposta = _post(cliente, kit, token)
    assert resposta.status_code == 302
    compra = CompraKit.query.one()
    assert resposta.location.endswith(f'/pedido/{compra.pedido_principal.codigo}/pagamento')
    repetida = _post(cliente, kit, token)
    assert repetida.status_code == 302 and repetida.location == resposta.location
    assert CompraKit.query.count() == 1 and PedidoOnline.query.count() == 2
    pagamento = cliente.get(resposta.location)
    assert pagamento.status_code == 200
    html = pagamento.get_data(as_text=True)
    assert '95,10' in html and '64,60' in html and '30,50' in html
    for entrega in compra.entregas:
        assert entrega.pedido.codigo in html
        assert entrega.pedido.data_entrega.strftime('%d/%m/%Y') in html


def test_reabrir_apos_compra_gera_novo_nonce_sem_tocar_compra_anterior(app, kit, frete):
    cliente, token, _ = _abrir(app, kit)
    assert _post(cliente, kit, token).status_code == 302
    assert cliente.get(f'/loja/kits-cafe/{kit.id}').status_code == 200
    with cliente.session_transaction() as sessao:
        assert sessao['kits_checkout_tokens'][str(kit.id)] != token
    assert CompraKit.query.count() == 1


@pytest.mark.parametrize('token', ['', 'b' * 64, 'á' * 64])
def test_post_nonce_forjado_recusado_sem_erro500(app, kit, frete, token):
    cliente, _, _ = _abrir(app, kit)
    resposta = _post(cliente, kit, token)
    assert resposta.status_code == 400
    _vazio()
    frete.assert_not_called()


def test_formulario_de_outra_sessao_nao_pode_ser_reutilizado(app, kit, frete):
    _, token, _ = _abrir(app, kit)
    outra_sessao = app.test_client()
    resposta = _post(outra_sessao, kit, token)
    assert resposta.status_code == 400
    _vazio()


def test_post_agenda_invalida_reexibe_dados_sem_perder_nome(app, kit, frete):
    cliente, token, _ = _abrir(app, kit)
    resposta = _post(cliente, kit, token, agenda=_agenda(1, 1), nome='Catarina')
    assert resposta.status_code == 400
    html = resposta.get_data(as_text=True)
    assert 'value="Catarina"' in html
    assert 'cada data uma única vez' in html
    _vazio()


def test_catalogo_que_muda_no_meio_nao_vende_kit_parcial(kit, frete, monkeypatch):
    original = loja_checkout.montar_itens
    chamadas = 0

    def mudar(raw, **kwargs):
        nonlocal chamadas
        chamadas += 1
        itens, avisos = original(raw, **kwargs)
        if chamadas == 3:
            return itens[:1], ['O suco saiu de catálogo entre as datas.']
        return itens, avisos
    monkeypatch.setattr(loja_checkout, 'montar_itens', mudar)
    compra, erros = compra_kits.criar_compra(kit, _form(), _agenda(),
                                            checkout_token=TOKEN, base=BASE)
    assert compra is None and erros
    _vazio()
