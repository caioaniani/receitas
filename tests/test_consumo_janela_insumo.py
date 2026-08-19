"""Visibilidade do MRP no cronograma (03/07/2026, cobrança do dono).

Editar 10.000 pains e ver a Massa para folhar em 0 parecia "não calculou" —
o MRP calculava, mas (1) o consumo derivado não aparecia em lugar nenhum
quando o estoque cobria (produzir 0) e (2) as linhas de insumo só
recalculavam no F5. Agora a linha do insumo carrega `consumo_janela` e
`editar_celula` devolve `insumos` recalculados pro front atualizar na hora.
"""
import pytest

from app.extensions import db
from app.models import EstoqueProducao, Receita, ReceitaIngrediente
from app.utils import hoje


@pytest.fixture(autouse=True)
def _hoje_e_segunda_fixa(congela_hoje):
    """Producao seg-sex + janela semanal tornaram o motor weekday-sensivel
    — congela numa SEGUNDA fixa (mesma fixture dos arquivos do cronograma;
    caso real 19/08/2026: test_cronograma_edit quebrou na QUARTA porque o
    indice 3 do grid caiu no sabado bloqueado)."""
    congela_hoje()



def _setup(estoque_massa):
    massa = Receita(nome='Massa CJ', rendimento_qtd=1,
                    rendimento_unidade='un', peso_base=2000.0,
                    dias_producao=1, sugerir_pedido_loja=False)
    db.session.add(massa)
    db.session.flush()
    pai = Receita(nome='Croissant CJ', rendimento_qtd=50,
                  rendimento_unidade='un', peso_base=1000.0, dias_producao=1)
    db.session.add(pai)
    db.session.flush()
    db.session.add(ReceitaIngrediente(
        receita_id=pai.id, tipo='receita', ingrediente_nome=massa.nome,
        porcentagem=1.257, sub_receita_id=massa.id))
    db.session.add(EstoqueProducao(receita_id=massa.id,
                                   quantidade=estoque_massa))
    db.session.commit()
    return pai, massa


def test_editar_celula_devolve_insumos_com_consumo(app, admin_user):
    """Cenário exato do dono: estoque de massa alto engole a produção
    (por_dia = 0), mas o consumo derivado aparece — prova que o MRP rodou."""
    from app.services.cronograma_edit import editar_celula
    with app.app_context():
        pai, massa = _setup(estoque_massa=900)
        r = editar_celula(pai.id, hoje().isoformat(), 100)
        assert r is not None
        assert r['total'] == 100
        ins = next((i for i in r['insumos']
                    if i['receita_id'] == massa.id), None)
        assert ins is not None
        # 100 croissants x 1,257/50 = 2,514 bolas -> consumo visivel...
        assert abs(ins['consumo_janela'] - 2.5) < 0.11
        assert ins['em_estoque'] == 900
        # ...mas produzir = 0 (estoque cobre): ANTES parecia "nao calculou".
        assert sum(c['qtd'] for c in ins['por_dia']) == 0


def test_sem_estoque_a_massa_vira_producao(app, admin_user):
    """Sem estoque, o consumo derivado vira produção da massa NA VÉSPERA.
    Regra da véspera (dono, 10/07/2026): consumo de HOJE não agenda massa
    pra hoje (não ficaria pronta a tempo — vira o aviso insumo_sem_vespera,
    ver tests/test_cronograma_ux.py); por isso o pai aqui produz AMANHÃ e
    as bolas caem HOJE."""
    from datetime import timedelta

    from app.services.cronograma_edit import editar_celula
    with app.app_context():
        pai, massa = _setup(estoque_massa=0)
        amanha = hoje() + timedelta(days=1)
        r = editar_celula(pai.id, amanha.isoformat(), 100)
        ins = next(i for i in r['insumos'] if i['receita_id'] == massa.id)
        # ceil(2,514) = 3 bolas programadas, na véspera do pai (hoje).
        assert sum(c['qtd'] for c in ins['por_dia']) == 3
        assert ins['por_dia'][0]['qtd'] == 3


def test_cronograma_marca_consumo_janela_na_linha(app, admin_user):
    from app.services.cronograma_edit import editar_celula
    from app.services.previsao_producao import cronograma_producao
    with app.app_context():
        pai, massa = _setup(estoque_massa=900)
        editar_celula(pai.id, hoje().isoformat(), 100)
        crono = cronograma_producao()
        rr = next(x for x in crono['receitas']
                  if x['receita_id'] == massa.id)
        assert rr.get('insumo') is True
        assert abs(rr['consumo_janela'] - 2.5) < 0.11
