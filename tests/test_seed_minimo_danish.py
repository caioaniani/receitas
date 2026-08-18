"""Danishes ASSADAS: 2 por loja POR DIA, "impreterivelmente" (dono
17/08/2026). O v1 (colchão de estoque) foi convertido pelo v2 no piso
INCONDICIONAL `pedido_minimo_diario` — a loja recebe 2 de cada TODO dia,
sem descontar o estoque que sobrou; a média de venda manda quando passa do
piso. Seeds rodam UMA vez (markers em AppConfig), nunca sobrescrevem valor
do dono e ignoram Industria/loja de funcionamento restrito."""

import pytest

from app.extensions import db
from app.migrations_legacy import (
    SEED_MINIMO_DANISH,
    _seed_minimo_danish,
)
from app.models import AppConfig, EstoqueLoja, Loja, Receita


@pytest.fixture(autouse=True)
def _hoje_e_segunda_fixa(congela_hoje):
    """O motor de pedidos tem janela semanal e produção seg-sex — congela
    numa SEGUNDA pros cenários não variarem com o dia da suíte."""
    congela_hoje()

NOMES = ['Danish de Calabresa', 'Danish de queijo branco',
         'Danish de Muçarela de Búfala', 'Danish de alho poró',
         'Danish de Maçã']


def _cenario():
    receitas = []
    for nome in NOMES:
        r = Receita(nome=nome, categoria='Danishes', rendimento_qtd=1,
                    rendimento_unidade='un', peso_base=100.0,
                    estado_padrao='assado')
        db.session.add(r)
        receitas.append(r)
    lojas = {
        'anesio': Loja(nome='Anesio', ativa=True),
        'ribeiro': Loja(nome='Ribeiro do Vale', ativa=True),
        'cantina': Loja(nome='Cantina', ativa=True, dias_funcionamento='56'),
        'industria': Loja(nome='Industria', ativa=True),
        'fechada': Loja(nome='Loja Fechada', ativa=False),
    }
    db.session.add_all(lojas.values())
    db.session.commit()
    return receitas, lojas


def _minimos(loja):
    return {el.receita_id: int(el.estoque_minimo or 0)
            for el in EstoqueLoja.query.filter_by(loja_id=loja.id)}


def test_seed_poe_minimo_2_nas_lojas_diarias(app):
    with app.app_context():
        receitas, lojas = _cenario()
        _seed_minimo_danish(app)
        for chave in ('anesio', 'ribeiro'):
            mins = _minimos(lojas[chave])
            assert len(mins) == 5
            assert all(v == 2 for v in mins.values())
        assert AppConfig.get(SEED_MINIMO_DANISH['chave'])


def test_seed_ignora_industria_cantina_e_inativa(app):
    """Industria (Loja só de RH), loja de funcionamento restrito (Cantina,
    sáb/dom — colchão diário não se aplica) e loja inativa ficam fora."""
    with app.app_context():
        _, lojas = _cenario()
        _seed_minimo_danish(app)
        for chave in ('cantina', 'industria', 'fechada'):
            assert _minimos(lojas[chave]) == {}


def test_seed_nao_sobrescreve_minimo_do_dono(app):
    """Mínimo já definido (> 0) é do dono — o seed mantém."""
    with app.app_context():
        receitas, lojas = _cenario()
        db.session.add(EstoqueLoja(loja_id=lojas['anesio'].id,
                                   receita_id=receitas[0].id,
                                   quantidade=3, estoque_minimo=5))
        db.session.commit()
        _seed_minimo_danish(app)
        mins = _minimos(lojas['anesio'])
        assert mins[receitas[0].id] == 5           # valor do dono mantido
        assert sum(1 for v in mins.values() if v == 2) == 4


def test_seed_reusa_linha_existente_sem_duplicar(app):
    with app.app_context():
        receitas, lojas = _cenario()
        db.session.add(EstoqueLoja(loja_id=lojas['anesio'].id,
                                   receita_id=receitas[1].id, quantidade=7))
        db.session.commit()
        _seed_minimo_danish(app)
        linhas = EstoqueLoja.query.filter_by(
            loja_id=lojas['anesio'].id, receita_id=receitas[1].id).all()
        assert len(linhas) == 1                    # reusa, não duplica
        assert int(linhas[0].quantidade) == 7      # saldo físico intocado
        assert int(linhas[0].estoque_minimo) == 2


