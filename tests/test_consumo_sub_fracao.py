"""Acumulador de fração no consumo de sub-receita (03/07/2026).

Caso Massa para folhar: o padeiro conta em BOLAS inteiras (1 bola = 3.580g),
mas a batida de 50 croissants consome 1,26 bola (90g/un). O `round()` por
lote sumia/sobrava ~meia bola por dia; agora floor(consumo + acumulado)
baixa inteiros e a fração fica em `ConsumoSubFracao` pra próxima produção —
exato no longo prazo. Consumo inteiro (almond 1:1) nunca cria fração.
"""
import pytest

from app.extensions import db
from app.models import (
    ConsumoSubFracao,
    EstoqueProducao,
    Receita,
    ReceitaIngrediente,
)


@pytest.fixture(autouse=True)
def _hoje_e_segunda_fixa(congela_hoje):
    """Producao so seg-sex (dono 17/08/2026) tornou o shaping do cronograma
    sensivel ao dia da semana — congela hoje() numa SEGUNDA pros cenarios
    hoje()+N deste arquivo cairem sempre em dia util, em qualquer dia em que
    a suite rode (ver conftest.congela_hoje)."""
    congela_hoje()


def _setup(porcentagem, rend_pai=50, estoque_sub=10):
    sub = Receita(nome='Bola Massa Frac', rendimento_qtd=1,
                  rendimento_unidade='un', peso_base=2000.0)
    db.session.add(sub)
    db.session.flush()
    pai = Receita(nome='Croissant Frac', rendimento_qtd=rend_pai,
                  rendimento_unidade='un', peso_base=1000.0)
    db.session.add(pai)
    db.session.flush()
    db.session.add(ReceitaIngrediente(
        receita_id=pai.id, tipo='receita', ingrediente_nome=sub.nome,
        porcentagem=porcentagem, sub_receita_id=sub.id))
    ep = EstoqueProducao(receita_id=sub.id, quantidade=estoque_sub)
    db.session.add(ep)
    db.session.commit()
    return pai, sub, ep


def test_fracao_acumula_e_baixa_inteiros(app, admin_user):
    from app.services.producao import consumir_subreceitas_prontas
    with app.app_context():
        # 1,257 bola por batida de 50 -> 0,02514 bola por croissant.
        pai, sub, ep = _setup(porcentagem=1.257)

        consumir_subreceitas_prontas(pai, 100, admin_user.id)
        db.session.commit()
        db.session.refresh(ep)
        assert ep.quantidade == 8                    # floor(2,514) = 2
        frac = ConsumoSubFracao.query.filter_by(receita_id=sub.id).first()
        assert abs(frac.fracao_pendente - 0.514) < 1e-6

        consumir_subreceitas_prontas(pai, 100, admin_user.id)
        db.session.commit()
        db.session.refresh(ep)
        # total 0,514 + 2,514 = 3,028 -> baixa 3; sobra 0,028
        assert ep.quantidade == 5
        db.session.refresh(frac)
        assert abs(frac.fracao_pendente - 0.028) < 1e-6
        # 200 croissants consumiram 5,028 bolas -> 5 baixadas, exato no acumulado.


def test_consumo_inteiro_nao_cria_fracao(app, admin_user):
    """Almond 1:1 (porcentagem = rendimento do pai): consumo inteiro exato,
    comportamento idêntico ao anterior."""
    from app.services.producao import consumir_subreceitas_prontas
    with app.app_context():
        pai, sub, ep = _setup(porcentagem=50, rend_pai=50, estoque_sub=30)
        consumir_subreceitas_prontas(pai, 20, admin_user.id)
        db.session.commit()
        db.session.refresh(ep)
        assert ep.quantidade == 10                   # 20 un x 1:1
        frac = ConsumoSubFracao.query.filter_by(receita_id=sub.id).first()
        assert frac is not None and frac.fracao_pendente == 0.0


def test_fracao_pequena_nao_baixa_nada_ate_fechar(app, admin_user):
    from app.services.producao import consumir_subreceitas_prontas
    with app.app_context():
        pai, sub, ep = _setup(porcentagem=1.257)
        # 10 croissants = 0,2514 bola: nada baixa, tudo acumula.
        consumir_subreceitas_prontas(pai, 10, admin_user.id)
        db.session.commit()
        db.session.refresh(ep)
        assert ep.quantidade == 10
        frac = ConsumoSubFracao.query.filter_by(receita_id=sub.id).first()
        assert abs(frac.fracao_pendente - 0.2514) < 1e-6
        # Mais 30 croissants: total 1,0056 -> baixa 1, sobra ~0,0056.
        consumir_subreceitas_prontas(pai, 30, admin_user.id)
        db.session.commit()
        db.session.refresh(ep)
        assert ep.quantidade == 9
        db.session.refresh(frac)
        assert abs(frac.fracao_pendente - 0.0056) < 1e-6
