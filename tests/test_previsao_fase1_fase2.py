"""Refino dos motores de previsão — Fases 1 e 2 (02/07/2026).

Fase 1 (pedido loja→indústria):
- 1b media_semanal_pedidos: exclui rascunho abandonado (anti auto-reforço),
  usa quantidade_recebida, capa pico isolado.
- 1a sugerir_pedidos_por_venda: venda manual conta, estorno subtrai, merma
  estrutural (devolução/perda) projeta consumo (sobra NÃO), minimo_pedido é
  piso, seguranca_pct adiciona colchão.

Fase 2 (ordem de produção):
- balanço: demanda = Σ_dia max(firme_d, previsto_d) (não o max agregado);
  produção já mandada (WIP) abate o produzir com offset>=1 e NÃO mexe no
  cronograma (offset 0).
"""
from datetime import datetime, time, timedelta

import pytest

from app.extensions import db
from app.models import (
    EstoqueLoja,
    Loja,
    MovEstoqueLoja,
    PedidoItem,
    PedidoLoja,
    PlanejamentoItem,
    PlanejamentoProducao,
    Receita,
)
from app.services.previsao_producao import (
    balanco_industria,
    media_semanal_pedidos,
    sugerir_pedidos_por_venda,
)
from app.utils import hoje


@pytest.fixture(autouse=True)
def _hoje_e_segunda_fixa(congela_hoje):
    """Producao seg-sex + janela semanal tornaram o motor weekday-sensivel
    — congela numa SEGUNDA fixa (mesma fixture dos arquivos do cronograma;
    caso real 19/08/2026: test_cronograma_edit quebrou na QUARTA porque o
    indice 3 do grid caiu no sabado bloqueado)."""
    congela_hoje()



def _receita(nome='Croissant', **kw):
    r = Receita(nome=nome, categoria='Paes', rendimento_qtd=1,
                rendimento_unidade='un', peso_base=100.0, **kw)
    db.session.add(r)
    db.session.commit()
    return r


def _loja(nome='Loja A'):
    loja = Loja(nome=nome, ativa=True)
    db.session.add(loja)
    db.session.commit()
    return loja


def _pedido(loja, data_entrega, receita, qtd, status='recebido',
            observacao=None, qtd_recebida=None):
    p = PedidoLoja(loja_id=loja.id, status=status, data_entrega=data_entrega,
                   data_pedido=data_entrega, observacao=observacao)
    db.session.add(p)
    db.session.flush()
    db.session.add(PedidoItem(pedido_id=p.id, receita_id=receita.id,
                              quantidade=qtd, quantidade_recebida=qtd_recebida))
    db.session.commit()
    return p


def _estoque(loja, receita, qtd):
    el = EstoqueLoja(loja_id=loja.id, receita_id=receita.id, quantidade=qtd)
    db.session.add(el)
    db.session.commit()
    return el


def _mov(el, data, qtd, tipo):
    db.session.add(MovEstoqueLoja(
        estoque_loja_id=el.id, tipo=tipo, quantidade=qtd,
        data=datetime.combine(data, time(12, 0)), referencia='teste'))
    db.session.commit()


def _prod(grade, loja_id, rid):
    loja = next((e for e in grade['lojas'] if e['loja_id'] == loja_id), None)
    return None if loja is None else next(
        (p for p in loja['produtos'] if p['receita_id'] == rid), None)


def _prox_dow(dow=0):
    """Próxima data futura com o dia-da-semana pedido (0=segunda)."""
    d = hoje()
    while d.weekday() != dow:
        d += timedelta(days=1)
    return d


# ── Fase 1.1 — média de pedidos (1b) ─────────────────────────────────────

def test_rascunho_abandonado_fora_da_media(app):
    """Anti auto-reforço: pedido gerado pela grade (observacao 'Gerado do
    histórico...') que continua 'pendente' com a entrega no passado NÃO entra
    na média — sem isso a média re-aprende o que a média criou."""
    loja = _loja()
    r = _receita()
    hoje_d = hoje()
    obs = 'Gerado do histórico (rascunho) — revisar e confirmar.'
    for sem in (1, 2, 3):
        _pedido(loja, hoje_d - timedelta(days=7 * sem), r, 500,
                status='pendente', observacao=obs)

    grade = media_semanal_pedidos(horizonte_dias=7, janela_semanas=6)
    assert _prod(grade, loja.id, r.id) is None   # só rascunho fantasma: some

    # O MESMO pedido confirmado pelo humano volta a contar.
    _pedido(loja, hoje_d - timedelta(days=7), r, 100,
            status='confirmado', observacao=obs)
    grade = media_semanal_pedidos(horizonte_dias=7, janela_semanas=6)
    p = _prod(grade, loja.id, r.id)
    assert p is not None
    assert p['media_semanal'] == 100.0


