"""Escolha de suco: catálogo autorizado, preço e produto reais em cada entrega."""
from datetime import datetime, timedelta
from decimal import Decimal
from unittest.mock import Mock

import pytest
from sqlalchemy import delete, update
from werkzeug.datastructures import MultiDict

from app.extensions import db
from app.models import (
    AppConfig,
    CompraKit,
    EntregaKit,
    EstoqueLoja,
    EstoqueSitePlano,
    KitCafe,
    KitCafeItem,
    KitCafeSuco,
    PedidoOnline,
    PedidoOnlineItem,
    Produto,
    Receita,
)
from app.services import compra_kits, kits_cafe, loja_catalogo

BASE = datetime(2026, 9, 14, 6, 0)
TOKEN = 'f' * 64


@pytest.fixture
def cenario(app, owner_user, loja, monkeypatch, congela_hoje):
    congela_hoje(2026, 9, 14, 6)
    monkeypatch.setattr('app.services.email.disponivel', lambda: False)
    monkeypatch.setattr('app.services.loja_alerta.alertar_esgotado', Mock())
    monkeypatch.setattr('app.services.loja_alerta.alertar_endereco_falho', Mock())
    frete = Mock(return_value={
        'ok': True, 'valor': 15.25, 'gratis': False, 'fora_area': False,
        'distancia_km': 3.4, 'endereco': 'Rua das Flores, 10', 'aviso': '',
    })
    monkeypatch.setattr('app.services.frete.consultar_frete', frete)
    receita = Receita(nome='Croissant simples', categoria='Croissants', rendimento_qtd=1,
                      rendimento_unidade='un', peso_base=100, site_ativo=True, preco_site=9.90)
    laranja = Produto(nome='Suco de laranja 1 L', ativo=True, site_ativo=True, preco_site=48)
    verde = Produto(nome='Suco verde 1 L', ativo=True, site_ativo=True, preco_site=50)
    outro = Produto(nome='Suco fora deste kit', ativo=True, site_ativo=True, preco_site=10)
    kit = KitCafe(nome='Café com escolha', ativo=True, usuario_id=owner_user.id)
    db.session.add_all([receita, laranja, verde, outro, kit])
    db.session.flush()
    kit.itens.append(KitCafeItem(kind='receita', receita_id=receita.id, quantidade=2))
    kit.sucos.extend([KitCafeSuco(produto_id=verde.id), KitCafeSuco(produto_id=laranja.id)])
    AppConfig.set('loja_site_estoque_id', loja.id)
    for produto in [laranja, verde, outro]:
        db.session.add(EstoqueLoja(loja_id=loja.id, produto_id=produto.id,
                                   quantidade=20, quantidade_reservada=0))
        for deslocamento in [1, 8]:
            db.session.add(EstoqueSitePlano(
                kind='produto', item_id=produto.id,
                data=BASE.date() + timedelta(days=deslocamento),
                qtd_planejada=10, qtd_reservada=0))
    db.session.add(EstoqueLoja(loja_id=loja.id, receita_id=receita.id,
                               quantidade=20, quantidade_reservada=0))
    db.session.commit()
    return kit, laranja, verde, outro, frete


def _form(**extras):
    return dict(nome='Maria', sobrenome='Cliente', email='maria@example.com',
                telefone='11999998888', cpf='52998224725', aceite_lgpd='1',
                logradouro='Rua das Flores', numero='10', bairro='Moema',
                cidade='São Paulo', uf='SP', cep='04077000', **extras)


def _comprar(kit, form):
    agenda = [{'data': (BASE.date() + timedelta(days=dia)).isoformat(),
               'janela': '09:00–10:00'} for dia in [1, 8]]
    return compra_kits.criar_compra(kit, form, agenda, checkout_token=TOKEN, base=BASE)


def _sem_compra(frete):
    assert CompraKit.query.count() == 0
    assert EntregaKit.query.count() == 0
    assert PedidoOnline.query.count() == 0
    assert PedidoOnlineItem.query.count() == 0
    frete.assert_not_called()


def test_preview_menor_preco_sem_alterar_opcoes_do_owner(cenario):
    kit, laranja, verde, _, _ = cenario
    assert kits_cafe.opcoes_suco(kit) == [
        {'id': laranja.id, 'nome': laranja.nome, 'preco': Decimal('48.00')},
        {'id': verde.id, 'nome': verde.nome, 'preco': Decimal('50.00')},
    ]
    assert kits_cafe.preco_kit(kit) == Decimal('67.80')
    assert kits_cafe.itens_do_kit(kit)[-1] == {'kind': 'produto', 'id': laranja.id, 'qtd': 1}
    assert len(kit.itens) == 1 and len(kit.sucos) == 2


