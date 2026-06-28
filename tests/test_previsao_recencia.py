"""Recencia na previsao do pedido semanal (28/06/2026).

A media por dia-da-semana passou a pesar MAIS as entregas recentes
(decaimento exponencial, meia-vida _MEIA_VIDA_DIAS), em vez de media uniforme
— pega tendencia de loja subindo/caindo sem sair de "media dos ultimos
pedidos". Continua so com historico de PedidoLoja (zero venda).
"""
from datetime import date, timedelta

from app.extensions import db
from app.models import Loja, PedidoItem, PedidoLoja, Receita
from app.services.previsao_producao import _media_recencia, sugerir_pedidos_semana
from app.utils import hoje


# ── helper puro ──────────────────────────────────────────────────────────
def test_media_recencia_um_ponto():
    assert _media_recencia({date(2026, 6, 28): 10}, date(2026, 6, 28)) == 10


def test_media_recencia_meia_vida_infinita_vira_media_uniforme():
    hoje_d = date(2026, 6, 28)
    pts = {date(2026, 6, 21): 10, date(2026, 5, 24): 30}
    # meia-vida gigante -> pesos ~iguais -> media simples (20)
    assert abs(_media_recencia(pts, hoje_d, meia_vida=10**9) - 20) < 1e-6


def test_media_recencia_pesa_mais_o_recente():
    hoje_d = date(2026, 6, 28)
    recente, antigo = date(2026, 6, 21), date(2026, 5, 17)  # 7 vs 42 dias
    # recente=30, antigo=10: media uniforme seria 20; com recencia puxa pra cima
    m = _media_recencia({recente: 30, antigo: 10}, hoje_d)
    assert 20 < m < 30


# ── integracao em sugerir_pedidos_semana ─────────────────────────────────
def _setup(app, qtds_por_semana):
    """Cria 1 loja/1 receita com pedidos no MESMO dia-da-semana de hoje, uma
    por semana. qtds_por_semana[i] = quantidade i+1 semanas atras."""
    loja = Loja(nome='Loja A', ativa=True)
    r = Receita(nome='Croissant', categoria='Croissants', rendimento_qtd=1,
                rendimento_unidade='un', peso_base=100.0)
    db.session.add_all([loja, r])
    db.session.commit()
    hoje_d = hoje()
    for i, q in enumerate(qtds_por_semana, start=1):
        p = PedidoLoja(loja_id=loja.id, status='recebido',
                       data_entrega=hoje_d - timedelta(days=7 * i),
                       data_pedido=hoje_d - timedelta(days=7 * i))
        db.session.add(p)
        db.session.flush()
        db.session.add(PedidoItem(pedido_id=p.id, receita_id=r.id, quantidade=q))
    db.session.commit()
    return loja, r


def _qtd_dia0(sug, loja_id, rid):
    la = next(l for l in sug['lojas'] if l['loja_id'] == loja_id)
    item = next((it for it in la['dias'][0]['itens'] if it['receita_id'] == rid),
                None)
    return item['qtd'] if item else 0


def test_tendencia_de_alta_puxa_sugestao_acima_da_media_uniforme(app):
    # semanas [5..1] atras: 10,10,10,40,40 (recente alto). Media uniforme=22.
    loja, r = _setup(app, [10, 10, 10, 40, 40][::-1])  # i=1 (recente)=40
    sug = sugerir_pedidos_semana(horizonte_dias=7, janela_semanas=6)
    qtd = _qtd_dia0(sug, loja.id, r.id)
    assert qtd > 22, f'recencia deveria puxar acima da media uniforme (22): {qtd}'
    assert qtd <= 40


def test_tendencia_de_queda_puxa_sugestao_abaixo_da_media_uniforme(app):
    # recente baixo: i=1 (recente)=10 ... i=5 (antigo)=40. Media uniforme=28.
    loja, r = _setup(app, [10, 10, 40, 40, 40])  # i=1=10 (recente), i=5=40
    sug = sugerir_pedidos_semana(horizonte_dias=7, janela_semanas=6)
    qtd = _qtd_dia0(sug, loja.id, r.id)
    assert qtd < 28, f'recencia deveria puxar abaixo da media uniforme (28): {qtd}'
    assert qtd >= 10