def test_quantidade_recebida_prevalece_na_media(app):
    """Divergência conferida na entrega (quantidade_recebida) é o sinal mais
    real — prevalece sobre o pedido digitado."""
    loja = _loja()
    r = _receita()
    hoje_d = hoje()
    for sem in (1, 2):
        _pedido(loja, hoje_d - timedelta(days=7 * sem), r, 100,
                qtd_recebida=60)

    grade = media_semanal_pedidos(horizonte_dias=7, janela_semanas=6)
    p = _prod(grade, loja.id, r.id)
    assert p['media_semanal'] == 60.0


def test_pico_isolado_capado_na_media_manual(app):
    """Um pedido gigante avulso (evento) não domina a média do dia-da-semana:
    o cap na 2ª maior ocorrência (mesma proteção do balanço) entra em ação."""
    loja = _loja()
    r = _receita()
    hoje_d = hoje()
    # mesmo dow: 10, 10 e um pico de 100 (mais antigo) -> capado em 10.
    for sem, q in ((1, 10), (2, 10), (3, 100)):
        _pedido(loja, hoje_d - timedelta(days=7 * sem), r, q)

    grade = media_semanal_pedidos(horizonte_dias=7, janela_semanas=6)
    p = _prod(grade, loja.id, r.id)
    assert p['media_semanal'] == 10.0


# ── Fase 1.2 — venda + estoque (1a) ──────────────────────────────────────

def test_venda_manual_conta_na_demanda(app):
    """Tipo 'venda' (tela de estoque, loja sem PDV) entra na demanda — antes
    ficava fora e a loja era sistematicamente sub-pedida."""
    loja = _loja()
    r = _receita('Bolo')
    el = _estoque(loja, r, 0)
    alvo = _prox_dow(0)
    for sem in range(1, 4):
        _mov(el, alvo - timedelta(days=7 * sem), 10, 'venda')

    grade = sugerir_pedidos_por_venda(
        horizonte_dias=7, janela_semanas=6,
        inicio_offset_dias=(alvo - hoje()).days)
    p = _prod(grade, loja.id, r.id)
    assert p is not None
    assert p['por_dia'][0] == 10


def test_estorno_seru_subtrai_da_demanda(app):
    """Venda Seru cancelada não infla a média: o estorno (gravado positivo no
    ledger) entra com sinal -1 na demanda."""
    loja = _loja()
    r = _receita('Pao')
    el = _estoque(loja, r, 0)
    alvo = _prox_dow(0)
    d = alvo - timedelta(days=7)
    _mov(el, d, 30, 'venda_seru')
    _mov(el, d, 30, 'venda_seru_estorno')   # cancelamento total

    grade = sugerir_pedidos_por_venda(
        horizonte_dias=7, janela_semanas=6,
        inicio_offset_dias=(alvo - hoje()).days)
    p = _prod(grade, loja.id, r.id)
    assert p is not None
    assert p['media_semanal'] == 0.0
    assert sum(p['por_dia']) == 0


def test_merma_estrutural_projeta_consumo_sobra_nao(app):
    """Devolução à indústria recorrente consome o estoque projetado (croissant
    devolvido pra virar Almond era sub-pedido); 'sobra' NÃO projeta (excesso
    não se repõe — incluir perpetuaria o desperdício)."""
    loja = _loja()
    r_dev = _receita('Croissant Tradicional')
    r_sobra = _receita('Bolo do Dia')
    el_dev = _estoque(loja, r_dev, 0)
    el_sobra = _estoque(loja, r_sobra, 0)
    alvo = _prox_dow(0)
    for sem in range(1, 4):
        _mov(el_dev, alvo - timedelta(days=7 * sem), 10, 'devolucao_industria')
        _mov(el_sobra, alvo - timedelta(days=7 * sem), 10, 'sobra')

    grade = sugerir_pedidos_por_venda(
        horizonte_dias=7, janela_semanas=6,
        inicio_offset_dias=(alvo - hoje()).days)
    p_dev = _prod(grade, loja.id, r_dev.id)
    assert p_dev is not None
    assert p_dev['por_dia'][0] == 10        # devolução consome -> repõe
    assert p_dev['media_semanal'] == 0.0    # coluna Venda/sem = só venda
    p_sobra = _prod(grade, loja.id, r_sobra.id)
    # 'sobra' não entra na demanda: se o item nem aparece (sem venda/estoque/
    # pedido) ou aparece zerado, nada é sugerido.
    assert p_sobra is None or sum(p_sobra['por_dia']) == 0