@pytest.mark.parametrize('escolha', [None, '', '0', '-1', '1.0', '01', '9999999999', True, 1])
def test_post_sem_escolha_ou_malformado_nao_assume_preview(cenario, escolha):
    kit, _, _, _, frete = cenario
    form = _form() if escolha is None else _form(suco_id=escolha)
    compra, erros = _comprar(kit, form)
    assert compra is None and any('suco' in erro.lower() for erro in erros)
    _sem_compra(frete)


def test_suco_publicado_de_outro_kit_nao_e_aceito(cenario):
    kit, _, _, outro, frete = cenario
    compra, erros = _comprar(kit, _form(suco_id=str(outro.id)))
    assert compra is None and erros
    _sem_compra(frete)


def test_duas_escolhas_no_post_nao_usam_primeira_em_silencio(cenario):
    kit, laranja, verde, _, frete = cenario
    form = MultiDict(_form(suco_id=str(verde.id)))
    form.add('suco_id', str(laranja.id))
    compra, erros = _comprar(kit, form)
    assert compra is None and erros
    _sem_compra(frete)


def test_verde_gera_produto_preco_e_reserva_corretos_nas_duas_entregas(cenario):
    kit, laranja, verde, _, frete = cenario
    compra, erros = _comprar(kit, _form(suco_id=str(verde.id), preco='0.01', qtd_suco='99'))
    assert not erros and compra is not None
    assert compra.subtotal == Decimal('139.60')
    assert compra.frete_total == Decimal('30.50')
    assert compra.valor_total == Decimal('170.10')
    for entrega in compra.entregas:
        sucos = [item for item in entrega.pedido.itens if item.kind == 'produto']
        assert len(sucos) == 1
        assert (sucos[0].produto_id, sucos[0].nome, sucos[0].quantidade,
                sucos[0].preco_unitario, sucos[0].subtotal) == (
                    verde.id, verde.nome, 1, Decimal('50.00'), Decimal('50.00'))
        assert entrega.pedido.valor_total == Decimal('85.05')
        assert entrega.reserva_plano and not entrega.coletado_em
    assert {p.qtd_reservada for p in EstoqueSitePlano.query.filter_by(kind='produto', item_id=verde.id)} == {1}
    assert {p.qtd_reservada for p in EstoqueSitePlano.query.filter_by(kind='produto', item_id=laranja.id)} == {0}
    assert all(p.quantidade == 20 and p.quantidade_reservada == 0 for p in EstoqueLoja.query.all())
    frete.assert_called_once()


@pytest.mark.parametrize('campo,valor', [('site_ativo', False), ('ativo', False), ('preco_site', None)])
def test_opcao_que_saiu_do_catalogo_recusa_sem_vender_parcial(cenario, campo, valor):
    kit, laranja, verde, _, frete = cenario
    setattr(verde, campo, valor)
    db.session.commit()
    compra, erros = _comprar(kit, _form(suco_id=str(laranja.id)))
    assert compra is None and erros
    assert kits_cafe.publicados() == []
    _sem_compra(frete)


def test_opcao_esgotada_na_janela_do_mes_e_recusada(cenario, monkeypatch):
    kit, _, verde, _, frete = cenario
    original = loja_catalogo.tem_estoque_site
    consultas = []

    def consultar(kind, item_id, **kwargs):
        if (kind, item_id) == ('produto', verde.id):
            consultas.append(kwargs.get('datas'))
            return False
        return original(kind, item_id, **kwargs)

    monkeypatch.setattr(loja_catalogo, 'tem_estoque_site', consultar)
    compra, erros = _comprar(kit, _form(suco_id=str(verde.id)))
    assert compra is None and erros
    assert consultas and all(len(datas) >= 31 for datas in consultas)
    _sem_compra(frete)


def test_edicao_posterior_nao_reescreve_suco_preco_ou_reserva_comprados(cenario):
    kit, laranja, verde, outro, _ = cenario
    compra, erros = _comprar(kit, _form(suco_id=str(verde.id)))
    assert not erros
    kit.nome = 'Novo kit'
    kit.sucos = [suco for suco in kit.sucos if suco.produto_id != verde.id]
    kit.sucos.append(KitCafeSuco(produto_id=outro.id))
    verde.nome, verde.preco_site, verde.site_ativo = 'Nome novo', 99, False
    db.session.commit()
    db.session.expire_all()
    assert compra.kit_nome == 'Café com escolha' and compra.valor_total == Decimal('170.10')
    for entrega in compra.entregas:
        suco = next(item for item in entrega.pedido.itens if item.kind == 'produto')
        assert (suco.produto_id, suco.nome, suco.preco_unitario) == (
            verde.id, 'Suco verde 1 L', Decimal('50.00'))
    assert {opcao['id'] for opcao in kits_cafe.opcoes_suco(kit)} == {laranja.id, outro.id}