def test_seed_roda_uma_vez(app):
    """Marker: segunda execução é no-op — apagar o mínimo depois é decisão
    do dono e o seed não ressuscita."""
    with app.app_context():
        receitas, lojas = _cenario()
        _seed_minimo_danish(app)
        el = EstoqueLoja.query.filter_by(loja_id=lojas['anesio'].id,
                                         receita_id=receitas[0].id).one()
        el.estoque_minimo = None                   # dono tirou o piso
        db.session.commit()
        _seed_minimo_danish(app)
        db.session.refresh(el)
        assert el.estoque_minimo is None           # seed não ressuscitou


def test_receita_arquivada_fica_fora(app):
    with app.app_context():
        receitas, lojas = _cenario()
        from app.utils import agora
        receitas[2].arquivada_em = agora()
        db.session.commit()
        _seed_minimo_danish(app)
        mins = _minimos(lojas['anesio'])
        assert receitas[2].id not in mins
        assert len(mins) == 4


# ── v2: o colchão vira piso INCONDICIONAL (pedido_minimo_diario) ────────

def test_v2_converte_colchao_em_piso_diario(app):
    """O v2 seta pedido_minimo_diario=2 e LIMPA o estoque_minimo que o v1
    deixou em exatamente 2; colchão diferente (ajuste do dono) fica."""
    from app.migrations_legacy import _seed_minimo_danish_v2
    with app.app_context():
        receitas, lojas = _cenario()
        _seed_minimo_danish(app)                     # v1: colchão 2
        el0 = EstoqueLoja.query.filter_by(loja_id=lojas['anesio'].id,
                                          receita_id=receitas[0].id).one()
        el0.estoque_minimo = 5                       # dono ajustou depois
        db.session.commit()
        _seed_minimo_danish_v2(app)
        for chave in ('anesio', 'ribeiro'):
            for el in EstoqueLoja.query.filter_by(loja_id=lojas[chave].id):
                assert int(el.pedido_minimo_diario or 0) == 2
        db.session.refresh(el0)
        assert el0.estoque_minimo == 5               # ajuste do dono fica
        outros = [el for el in EstoqueLoja.query.filter_by(
            loja_id=lojas['anesio'].id) if el.receita_id != receitas[0].id]
        assert all(el.estoque_minimo is None for el in outros)
        assert AppConfig.get(SEED_MINIMO_DANISH['chave_v2'])


def test_v2_roda_uma_vez_e_nao_sobrescreve(app):
    from app.migrations_legacy import _seed_minimo_danish_v2
    with app.app_context():
        receitas, lojas = _cenario()
        el = EstoqueLoja(loja_id=lojas['anesio'].id,
                         receita_id=receitas[0].id, quantidade=0,
                         pedido_minimo_diario=4)     # valor do dono
        db.session.add(el)
        db.session.commit()
        _seed_minimo_danish_v2(app)
        db.session.refresh(el)
        assert el.pedido_minimo_diario == 4          # mantido
        el.pedido_minimo_diario = None               # dono tirou o piso
        db.session.commit()
        _seed_minimo_danish_v2(app)
        db.session.refresh(el)
        assert el.pedido_minimo_diario is None       # não ressuscita


# ── v3: filtro de loja corrigido ('0123456' da tela = abre todo dia) ────

def test_v3_inclui_loja_com_semana_inteira_gravada(app):
    """CAUSA REAL do setados=0 em prod: a tela de lojas grava '0123456' e o
    filtro antigo só aceitava VAZIO. O v3 trata semana inteira como loja
    diária; restrita de verdade ('56') segue fora; estoque_minimo (ajuste
    manual do dono) não é tocado."""
    from app.migrations_legacy import _seed_minimo_danish_v3
    with app.app_context():
        receitas, lojas = _cenario()
        for chave in ('anesio', 'ribeiro'):
            lojas[chave].dias_funcionamento = '0123456'   # como em prod
        el_manual = EstoqueLoja(loja_id=lojas['anesio'].id,
                                receita_id=receitas[0].id,
                                quantidade=5, estoque_minimo=2)
        db.session.add(el_manual)
        db.session.commit()
        _seed_minimo_danish_v3(app)
        for chave in ('anesio', 'ribeiro'):
            els = EstoqueLoja.query.filter_by(loja_id=lojas[chave].id).all()
            assert len(els) == 5
            assert all(int(e.pedido_minimo_diario or 0) == 2 for e in els)
        for chave in ('cantina', 'industria', 'fechada'):
            assert EstoqueLoja.query.filter_by(
                loja_id=lojas[chave].id).count() == 0
        db.session.refresh(el_manual)
        assert el_manual.estoque_minimo == 2       # ajuste do dono intocado
        marker = AppConfig.get(SEED_MINIMO_DANISH['chave_v3'])
        assert 'setados=10' in marker              # 2 lojas × 5 receitas
        assert 'lojas=2' in marker and 'receitas=5' in marker


