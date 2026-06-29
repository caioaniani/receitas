"""Modo MANUAL de pedidos da semana (media_semanal_pedidos): em vez de prever
por dia, devolve a MEDIA SEMANAL por (loja, produto) dividida IGUAL entre os
dias do horizonte, pro admin ajustar. Reusa o POST de pedidos_semana_gerar.
"""
from datetime import timedelta

from app.extensions import db
from app.models import Loja, PedidoItem, PedidoLoja, Receita
from app.services.previsao_producao import media_semanal_pedidos
from app.utils import hoje


def _receita(nome='Croissant'):
    r = Receita(nome=nome, categoria='Croissants', rendimento_qtd=1,
                rendimento_unidade='un', peso_base=100.0)
    db.session.add(r)
    db.session.commit()
    return r


def _loja(nome='Loja A'):
    loja = Loja(nome=nome, ativa=True)
    db.session.add(loja)
    db.session.commit()
    return loja


def _pedido(loja, data_entrega, receita, qtd, status='recebido'):
    p = PedidoLoja(loja_id=loja.id, status=status, data_entrega=data_entrega,
                   data_pedido=data_entrega)
    db.session.add(p)
    db.session.flush()
    db.session.add(PedidoItem(pedido_id=p.id, receita_id=receita.id,
                              quantidade=qtd))
    db.session.commit()
    return p


def _prod(grade, loja_id, rid):
    loja = next((entry for entry in grade['lojas']
                 if entry['loja_id'] == loja_id), None)
    if loja is None:
        return None
    return next((p for p in loja['produtos'] if p['receita_id'] == rid), None)


def test_media_semanal_e_split_igual(app):
    """4 semanas, 70 un por semana -> media 70; split igual entre 7 dias soma
    exatamente 70 (maior resto), nada perdido."""
    loja = _loja()
    r = _receita()
    hoje_d = hoje()
    for sem in (1, 2, 3, 4):
        _pedido(loja, hoje_d - timedelta(days=7 * sem), r, 70)

    grade = media_semanal_pedidos(horizonte_dias=7, janela_semanas=4,
                                  inicio_offset_dias=0)
    p = _prod(grade, loja.id, r.id)
    assert p is not None
    assert p['media_semanal'] == 70.0          # 280 / 4 semanas
    assert sum(p['por_dia']) == 70             # split soma exata
    assert len(p['por_dia']) == 7
    # split o mais igual possivel: 70/7 = 10 em todos
    assert p['por_dia'] == [10, 10, 10, 10, 10, 10, 10]


def test_media_usa_total_da_janela(app):
    """A media e o TOTAL na janela / nº de semanas — varias semanas com
    quantidades diferentes."""
    loja = _loja()
    r = _receita()
    hoje_d = hoje()
    # 3 semanas: 100, 50, 30 -> total 180 / 6 semanas (janela) = 30/sem
    for sem, q in ((1, 100), (2, 50), (3, 30)):
        _pedido(loja, hoje_d - timedelta(days=7 * sem), r, q)

    grade = media_semanal_pedidos(horizonte_dias=7, janela_semanas=6)
    p = _prod(grade, loja.id, r.id)
    assert p['media_semanal'] == 30.0
    assert sum(p['por_dia']) == 30


def test_loja_sem_historico_nao_aparece(app):
    """Loja/produto sem pedido na janela nao entra na grade."""
    _loja('Sem Historico')
    _receita('Pao')
    grade = media_semanal_pedidos(horizonte_dias=7, janela_semanas=6)
    assert grade['lojas'] == []


def test_marca_dias_ja_pedidos(app):
    """Dia no horizonte em que a loja ja tem pedido vem em `ja_tem` (a tela
    trava; o gerar pula)."""
    loja = _loja()
    r = _receita()
    hoje_d = hoje()
    for sem in (1, 2):
        _pedido(loja, hoje_d - timedelta(days=7 * sem), r, 70)
    # pedido futuro (no horizonte) -> marca o dia
    futuro = hoje_d + timedelta(days=2)
    _pedido(loja, futuro, r, 5, status='pendente')

    grade = media_semanal_pedidos(horizonte_dias=7, janela_semanas=6,
                                  inicio_offset_dias=0)
    loja_out = next(entry for entry in grade['lojas']
                    if entry['loja_id'] == loja.id)
    assert futuro.isoformat() in loja_out['ja_tem']


def test_expoe_lote_da_receita(app):
    """Cada produto carrega o `lote_pedido` da receita (caixa) — a tela usa pra
    arredondar ao dividir a entrega entre dias escolhidos."""
    loja = _loja()
    r = _receita('Croissant Tradicional')
    r.lote_pedido = 50
    db.session.commit()
    hoje_d = hoje()
    for sem in (1, 2):
        _pedido(loja, hoje_d - timedelta(days=7 * sem), r, 550)

    grade = media_semanal_pedidos(horizonte_dias=7, janela_semanas=6)
    p = _prod(grade, loja.id, r.id)
    assert p is not None
    assert p['lote'] == 50


def test_lote_ausente_vem_zero(app):
    """Receita sem lote_pedido -> lote 0 (a tela divide sem arredondar)."""
    loja = _loja()
    r = _receita()
    hoje_d = hoje()
    _pedido(loja, hoje_d - timedelta(days=7), r, 70)
    grade = media_semanal_pedidos(horizonte_dias=7, janela_semanas=6)
    p = _prod(grade, loja.id, r.id)
    assert p['lote'] == 0


def test_rota_renderiza(app, admin_user):
    loja = _loja('Loja Centro')
    r = _receita('Pão Francês')
    hoje_d = hoje()
    for sem in (1, 2, 3):
        _pedido(loja, hoje_d - timedelta(days=7 * sem), r, 100)

    client = app.test_client()
    client.post('/auth/login', data={'login': admin_user.login, 'senha': '123'},
                follow_redirects=True)
    resp = client.get('/producao/pedidos-semana/media?horizonte=7&janela=6&inicio=0')
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert 'Média semanal' in body or 'média semanal' in body
    assert 'Loja Centro' in body
    assert 'Pão Francês' in body
