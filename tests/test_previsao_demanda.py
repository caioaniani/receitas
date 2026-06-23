"""Smoke tests da previsao de demanda.

Cria vendas Seru sinteticas em datas conhecidas e verifica que a media
por dia-da-semana sai certa.
"""
from datetime import date, datetime, timedelta


def test_prever_demanda_zero_sem_historico(app, loja, catalogo):
    """Loja sem vendas Seru: retorna lista vazia, nao crasha."""
    from app.services.previsao_demanda import prever_demanda
    out = prever_demanda(loja.id, date.today())
    assert out == []


def test_prever_demanda_media_por_dow(app, loja, catalogo):
    """Cria 4 vendas Seru em 4 segundas-feiras (qtd 10, 12, 8, 6),
    previsao pra proxima segunda deve ser 9."""
    from app.extensions import db
    from app.models import EstoqueLoja, MovEstoqueLoja
    from app.services.previsao_demanda import prever_demanda

    el = EstoqueLoja(loja_id=loja.id, receita_id=catalogo['receita'].id,
                     quantidade=0)
    db.session.add(el)
    db.session.flush()

    # 4 segundas-feiras, +1 quarta (pra confirmar que so dow correto entra).
    hoje = date.today()
    proxima_segunda = hoje + timedelta(days=(0 - hoje.weekday()) % 7 or 7)
    qtds = [10, 12, 8, 6]
    for i, qtd in enumerate(qtds, start=1):
        d = proxima_segunda - timedelta(weeks=i)
        dt = datetime.combine(d, datetime.min.time().replace(hour=12))
        db.session.add(MovEstoqueLoja(
            estoque_loja_id=el.id, tipo='venda_seru',
            quantidade=qtd, data=dt,
        ))
    # 1 venda numa quarta (deve ser ignorada pra previsao de segunda)
    quarta_passada = proxima_segunda - timedelta(weeks=1, days=-2)
    db.session.add(MovEstoqueLoja(
        estoque_loja_id=el.id, tipo='venda_seru', quantidade=999,
        data=datetime.combine(quarta_passada, datetime.min.time()),
    ))
    db.session.commit()

    out = prever_demanda(loja.id, proxima_segunda)
    assert len(out) == 1
    assert out[0]['nome'] == 'Croissant Tradicional'
    assert out[0]['previsao'] == 9.0  # (10+12+8+6)/4
    assert out[0]['observacoes_n'] == 4


def test_prever_demanda_ignora_outros_tipos(app, loja, catalogo):
    """desperdicio e entrada_lote nao contam como demanda."""
    from app.extensions import db
    from app.models import EstoqueLoja, MovEstoqueLoja
    from app.services.previsao_demanda import prever_demanda

    el = EstoqueLoja(loja_id=loja.id, receita_id=catalogo['receita'].id,
                     quantidade=0)
    db.session.add(el)
    db.session.flush()
    hoje = date.today()
    proxima_segunda = hoje + timedelta(days=(0 - hoje.weekday()) % 7 or 7)
    segunda_passada = proxima_segunda - timedelta(weeks=1)
    db.session.add_all([
        MovEstoqueLoja(estoque_loja_id=el.id, tipo='desperdicio',
                       quantidade=100,
                       data=datetime.combine(segunda_passada, datetime.min.time())),
        MovEstoqueLoja(estoque_loja_id=el.id, tipo='entrada_lote',
                       quantidade=50,
                       data=datetime.combine(segunda_passada, datetime.min.time())),
    ])
    db.session.commit()
    out = prever_demanda(loja.id, proxima_segunda)
    assert out == []  # nenhuma venda_seru = nenhuma previsao


# ── prever_pedido_por_loja: previsao baseada em PedidoLoja (23/06/2026) ──

def test_prever_pedido_por_loja_media_semanal(app, loja, catalogo):
    """3 pedidos da loja em 3 semanas (30 cada) -> media 30/sem, sugerido 30.
    Cancelado e pedido fora da janela NAO entram."""
    from datetime import timedelta

    from app.extensions import db
    from app.models import PedidoItem, PedidoLoja
    from app.services.previsao_demanda import prever_pedido_por_loja
    from app.utils import hoje

    ref = hoje() + timedelta(days=1)  # janela = [ref-21, ref)
    rid = catalogo['receita'].id

    def _ped(dias_atras, qtd, status='recebido'):
        p = PedidoLoja(loja_id=loja.id, status=status,
                       data_entrega=ref - timedelta(days=dias_atras))
        db.session.add(p)
        db.session.flush()
        db.session.add(PedidoItem(pedido_id=p.id, receita_id=rid,
                                  quantidade=qtd))
        return p

    _ped(1, 30)
    _ped(8, 30)
    _ped(15, 30)
    _ped(3, 999, status='cancelado')      # cancelado: ignorado
    _ped(40, 500)                          # fora da janela (40 dias): ignorado
    db.session.commit()

    out = prever_pedido_por_loja(semanas=3, data_ref=ref.isoformat())
    assert loja.id in out
    d = out[loja.id]
    assert d['pedidos_considerados'] == 3       # cancelado e antigo fora
    item = next(i for i in d['itens']
                if i['nome'] == catalogo['receita'].nome)
    assert item['total'] == 90
    assert item['media_semanal'] == 30.0
    assert item['sugerido'] == 30


def test_prever_pedido_por_loja_vazio(app, loja, catalogo):
    """Sem pedidos na janela -> dict vazio (nao crasha)."""
    from app.services.previsao_demanda import prever_pedido_por_loja
    assert prever_pedido_por_loja(semanas=3) == {}


def test_prever_pedido_arredonda_pra_cima(app, loja, catalogo):
    """media fracionaria -> sugerido eh ceil (2 pedidos de 10 em 3 semanas =
    6.67/sem -> sugerido 7)."""
    from datetime import timedelta

    from app.extensions import db
    from app.models import PedidoItem, PedidoLoja
    from app.services.previsao_demanda import prever_pedido_por_loja
    from app.utils import hoje

    ref = hoje() + timedelta(days=1)
    rid = catalogo['receita'].id
    for dias in (2, 9):
        p = PedidoLoja(loja_id=loja.id, status='recebido',
                       data_entrega=ref - timedelta(days=dias))
        db.session.add(p)
        db.session.flush()
        db.session.add(PedidoItem(pedido_id=p.id, receita_id=rid, quantidade=10))
    db.session.commit()

    out = prever_pedido_por_loja(semanas=3, data_ref=ref.isoformat())
    item = out[loja.id]['itens'][0]
    assert item['total'] == 20
    assert item['media_semanal'] == 6.7
    assert item['sugerido'] == 7   # ceil(6.67)


def test_read_prever_pedido_executor_formata(app, loja, catalogo):
    """Executor do copilot formata a previsao por loja em texto (nao crash)."""
    from datetime import timedelta
    from types import SimpleNamespace

    from app.extensions import db
    from app.models import PedidoItem, PedidoLoja
    from app.services.copilot import _read_prever_pedido
    from app.utils import hoje

    ref = hoje()
    rid = catalogo['receita'].id
    p = PedidoLoja(loja_id=loja.id, status='recebido',
                   data_entrega=ref - timedelta(days=2))
    db.session.add(p)
    db.session.flush()
    db.session.add(PedidoItem(pedido_id=p.id, receita_id=rid, quantidade=12))
    db.session.commit()

    user = SimpleNamespace(is_admin=lambda: True, papel='admin')
    res = _read_prever_pedido({'semanas': 3}, user)
    assert 'texto' in res
    assert loja.nome in res['texto']
    assert catalogo['receita'].nome in res['texto']