def test_v3_nao_sobrescreve_piso_e_roda_uma_vez(app):
    from app.migrations_legacy import _seed_minimo_danish_v3
    with app.app_context():
        receitas, lojas = _cenario()
        el = EstoqueLoja(loja_id=lojas['anesio'].id,
                         receita_id=receitas[0].id, quantidade=0,
                         pedido_minimo_diario=4)   # valor do dono
        db.session.add(el)
        db.session.commit()
        _seed_minimo_danish_v3(app)
        db.session.refresh(el)
        assert el.pedido_minimo_diario == 4        # mantido
        el.pedido_minimo_diario = None             # dono tirou o piso
        db.session.commit()
        _seed_minimo_danish_v3(app)
        db.session.refresh(el)
        assert el.pedido_minimo_diario is None     # marker: não ressuscita


# ── motor: o piso diário é INCONDICIONAL no pedido de cada dia ──────────

def test_motor_pede_2_por_dia_mesmo_com_estoque_sobrando(app, loja):
    """"Receber 2 por dia impreterivelmente": com 10 em estoque e venda
    zero, a sugestão continua 2 em TODOS os dias — o piso diário NÃO
    desconta o estoque (diferente do colchão estoque_minimo)."""
    from app.services.previsao_producao import sugerir_pedidos_por_venda
    with app.app_context():
        r = Receita(nome='Danish Piso', categoria='Danishes',
                    rendimento_qtd=1, rendimento_unidade='un',
                    peso_base=100.0, estado_padrao='assado')
        db.session.add(r)
        db.session.flush()
        db.session.add(EstoqueLoja(loja_id=loja.id, receita_id=r.id,
                                   quantidade=10, pedido_minimo_diario=2))
        db.session.commit()
        sug = sugerir_pedidos_por_venda(horizonte_dias=5,
                                        inicio_offset_dias=1)
        lj = next(x for x in sug['lojas'] if x['loja_id'] == loja.id)
        p = next(x for x in lj['produtos'] if x['receita_id'] == r.id)
        assert p['por_dia'] == [2, 2, 2, 2, 2]
        assert p['pedido_minimo_diario'] == 2


def test_motor_media_manda_acima_do_piso(app, loja):
    """"Se vendeu 2 ou mais... deveria pedir a mais": média de venda de
    5/dia no dia-da-semana supera o piso — o pedido sai 5, não trava em 2."""
    from datetime import datetime as _dt
    from datetime import time as _time
    from datetime import timedelta

    from app.models import MovEstoqueLoja
    from app.services.previsao_producao import sugerir_pedidos_por_venda
    from app.utils import hoje
    with app.app_context():
        r = Receita(nome='Danish Vende', categoria='Danishes',
                    rendimento_qtd=1, rendimento_unidade='un',
                    peso_base=100.0)
        db.session.add(r)
        db.session.flush()
        el = EstoqueLoja(loja_id=loja.id, receita_id=r.id, quantidade=0,
                         pedido_minimo_diario=2)
        db.session.add(el)
        db.session.flush()
        alvo = hoje() + timedelta(days=1)
        for sem in range(1, 5):                      # 4 semanas do mesmo dow
            d = alvo - timedelta(days=7 * sem)
            db.session.add(MovEstoqueLoja(
                estoque_loja_id=el.id, tipo='venda_seru', quantidade=5,
                data=_dt.combine(d, _time(12, 0)), referencia='teste-piso'))
        db.session.commit()
        sug = sugerir_pedidos_por_venda(horizonte_dias=1,
                                        inicio_offset_dias=1)
        lj = next(x for x in sug['lojas'] if x['loja_id'] == loja.id)
        p = next(x for x in lj['produtos'] if x['receita_id'] == r.id)
        assert p['por_dia'][0] >= 5                  # média venceu o piso


# ---------------------------------------------------------------------------
# Cinnamon Roll na mesma regra (dono 17/08/2026: "Esqueci de falar sobre o
# cinnamon Roll, entra na mesma regra dos 2 danishes")
# ---------------------------------------------------------------------------