def test_disponibilidade_do_suco_respeita_d_32_do_kit_sob_encomenda(cenario):
    kit, laranja, verde, _, _ = cenario
    db.session.get(Receita, kit.itens[0].receita_id).sob_encomenda = True
    EstoqueSitePlano.query.filter_by(kind='produto', item_id=verde.id).delete()
    for dia in range(35):
        db.session.add(EstoqueSitePlano(
            kind='produto', item_id=verde.id, data=BASE.date() + timedelta(days=dia),
            qtd_planejada=10 if dia == 32 else 0, qtd_reservada=0))
    db.session.commit()
    assert not loja_catalogo.tem_estoque_site('produto', verde.id)
    opcoes = kits_cafe.opcoes_suco(kit)
    assert {opcao['id'] for opcao in opcoes} == {laranja.id, verde.id}
    itens, erros = kits_cafe.montar(kit, verde.id)
    assert not erros and itens[-1]['produto_id'] == verde.id


def test_relacoes_carregadas_antes_do_lock_sao_relidas(cenario):
    kit, laranja, verde, _, frete = cenario
    assert len(kit.sucos) == 2 and kit.itens[0].quantidade == 2
    # Simula outra transação sem sincronizar o identity map desta sessão.
    db.session.execute(delete(KitCafeSuco).where(KitCafeSuco.produto_id == verde.id),
                       execution_options={'synchronize_session': False})
    db.session.execute(update(KitCafeItem).where(KitCafeItem.kit_id == kit.id).values(quantidade=3),
                       execution_options={'synchronize_session': False})
    assert len(kit.sucos) == 2 and kit.itens[0].quantidade == 2
    compra, erros = _comprar(kit, _form(suco_id=str(laranja.id)))
    assert compra is None and erros
    assert len(kit.sucos) == 1 and kit.itens[0].quantidade == 3
    _sem_compra(frete)


def test_kit_legado_sem_grupo_mantem_compra_sem_escolha(cenario):
    kit, laranja, _, _, _ = cenario
    kit.sucos.clear()
    kit.itens.append(KitCafeItem(kind='produto', produto_id=laranja.id, quantidade=1))
    db.session.commit()
    compra, erros = _comprar(kit, _form())
    assert not erros and compra.valor_total == Decimal('166.10')
    assert all(any(item.produto_id == laranja.id for item in entrega.pedido.itens)
               for entrega in compra.entregas)


def test_kit_sem_grupo_nao_aceita_injecao_de_suco(cenario):
    kit, laranja, _, _, frete = cenario
    kit.sucos.clear()
    db.session.commit()
    compra, erros = _comprar(kit, _form(suco_id=str(laranja.id)))
    assert compra is None and erros
    _sem_compra(frete)


@pytest.mark.parametrize('ids', [[1], list(range(1, 12)), [1, 1], [True, 2], ['1', 2], [-1, 2]])
def test_owner_recusa_grupo_invalido(cenario, ids):
    opcoes, erros = kits_cafe.preparar_sucos(ids, [])
    assert not opcoes and erros


def test_owner_recusa_opcao_duplicada_nos_itens_fixos(cenario):
    _, laranja, verde, _, _ = cenario
    opcoes, erros = kits_cafe.preparar_sucos(
        [laranja.id, verde.id], [{'kind': 'produto', 'produto_id': verde.id, 'qtd': 1}])
    assert not opcoes and any('item fixo' in erro for erro in erros)


def test_owner_recusa_menu_configuravel_como_suco(cenario):
    from test_menu_configuravel import _menu

    _, laranja, _, _, _ = cenario
    menu, _ = _menu(db)
    opcoes, erros = kits_cafe.preparar_sucos([laranja.id, menu.id], [])
    assert not opcoes and any('menu configurável' in erro for erro in erros)


def test_owner_pode_remover_grupo_sem_mudar_itens_fixos(cenario):
    kit, _, _, _, _ = cenario
    assert kits_cafe.preparar_sucos([], kits_cafe.itens_do_kit(kit)) == ([], [])
