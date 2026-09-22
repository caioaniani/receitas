"""Pisos frescos respeitam os dias de abertura na grade e nos pedidos reais."""
from datetime import datetime, timedelta

import pytest

from app.extensions import db
from app.models import (
    EstoqueLoja,
    Loja,
    MovEstoqueLoja,
    PedidoItem,
    PedidoLoja,
    Receita,
)
from app.services import auto_pedidos
from app.services.pedido_merge import MARCADOR_RASCUNHO_AUTO
from app.services.previsao_producao import sugerir_pedidos_por_venda
from app.utils import hoje


@pytest.fixture(autouse=True)
def segunda(congela_hoje):
    congela_hoje()


def _produto(loja, *, estoque=10, fresco=False):
    receita = Receita(
        nome='Choconana', categoria='Viennoiserie', rendimento_qtd=1,
        rendimento_unidade='un', peso_base=100, estado_padrao='assado',
        sugerir_pedido_loja=True,
    )
    db.session.add(receita)
    db.session.flush()
    saldo = EstoqueLoja(
        loja_id=loja.id, receita_id=receita.id, quantidade=estoque,
        pedido_minimo_diario=2, reposicao_por_venda_diaria=fresco,
    )
    db.session.add(saldo)
    db.session.commit()
    return receita, saldo


def _vendas(saldo, quantidade=5):
    for atraso in range(1, 43):
        db.session.add(MovEstoqueLoja(
            estoque_loja_id=saldo.id, tipo='venda_seru', quantidade=quantidade,
            data=datetime.combine(hoje() - timedelta(days=atraso),
                                  datetime.min.time()),
            referencia='historico anterior ao calendario de abertura',
        ))
    db.session.commit()


def _pedido(loja, receita, dias, quantidade, usuario_id=None):
    pedido = PedidoLoja(
        loja_id=loja.id, data_entrega=hoje() + timedelta(days=dias),
        status='pendente', criado_por=usuario_id,
        observacao=MARCADOR_RASCUNHO_AUTO if usuario_id is None else 'Manual',
    )
    db.session.add(pedido)
    db.session.flush()
    db.session.add(PedidoItem(
        pedido_id=pedido.id, receita_id=receita.id, quantidade=quantidade,
    ))
    db.session.commit()
    return pedido


def _sugestao(loja, receita, *, inicio=1, horizonte=6):
    grade = sugerir_pedidos_por_venda(
        horizonte_dias=horizonte, inicio_offset_dias=inicio,
    )
    loja_grade = next(l for l in grade['lojas'] if l['loja_id'] == loja.id)
    return next(p for p in loja_grade['produtos']
                if p['receita_id'] == receita.id)


@pytest.mark.parametrize('fresco', [False, True])
@pytest.mark.parametrize('abertura,esperado', [
    (None, [2, 2, 2, 2, 2, 2]),
    ('56', [0, 0, 0, 0, 2, 2]),
    ('01234', [2, 2, 2, 2, 0, 0]),
])
def test_piso_so_nos_dias_abertos_mesmo_com_sobra_e_sem_venda(
        app, loja, fresco, abertura, esperado):
    with app.app_context():
        loja = db.session.get(Loja, loja.id)
        loja.dias_funcionamento = abertura
        receita, _ = _produto(loja, fresco=fresco)

        assert _sugestao(loja, receita)['por_dia'] == esperado


@pytest.mark.parametrize('fresco', [False, True])
def test_demanda_acima_do_piso_continua_valendo_nos_dias_abertos(
        app, loja, fresco):
    with app.app_context():
        loja = db.session.get(Loja, loja.id)
        loja.dias_funcionamento = '56'
        receita, saldo = _produto(loja, estoque=0, fresco=fresco)
        _vendas(saldo)

        assert _sugestao(loja, receita)['por_dia'] == [0, 0, 0, 0, 5, 5]


@pytest.mark.parametrize('inicio,horizonte,esperado', [
    (1, 6, [0, 0, 0, 0, 2, 3]),
    (5, 2, [2, 3]),
])
def test_dia_fechado_preserva_entrega_existente_sem_consumo_ficticio(
        app, loja, admin_user, inicio, horizonte, esperado):
    """A entrega excepcional de quinta continua no saldo de sábado."""
    with app.app_context():
        loja = db.session.get(Loja, loja.id)
        loja.dias_funcionamento = '56'
        receita, saldo = _produto(loja, estoque=0)
        _vendas(saldo)
        manual = _pedido(loja, receita, 3, 5, admin_user.id)

        produto = _sugestao(loja, receita, inicio=inicio, horizonte=horizonte)

        assert produto['por_dia'] == esperado
        if inicio == 1:
            assert produto['ja_pedido'][2] == 5
        assert manual.status == 'pendente'
        assert manual.itens[0].quantidade == 5


def test_auto_pede_no_fim_de_semana_limpa_rascunho_e_preserva_humano(
        app, loja, admin_user):
    """O motor real não deixa o piso virar pedido em dia fechado."""
    with app.app_context():
        loja = db.session.get(Loja, loja.id)
        loja.dias_funcionamento = '56'
        receita, _ = _produto(loja)
        antigo = _pedido(loja, receita, 1, 2)
        manual = _pedido(loja, receita, 2, 9, admin_user.id)

        resultado = auto_pedidos.gerar_pedidos_automaticos()

        assert resultado['criados'] == 2
        assert antigo.status == 'cancelado'
        assert manual.status == 'pendente'
        assert manual.itens[0].quantidade == 9
        vivos = (PedidoLoja.query
                 .filter(PedidoLoja.loja_id == loja.id,
                         PedidoLoja.status != 'cancelado')
                 .order_by(PedidoLoja.data_entrega).all())
        assert [(p.data_entrega, p.itens[0].quantidade) for p in vivos] == [
            (hoje() + timedelta(days=2), 9),
            (hoje() + timedelta(days=5), 2),
            (hoje() + timedelta(days=6), 2),
        ]
