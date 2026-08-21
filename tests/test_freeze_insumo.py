"""Trava POR INSUMO: item cuja massa/levain é batido na véspera fecha ANTES.

Regra do dono (20/08/2026, no caso da ordem que mudou sob o padeiro):
"Inclusive o croissant você tem que passar bem antes porque a massa para
folhar é feita 24 horas antes do modelar o croissant, fica esperto."

Ou seja: aumentar croissant na véspera é inútil — a massa daquele dia já foi
batida com o número antigo e o padeiro recebe ordem impossível. A decisão do
dono (AskUserQuestion, mesma conversa) foi: vale pra TODA receita com
sub-receita de lead (croissant/pain pela massa; pão francês e sourdoughs pelo
levain), e demanda que sobe depois do fechamento **não entra + avisa**.
"""
from datetime import date, timedelta

import pytest

from app.extensions import db


@pytest.fixture(autouse=True)
def _segunda_fixa(congela_hoje):
    """SEGUNDA 17/08/2026 — a antecedência depende do dia da semana (produção
    é seg-sex, então a massa de segunda rola pra sexta). Sem congelar, o teste
    mede coisas diferentes conforme o dia em que a suíte roda."""
    congela_hoje()


def _receita(nome, **kw):
    from app.models import Receita
    base = dict(nome=nome, categoria='Paes', rendimento_qtd=1,
                rendimento_unidade='un', peso_base=100.0)
    base.update(kw)
    return Receita(**base)


@pytest.fixture
def familia_massa(app):
    """Croissant (lead 1) que consome Massa para folhar (lead 1) — a ficha
    real da padaria, reduzida ao essencial."""
    from app.models import ReceitaIngrediente
    massa = _receita('Massa para folhar', dias_producao=1,
                     rendimento_qtd=50, peso_base=3580.0)
    croissant = _receita('Croissant Tradicional', categoria='Croissants',
                         dias_producao=1, rendimento_qtd=50)
    simples = _receita('Pão de Forma', dias_producao=0)
    db.session.add_all([massa, croissant, simples])
    db.session.flush()
    db.session.add(ReceitaIngrediente(
        receita_id=croissant.id, sub_receita_id=massa.id, tipo='receita',
        ingrediente_nome='Massa para folhar', porcentagem=1.0))
    db.session.commit()
    return {'massa': massa, 'croissant': croissant, 'simples': simples}


def _ctx(familia):
    from app.services.previsao_producao import ant_insumo
    receitas = {r.id: r for r in
                [familia['massa'], familia['croissant'], familia['simples']]}
    lead = {rid: int(r.dias_producao or 0) for rid, r in receitas.items()}
    return ant_insumo, receitas, lead


# ── A função pura ────────────────────────────────────────────────────────

def test_croissant_numa_terca_fecha_um_dia_antes(app, familia_massa):
    """Terça: a massa sai na segunda (dia útil anterior) → antecedência 1."""
    with app.app_context():
        ant_insumo, receitas, lead = _ctx(familia_massa)
        terca = date(2026, 8, 18)
        assert ant_insumo(familia_massa['croissant'].id, terca,
                          receitas, lead) == 1


def test_croissant_numa_SEGUNDA_fecha_tres_dias_antes(app, familia_massa):
    """Segunda: a massa cairia no domingo, que é dia SEM produção — rola pra
    sexta, então a decisão precisa estar fechada 3 dias antes. É o caso que
    mais surpreende, e o que justifica medir em dias de calendário."""
    with app.app_context():
        ant_insumo, receitas, lead = _ctx(familia_massa)
        segunda = date(2026, 8, 24)
        assert ant_insumo(familia_massa['croissant'].id, segunda,
                          receitas, lead) == 3


def test_item_sem_sub_receita_nao_congela(app, familia_massa):
    """Quem não depende de massa/levain segue podendo ser ajustado na
    véspera — é o que o 🔄 das 19:05 existe pra fazer."""
    with app.app_context():
        ant_insumo, receitas, lead = _ctx(familia_massa)
        assert ant_insumo(familia_massa['simples'].id, date(2026, 8, 19),
                          receitas, lead) == 0


def test_a_propria_massa_nao_congela(app, familia_massa):
    """A massa é o insumo, não o dependente: ela mesma fecha na véspera."""
    with app.app_context():
        ant_insumo, receitas, lead = _ctx(familia_massa)
        assert ant_insumo(familia_massa['massa'].id, date(2026, 8, 19),
                          receitas, lead) == 0