def test_minimo_pedido_e_piso_do_dia(app):
    """minimo_pedido cadastrado vira piso do pedido do dia; o excedente cobre
    os dias seguintes (carry)."""
    loja = _loja()
    r = _receita('Croissant', minimo_pedido=25)
    el = _estoque(loja, r, 0)
    alvo = _prox_dow(0)
    for sem in range(1, 4):
        _mov(el, alvo - timedelta(days=7 * sem), 10, 'venda_seru')

    grade = sugerir_pedidos_por_venda(
        horizonte_dias=7, janela_semanas=6,
        inicio_offset_dias=(alvo - hoje()).days)
    p = _prod(grade, loja.id, r.id)
    assert p['por_dia'][0] == 25            # deficit 10 -> elevado ao piso


def test_seguranca_pct_adiciona_colchao(app):
    """seguranca_pct=50: o pedido cobre o consumo do dia + 50% de colchão."""
    loja = _loja()
    r = _receita('Pao')
    el = _estoque(loja, r, 0)
    alvo = _prox_dow(0)
    for sem in range(1, 4):
        _mov(el, alvo - timedelta(days=7 * sem), 10, 'venda_seru')

    grade = sugerir_pedidos_por_venda(
        horizonte_dias=7, janela_semanas=6,
        inicio_offset_dias=(alvo - hoje()).days, seguranca_pct=50)
    p = _prod(grade, loja.id, r.id)
    assert p['por_dia'][0] == 15            # 10 * 1.5


# ── Fase 2 — ordem de produção ───────────────────────────────────────────

def test_demanda_soma_max_por_dia_nao_agregado(app):
    """Σ_dia max(firme_d, previsto_d) > max(Σfirme, Σprevisto) quando um dia
    firme veio ACIMA da média e os outros virão NA média — o max agregado
    subproduzia exatamente essa diferença."""
    loja = _loja()
    r = _receita()
    hoje_d = hoje()
    amanha = hoje_d + timedelta(days=1)
    # Histórico: 10 toda semana no dow de (amanhã+1) — previsto ~10 nesse dow.
    dow_prev = (amanha + timedelta(days=1))
    for sem in (1, 2, 3):
        _pedido(loja, dow_prev - timedelta(days=7 * sem), r, 10)
    # Firme AMANHÃ (outro dow, sem histórico): 50 — bem acima de qualquer média.
    _pedido(loja, amanha, r, 50, status='pendente')

    bal = balanco_industria(horizonte_dias=7, janela_semanas=6,
                            usar_cache=False, inicio_offset_dias=1)
    it = next(i for i in bal['itens'] if i['receita_id'] == r.id)
    # max agregado seria max(50, ~10-20) = 50; a soma por dia é 50 + ~10 = ~60.
    assert it['demanda'] > max(it['comprometido'], it['previsto'])
    assert it['produzir'] == it['demanda']   # estoque 0


def test_wip_abate_produzir_no_painel_offset1(app):
    """Plano de HOJE já enviado ao padeiro é suprimento a caminho: com
    offset=1 (painel), o produzir de amanhã desconta o que está no forno."""
    loja = _loja()
    r = _receita()
    hoje_d = hoje()
    _pedido(loja, hoje_d + timedelta(days=2), r, 30, status='pendente')
    plano = PlanejamentoProducao(data=hoje_d, nome='Plano hoje',
                                 status='aprovado', origem='cronograma',
                                 enviado_ao_padeiro=True)
    db.session.add(plano)
    db.session.flush()
    db.session.add(PlanejamentoItem(planejamento_id=plano.id, receita_id=r.id,
                                    multiplicador=1, qtd_alvo=30,
                                    produzido_qtd=0))
    db.session.commit()

    bal = balanco_industria(horizonte_dias=7, janela_semanas=6,
                            usar_cache=False, inicio_offset_dias=1)
    it = next(i for i in bal['itens'] if i['receita_id'] == r.id)
    assert it['em_producao'] == 30
    assert it['produzir'] == 0               # os 30 já estão no forno