# ── item 1: recencia tambem no painel de producao (balanco_industria) ─────
def test_balanco_previsto_responde_a_recencia(app):
    from app.services.previsao_producao import balanco_industria
    loja = Loja(nome='Loja A', ativa=True)
    db.session.add(loja)
    db.session.commit()
    hoje_d = hoje()

    def _receita_com(nome, qtds):
        r = Receita(nome=nome, categoria='X', rendimento_qtd=1,
                    rendimento_unidade='un', peso_base=100.0)
        db.session.add(r)
        db.session.commit()
        for i, q in enumerate(qtds, start=1):
            d = hoje_d - timedelta(days=7 * i)
            p = PedidoLoja(loja_id=loja.id, status='recebido',
                           data_entrega=d, data_pedido=d)
            db.session.add(p)
            db.session.flush()
            db.session.add(PedidoItem(pedido_id=p.id, receita_id=r.id,
                                      quantidade=q))
        db.session.commit()
        return r

    # MESMA soma total (140); a diferenca e so QUANDO a massa aconteceu.
    _receita_com('Recente', [40, 40, 20, 20, 20])  # recente alto
    _receita_com('Antigo', [20, 20, 20, 40, 40])   # recente baixo
    bal = balanco_industria(horizonte_dias=7, janela_semanas=6, usar_cache=False)
    prev = {it['nome']: it['previsto'] for it in bal['itens']}
    assert prev['Recente'] > prev['Antigo'], (
        f'recencia deveria elevar o previsto da receita em alta: {prev}')


# ── item 2 REVERTIDO: loja marginal NAO entra (sem "pedidos picados") ─────
def test_rateio_normal_por_participacao(app):
    """Caso equilibrado: cada loja recebe o round da sua participacao."""
    loja_a = Loja(nome='Loja A', ativa=True)
    loja_b = Loja(nome='Loja B', ativa=True)
    r = Receita(nome='Croissant', categoria='X', rendimento_qtd=1,
                rendimento_unidade='un', peso_base=100.0)
    db.session.add_all([loja_a, loja_b, r])
    db.session.commit()
    hoje_d = hoje()
    for i in (1, 2, 3):
        d = hoje_d - timedelta(days=7 * i)
        for loja, q in ((loja_a, 10), (loja_b, 5)):
            p = PedidoLoja(loja_id=loja.id, status='recebido',
                           data_entrega=d, data_pedido=d)
            db.session.add(p)
            db.session.flush()
            db.session.add(PedidoItem(pedido_id=p.id, receita_id=r.id,
                                      quantidade=q))
    db.session.commit()

    sug = sugerir_pedidos_semana(horizonte_dias=7, janela_semanas=6)
    qtds = {}
    for la in sug['lojas']:
        item = next((it for it in la['dias'][0]['itens']
                     if it['receita_id'] == r.id), None)
        if item:
            qtds[la['loja_id']] = item['qtd']
    assert qtds.get(loja_a.id, 0) == 10
    assert qtds.get(loja_b.id, 0) == 5


def test_loja_marginal_nao_entra_no_pedido(app):
    """Anti-"pedidos picados" (28/06/2026): lojas que mal pediam o item (fracao
    < 0,5 un) NAO recebem 1-2 un por causa de sobra de arredondamento. Uma loja
    forte + 3 marginais (1 un cada, uma vez): so a forte entra. Se alguem voltar
    a distribuir por maior-resto, alguma marginal ganharia 1 e o teste quebra."""
    forte = Loja(nome='Forte', ativa=True)
    marginais = [Loja(nome=f'Marg{i}', ativa=True) for i in range(3)]
    r = Receita(nome='Croissant', categoria='X', rendimento_qtd=1,
                rendimento_unidade='un', peso_base=100.0)
    db.session.add_all([forte, *marginais, r])
    db.session.commit()
    hoje_d = hoje()
    # forte: 30 un em 3 semanas (mesmo dow). marginais: 1 un so na 1a semana.
    for i in (1, 2, 3):
        d = hoje_d - timedelta(days=7 * i)
        p = PedidoLoja(loja_id=forte.id, status='recebido', data_entrega=d,
                       data_pedido=d)
        db.session.add(p)
        db.session.flush()
        db.session.add(PedidoItem(pedido_id=p.id, receita_id=r.id, quantidade=30))
    d1 = hoje_d - timedelta(days=7)
    for m in marginais:
        p = PedidoLoja(loja_id=m.id, status='recebido', data_entrega=d1,
                       data_pedido=d1)
        db.session.add(p)
        db.session.flush()
        db.session.add(PedidoItem(pedido_id=p.id, receita_id=r.id, quantidade=1))
    db.session.commit()

    sug = sugerir_pedidos_semana(horizonte_dias=7, janela_semanas=6)
    com_pedido = set()
    for la in sug['lojas']:
        item = next((it for it in la['dias'][0]['itens']
                     if it['receita_id'] == r.id), None)
        if item and item['qtd'] > 0:
            com_pedido.add(la['loja_id'])
    assert com_pedido == {forte.id}, (
        f'so a loja forte deveria entrar (sem picado): {com_pedido}')