def _cenario_cinnamon():
    classico = Receita(nome='Cinnamon Roll', categoria='Viennoiserie',
                       rendimento_qtd=1, rendimento_unidade='un',
                       peso_base=100.0)          # estado_padrao vazio (prod)
    doce = Receita(nome='Cinnamon Roll Doce de leite',
                   categoria='Viennoiserie', rendimento_qtd=1,
                   rendimento_unidade='un', peso_base=100.0)
    lojas = {
        'anesio': Loja(nome='Anesio', ativa=True),
        'semana': Loja(nome='Loja Semana Cheia', ativa=True,
                       dias_funcionamento='0123456'),
        'cantina': Loja(nome='Cantina', ativa=True, dias_funcionamento='56'),
        'industria': Loja(nome='Industria', ativa=True),
        'fechada': Loja(nome='Loja Fechada', ativa=False),
    }
    db.session.add_all([classico, doce, *lojas.values()])
    db.session.commit()
    return classico, doce, lojas


def _pisos(loja):
    return {el.receita_id: int(el.pedido_minimo_diario or 0)
            for el in EstoqueLoja.query.filter_by(loja_id=loja.id)}


def test_cinnamon_piso_2_nas_lojas_diarias_e_assado(app):
    from app.migrations_legacy import _seed_minimo_cinnamon
    with app.app_context():
        classico, doce, lojas = _cenario_cinnamon()
        _seed_minimo_cinnamon(app)
        for chave in ('anesio', 'semana'):           # vazio E '0123456'
            assert _pisos(lojas[chave]).get(classico.id) == 2
        for chave in ('cantina', 'industria', 'fechada'):
            assert classico.id not in _pisos(lojas[chave])
        # so o classico — o Doce de leite fica fora (dono citou um)
        assert doce.id not in _pisos(lojas['anesio'])
        # a regra e receber ASSADO: estado_padrao vazio vira 'assado'
        assert classico.estado_padrao == 'assado'
        assert not doce.estado_padrao
        marker = AppConfig.get('seed_minimo_cinnamon_2026_08')
        assert 'setados=2' in marker and 'lojas=2' in marker
        assert 'receitas=1' in marker


def test_cinnamon_nao_sobrescreve_piso_nem_estado_do_dono(app):
    from app.migrations_legacy import _seed_minimo_cinnamon
    with app.app_context():
        classico, _doce, lojas = _cenario_cinnamon()
        classico.estado_padrao = 'backup'            # escolha explicita
        db.session.add(EstoqueLoja(loja_id=lojas['anesio'].id,
                                   receita_id=classico.id, quantidade=0,
                                   pedido_minimo_diario=5))
        db.session.commit()
        _seed_minimo_cinnamon(app)
        assert _pisos(lojas['anesio']).get(classico.id) == 5
        assert classico.estado_padrao == 'backup'
        marker = AppConfig.get('seed_minimo_cinnamon_2026_08')
        assert 'mantidos=1' in marker and 'assado=0' in marker


def test_cinnamon_roda_uma_vez(app):
    from app.migrations_legacy import _seed_minimo_cinnamon
    with app.app_context():
        classico, _doce, lojas = _cenario_cinnamon()
        _seed_minimo_cinnamon(app)
        el = EstoqueLoja.query.filter_by(
            loja_id=lojas['anesio'].id, receita_id=classico.id).first()
        el.pedido_minimo_diario = 9                  # dono mexeu depois
        db.session.commit()
        _seed_minimo_cinnamon(app)                   # 2a rodada = no-op
        db.session.refresh(el)
        assert el.pedido_minimo_diario == 9


def test_seed_antecedencia_brioche(app):
    """Brioche clássico ganha antecedencia_max_dias=0 (fresco máximo);
    homônimo composto fica fora; valor já definido pelo dono é mantido;
    marker grava contagens (setados=0 nunca mais passa batido)."""
    from app.migrations_legacy import _seed_antecedencia_brioche
    with app.app_context():
        b = Receita(nome='Brioche', categoria='Paes', rendimento_qtd=1,
                    rendimento_unidade='un', peso_base=100.0)
        gotas = Receita(nome='Brioche gotas de chocolate 250g',
                        categoria='Paes', rendimento_qtd=1,
                        rendimento_unidade='un', peso_base=100.0)
        db.session.add_all([b, gotas])
        db.session.commit()
        _seed_antecedencia_brioche(app)
        assert b.antecedencia_max_dias == 0
        assert gotas.antecedencia_max_dias is None
        marker = AppConfig.get('seed_antecedencia_brioche_2026_08')
        assert 'setados=1' in marker and 'receitas=1' in marker
        # 2ª rodada é no-op mesmo se o dono mudar depois
        b.antecedencia_max_dias = 2
        db.session.commit()
        _seed_antecedencia_brioche(app)
        db.session.refresh(b)
        assert b.antecedencia_max_dias == 2