def test_wip_nao_mexe_no_cronograma_offset0(app):
    """Com offset=0 (cronograma) o plano de hoje NÃO é descontado — o grid de
    hoje é a fonte do próprio plano; descontar zeraria o grid após o envio."""
    loja = _loja()
    r = _receita()
    hoje_d = hoje()
    _pedido(loja, hoje_d + timedelta(days=2), r, 30, status='pendente')
    plano = PlanejamentoProducao(data=hoje_d, nome='Plano hoje',
                                 status='aprovado', origem='cronograma',
                                 enviado_ao_padeiro=True)
    db.session.add(plano)
    db.session.flush()
    db.session.add(PlanejamentoItem(planejamento_id=plano.id, receita_id=r.id,
                                    multiplicador=1, qtd_alvo=30,
                                    produzido_qtd=0))
    db.session.commit()

    bal = balanco_industria(horizonte_dias=7, janela_semanas=6,
                            usar_cache=False, inicio_offset_dias=0)
    it = next(i for i in bal['itens'] if i['receita_id'] == r.id)
    assert it['em_producao'] == 0
    assert it['produzir'] == 30


def test_wip_parcialmente_produzido_abate_so_o_que_falta(app):
    """WIP = alvo - produzido: o que o padeiro JÁ confirmou virou estoque real
    (não conta duas vezes)."""
    loja = _loja()
    r = _receita()
    hoje_d = hoje()
    _pedido(loja, hoje_d + timedelta(days=2), r, 30, status='pendente')
    plano = PlanejamentoProducao(data=hoje_d, nome='Plano hoje',
                                 status='aprovado', origem='cronograma',
                                 enviado_ao_padeiro=True)
    db.session.add(plano)
    db.session.flush()
    db.session.add(PlanejamentoItem(planejamento_id=plano.id, receita_id=r.id,
                                    multiplicador=1, qtd_alvo=30,
                                    produzido_qtd=20))
    db.session.commit()

    bal = balanco_industria(horizonte_dias=7, janela_semanas=6,
                            usar_cache=False, inicio_offset_dias=1)
    it = next(i for i in bal['itens'] if i['receita_id'] == r.id)
    assert it['em_producao'] == 10           # só o que ainda falta produzir


def test_mp_caixa_e_minimo_no_pedido(app):
    """MP pedida pela loja com lote/minimo cadastrados: a sugestão fecha na
    caixa e respeita o piso (antes MP saía picada, un a un)."""
    from app.models import MateriaPrima

    loja = _loja()
    mp = MateriaPrima(nome='Pão de Queijo Congelado', unidade='un',
                      custo_por_kg=0.5, sugerir_pedido_loja=True,
                      lote_pedido=50, minimo_pedido=100)
    db.session.add(mp)
    db.session.commit()
    el = EstoqueLoja(loja_id=loja.id, materia_prima_id=mp.id, quantidade=0)
    db.session.add(el)
    db.session.commit()
    alvo = _prox_dow(0)
    for sem in range(1, 4):
        _mov(el, alvo - timedelta(days=7 * sem), 30, 'venda_seru')

    grade = sugerir_pedidos_por_venda(
        horizonte_dias=7, janela_semanas=6,
        inicio_offset_dias=(alvo - hoje()).days)
    loja_out = next(e for e in grade['lojas'] if e['loja_id'] == loja.id)
    p = next(x for x in loja_out['produtos']
             if x['materia_prima_id'] == mp.id)
    assert p['lote'] == 50
    assert p['minimo'] == 100
    # déficit 30 -> caixa 50 -> piso 100 (múltiplo da caixa)
    assert p['por_dia'][0] == 100


def test_banco_mp_salva_lote_e_minimo(app, admin_user):
    """O form do banco de MPs persiste lote_pedido/minimo_pedido."""
    from app.models import MateriaPrima

    mp = MateriaPrima(nome='Cone', unidade='un', custo_por_kg=1.0)
    db.session.add(mp)
    db.session.commit()

    client = app.test_client()
    client.post('/auth/login', data={'login': admin_user.login, 'senha': '123'})
    resp = client.post('/materias-primas/salvar', data={
        'mp_id[]': [str(mp.id)],
        'nome[]': ['Cone'],
        'unidade[]': ['un'],
        'custo_por_kg[]': ['1.00'],
        'peso_unidade[]': [''],
        'fornecedor[]': [''],
        'observacoes[]': [''],
        'lote_pedido[]': ['24'],
        'minimo_pedido[]': ['48'],
        'sugerir_loja_ids': [str(mp.id)],
    })
    assert resp.status_code in (200, 302)
    db.session.refresh(mp)
    assert mp.lote_pedido == 24
    assert mp.minimo_pedido == 48
    assert mp.sugerir_pedido_loja is True
