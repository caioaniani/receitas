"""UX de decisão das grades de pedidos da semana (11/07/2026, "aprimore
essa área de forecast"): contraprova entre motores (?comparar=1), coluna de
desperdício 7d, badge de acurácia por item e badge de histórico raso.
Nada aqui muda a conta dos motores — só o que o operador vê.
"""
from datetime import datetime, time, timedelta

from app.extensions import db
from app.models import (
    Desperdicio,
    EstoqueLoja,
    Loja,
    MovEstoqueLoja,
    PedidoItem,
    PedidoLoja,
    PrevisaoSnapshot,
    Receita,
)
from app.utils import hoje


def _loja(nome='Loja A'):
    loja = Loja(nome=nome, ativa=True)
    db.session.add(loja)
    db.session.commit()
    return loja


def _receita(nome='Croissant'):
    r = Receita(nome=nome, categoria='X', rendimento_qtd=1,
                rendimento_unidade='un', peso_base=100.0)
    db.session.add(r)
    db.session.commit()
    return r


def _pedido_hist(loja, receita, data, qtd=10):
    p = PedidoLoja(loja_id=loja.id, status='recebido', data_entrega=data,
                   data_pedido=data)
    db.session.add(p)
    db.session.flush()
    db.session.add(PedidoItem(pedido_id=p.id, receita_id=receita.id,
                              quantidade=qtd))
    db.session.commit()
    return p


def _venda(loja, receita, data, qtd):
    el = EstoqueLoja.query.filter_by(loja_id=loja.id,
                                     receita_id=receita.id).first()
    if el is None:
        el = EstoqueLoja(loja_id=loja.id, receita_id=receita.id, quantidade=0)
        db.session.add(el)
        db.session.flush()
    db.session.add(MovEstoqueLoja(
        estoque_loja_id=el.id, tipo='venda_seru', quantidade=qtd,
        data=datetime.combine(data, time(12, 0)), referencia='teste'))
    db.session.commit()


def _login(app):
    c = app.test_client()
    c.post('/auth/login', data={'login': 'admin', 'senha': '123'})
    return c


def _semear_historico(loja, receita, semanas=6):
    hoje_d = hoje()
    for sem in range(1, semanas + 1):
        _pedido_hist(loja, receita, hoje_d - timedelta(days=7 * sem))


def test_comparar_motores_na_grade_da_media(app, admin_user):
    """?comparar=1: o número do motor venda+estoque aparece sob a célula da
    média (classe .cp); sem o parâmetro, não."""
    loja = _loja()
    r = _receita()
    _semear_historico(loja, r)
    _venda(loja, r, hoje() - timedelta(days=7), 5)
    c = _login(app)
    sem = c.get('/producao/pedidos-semana/media').get_data(as_text=True)
    assert 'class="cp' not in sem
    assert 'Comparar motores' in sem                # o toggle existe
    com = c.get('/producao/pedidos-semana/media?comparar=1') \
           .get_data(as_text=True)
    assert 'class="cp' in com
    assert 'Motor venda+estoque sugere' in com


def test_comparar_motores_na_grade_de_estoque(app, admin_user):
    loja = _loja()
    r = _receita()
    _semear_historico(loja, r)
    _venda(loja, r, hoje() - timedelta(days=7), 5)
    c = _login(app)
    com = c.get('/producao/pedidos-semana/estoque?comparar=1') \
           .get_data(as_text=True)
    assert 'class="cp' in com
    assert 'Motor de média sugere' in com


def test_coluna_desperdicio_7d_nas_grades(app, admin_user):
    """Desperdício da última semana vira coluna — o operador via só a IA."""
    loja = _loja()
    r = _receita()
    _semear_historico(loja, r)
    _venda(loja, r, hoje() - timedelta(days=7), 5)
    db.session.add(Desperdicio(loja_id=loja.id, receita_id=r.id,
                               quantidade=7, data=hoje() - timedelta(days=2)))
    db.session.commit()
    c = _login(app)
    for url in ('/producao/pedidos-semana/media',
                '/producao/pedidos-semana/estoque'):
        corpo = c.get(url).get_data(as_text=True)
        assert 'Desp. 7d' in corpo
        assert 'descartou 7 un' in corpo


def test_desperdicio_de_mp_aparece_na_grade_de_estoque(app, admin_user):
    """Item MP (token 'mp:<id>') também mostra o desperdício da semana."""
    from app.models import MateriaPrima
    loja = _loja()
    mp = MateriaPrima(nome='Pão de Queijo Congelado', unidade='un',
                      custo_por_kg=10.0, sugerir_pedido_loja=True)
    db.session.add(mp)
    db.session.flush()
    db.session.add(EstoqueLoja(loja_id=loja.id, materia_prima_id=mp.id,
                               quantidade=30))
    db.session.add(Desperdicio(loja_id=loja.id, materia_prima_id=mp.id,
                               quantidade=4, data=hoje()))
    db.session.commit()
    c = _login(app)
    corpo = c.get('/producao/pedidos-semana/estoque').get_data(as_text=True)
    assert 'Pão de Queijo Congelado' in corpo
    assert 'descartou 4 un' in corpo


def test_badge_acuracia_por_item_na_grade(app, admin_user):
    """Com >= 5 snapshots casados do motor da tela, a linha do produto ganha
    o badge WAPE com viés no tooltip."""
    loja = _loja()
    r = _receita()
    _semear_historico(loja, r)
    ontem = hoje() - timedelta(days=1)
    for k in range(5):
        db.session.add(PrevisaoSnapshot(
            data_alvo=ontem - timedelta(days=k), loja_id=loja.id,
            receita_id=r.id, previsto=12, realizado=10,
            motor='media_pedido', lead_dias=1))
    db.session.commit()
    c = _login(app)
    corpo = c.get('/producao/pedidos-semana/media').get_data(as_text=True)
    assert 'WAPE 20.0%' in corpo
    assert 'Acurácia deste motor para este item' in corpo
    # a grade de estoque mede OUTRO motor — sem snapshot dele, sem badge
    corpo_e = c.get('/producao/pedidos-semana/estoque').get_data(as_text=True)
    assert 'Acurácia deste motor para este item' not in corpo_e


def test_badge_historico_raso(app, admin_user):
    """Item cuja média vem de poucas datas ganha o aviso de confiança."""
    loja = _loja()
    r = _receita()
    hoje_d = hoje()
    _pedido_hist(loja, r, hoje_d - timedelta(days=7))    # UMA data só
    c = _login(app)
    corpo = c.get('/producao/pedidos-semana/media').get_data(as_text=True)
    assert 'histórico raso (1)' in corpo
    # com 6 datas, o badge some
    _semear_historico(loja, r)                            # +6 datas
    corpo2 = c.get('/producao/pedidos-semana/media').get_data(as_text=True)
    assert 'histórico raso' not in corpo2