def test_ciclo_na_ficha_nao_trava_o_motor(app, familia_massa):
    """Ficha com A→B→A é erro de cadastro; não pode derrubar a produção."""
    from app.models import ReceitaIngrediente
    with app.app_context():
        db.session.add(ReceitaIngrediente(
            receita_id=familia_massa['massa'].id,
            sub_receita_id=familia_massa['croissant'].id, tipo='receita',
            ingrediente_nome='Croissant Tradicional', porcentagem=1.0))
        db.session.commit()
        ant_insumo, receitas, lead = _ctx(familia_massa)
        v = ant_insumo(familia_massa['croissant'].id, date(2026, 8, 19),
                       receitas, lead)
        assert isinstance(v, int) and v >= 0


def test_teto_de_sanidade(app):
    """Lead absurdo na ficha não congela a semana inteira."""
    from app.models import ReceitaIngrediente
    from app.services.previsao_producao import _ANT_INSUMO_MAX, ant_insumo
    with app.app_context():
        sub = _receita('Fermento eterno', dias_producao=99)
        pai = _receita('Pão do fermento eterno')
        db.session.add_all([sub, pai])
        db.session.flush()
        db.session.add(ReceitaIngrediente(
            receita_id=pai.id, sub_receita_id=sub.id, tipo='receita',
            ingrediente_nome='Fermento eterno', porcentagem=1.0))
        db.session.commit()
        receitas = {sub.id: sub, pai.id: pai}
        lead = {sub.id: 99, pai.id: 0}
        assert ant_insumo(pai.id, date(2026, 8, 19), receitas,
                          lead) == _ANT_INSUMO_MAX


# ── A trava no escritor da ordem ─────────────────────────────────────────

def _ordem(data, itens):
    """Ordem enviada pelo cron com os itens dados [(receita, qtd)]."""
    from app.models import PlanejamentoItem, PlanejamentoProducao
    plano = PlanejamentoProducao(data=data, origem='cronograma',
                                 enviado_ao_padeiro=True, criado_por=None,
                                 nome=f'Ordem {data:%d/%m}')
    db.session.add(plano)
    db.session.flush()
    for rec, qtd in itens:
        db.session.add(PlanejamentoItem(planejamento_id=plano.id,
                                        receita_id=rec.id, multiplicador=1,
                                        qtd_alvo=qtd))
    db.session.commit()
    return plano


def _crono_falso(dia, pares):
    """Grid mínimo no formato que `_sync_itens_do_cronograma` consome."""
    return {'receitas': [
        {'receita_id': rec.id, 'retorno': False,
         'por_dia': [{'data': dia.isoformat(), 'qtd': qtd}]}
        for rec, qtd in pares]}


def test_automatico_NAO_sobe_croissant_da_vespera(app, familia_massa):
    """O caso do dono: amanhã (terça) o croissant já está fechado porque a
    massa dele é batida hoje. O grid pede 400, a ordem fica com 250."""
    from app.models import PlanejamentoItem
    from app.services import producao
    from app.utils import hoje
    with app.app_context():
        amanha = hoje() + timedelta(days=1)
        croi = familia_massa['croissant']
        plano = _ordem(amanha, [(croi, 250)])
        n, congelados = producao._sync_itens_do_cronograma(
            plano, amanha, 7, 6, 0, False, motor='vendas', automatico=True,
            crono=_crono_falso(amanha, [(croi, 400)]))
        db.session.commit()
        item = PlanejamentoItem.query.filter_by(planejamento_id=plano.id,
                                                receita_id=croi.id).one()
        assert item.qtd_alvo == 250                      # não subiu
        assert n == 1
        assert congelados and congelados[0][0] == 'Croissant Tradicional'
        assert congelados[0][1] == 250 and congelados[0][2] == 400


def test_automatico_AINDA_ajusta_item_sem_insumo(app, familia_massa):
    """O que se resolve no próprio turno continua sendo corrigido — se tudo
    congelasse, o 🔄 perderia a razão de existir."""
    from app.models import PlanejamentoItem
    from app.services import producao
    from app.utils import hoje
    with app.app_context():
        amanha = hoje() + timedelta(days=1)
        simples = familia_massa['simples']
        plano = _ordem(amanha, [(simples, 100)])
        producao._sync_itens_do_cronograma(
            plano, amanha, 7, 6, 0, False, motor='vendas', automatico=True,
            crono=_crono_falso(amanha, [(simples, 180)]))
        db.session.commit()
        item = PlanejamentoItem.query.filter_by(planejamento_id=plano.id,
                                                receita_id=simples.id).one()
        assert item.qtd_alvo == 180


def test_humano_passa_por_cima_da_trava(app, familia_massa):
    """A escapatória: o dono decide na tela, conscientemente."""
    from app.models import PlanejamentoItem
    from app.services import producao
    from app.utils import hoje
    with app.app_context():
        amanha = hoje() + timedelta(days=1)
        croi = familia_massa['croissant']
        plano = _ordem(amanha, [(croi, 250)])
        producao._sync_itens_do_cronograma(
            plano, amanha, 7, 6, 0, False, motor='vendas', automatico=False,
            crono=_crono_falso(amanha, [(croi, 400)]))
        db.session.commit()
        item = PlanejamentoItem.query.filter_by(planejamento_id=plano.id,
                                                receita_id=croi.id).one()
        assert item.qtd_alvo == 400


def test_croissant_novo_NAO_nasce_na_vespera(app, familia_massa):
    """Decisão do dono: demanda tardia NÃO entra (croissant sem massa seria
    ordem impossível) — e vira aviso."""
    from app.models import PlanejamentoItem
    from app.services import producao
    from app.utils import hoje
    with app.app_context():
        amanha = hoje() + timedelta(days=1)
        croi, simples = familia_massa['croissant'], familia_massa['simples']
        plano = _ordem(amanha, [(simples, 100)])
        _n, congelados = producao._sync_itens_do_cronograma(
            plano, amanha, 7, 6, 0, False, motor='vendas', automatico=True,
            crono=_crono_falso(amanha, [(simples, 100), (croi, 300)]))
        db.session.commit()
        assert PlanejamentoItem.query.filter_by(
            planejamento_id=plano.id, receita_id=croi.id).first() is None
        assert any(c[0] == 'Croissant Tradicional' for c in congelados)


def test_item_congelado_nao_e_removido_da_ordem(app, familia_massa):
    """Grid que zera o croissant na última hora não pode apagar a linha: a
    massa já foi batida e o padeiro conta com ela."""
    from app.models import PlanejamentoItem
    from app.services import producao
    from app.utils import hoje
    with app.app_context():
        amanha = hoje() + timedelta(days=1)
        croi = familia_massa['croissant']
        plano = _ordem(amanha, [(croi, 250)])
        producao._sync_itens_do_cronograma(
            plano, amanha, 7, 6, 0, False, motor='vendas', automatico=True,
            crono=_crono_falso(amanha, []))
        db.session.commit()
        item = PlanejamentoItem.query.filter_by(planejamento_id=plano.id,
                                                receita_id=croi.id).one()
        assert item.qtd_alvo == 250


def test_dia_distante_segue_livre(app, familia_massa):
    """Longe do dia, tudo ainda muda — a trava é só na janela do insumo."""
    from app.models import PlanejamentoItem
    from app.services import producao
    from app.utils import hoje
    with app.app_context():
        longe = hoje() + timedelta(days=6)
        croi = familia_massa['croissant']
        plano = _ordem(longe, [(croi, 250)])
        producao._sync_itens_do_cronograma(
            plano, longe, 7, 6, 0, False, motor='vendas', automatico=True,
            crono=_crono_falso(longe, [(croi, 400)]))
        db.session.commit()
        item = PlanejamentoItem.query.filter_by(planejamento_id=plano.id,
                                                receita_id=croi.id).one()
        assert item.qtd_alvo == 400


def test_kill_switch_desliga(app, familia_massa, monkeypatch):
    from app.models import PlanejamentoItem
    from app.services import producao
    from app.utils import hoje
    monkeypatch.setenv('FREEZE_INSUMO', '0')
    with app.app_context():
        amanha = hoje() + timedelta(days=1)
        croi = familia_massa['croissant']
        plano = _ordem(amanha, [(croi, 250)])
        producao._sync_itens_do_cronograma(
            plano, amanha, 7, 6, 0, False, motor='vendas', automatico=True,
            crono=_crono_falso(amanha, [(croi, 400)]))
        db.session.commit()
        item = PlanejamentoItem.query.filter_by(planejamento_id=plano.id,
                                                receita_id=croi.id).one()
        assert item.qtd_alvo == 400


def test_aviso_fica_registrado_pro_dono(app, familia_massa):
    """"Não entra + AVISA": o que foi segurado tem que aparecer pra ele —
    silêncio esconderia venda que o sistema deixou de atender."""
    from app.services import producao
    from app.utils import hoje
    with app.app_context():
        amanha = hoje() + timedelta(days=1)
        croi = familia_massa['croissant']
        plano = _ordem(amanha, [(croi, 250)])
        producao.enviar_plano_do_dia(
            amanha, user_id=None, motor='vendas',
            crono=_crono_falso(amanha, [(croi, 400)]))
        pendentes = producao.itens_congelados_pendentes()
        assert pendentes and pendentes[0][0] == amanha.isoformat()
        item = pendentes[0][1][0]
        assert item['item'] == 'Croissant Tradicional'
        assert item['ordem'] == 250 and item['grid'] == 400
