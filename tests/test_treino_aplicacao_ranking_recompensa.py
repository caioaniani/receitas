"""Fases 4-6 — aplicação prática, ranking e recompensas.
Critérios de aceite 12, 13, 14, 17, 19.
"""
from datetime import timedelta

import pytest

from app.extensions import db
from app.models import (
    Funcionario,
    Loja,
    TreinoEventoPontos,
    TreinoRecompensa,
    TreinoTemporada,
    TreinoTrilha,
)
from app.services import treino_aplicacao as ap
from app.services import treino_ledger as ledger
from app.services import treino_lideranca as lideranca
from app.services import treino_ranking as rk
from app.services import treino_recompensa as rc
from app.utils import hoje


def _temp():
    t = TreinoTemporada(nome='T', inicio=hoje() - timedelta(days=1),
                        fim=hoje() + timedelta(days=30), status='ATIVA')
    db.session.add(t)
    db.session.commit()
    return t


def _loja(nome):
    lj = Loja(nome=nome, ativa=True)
    db.session.add(lj)
    db.session.commit()
    return lj


def _func(nome, cpf, loja):
    f = Funcionario(nome=nome, cpf=cpf, ativo=True)
    db.session.add(f)
    db.session.commit()
    if loja:
        f.lojas.append(loja)
        db.session.commit()
    return f


# ── Aplicação prática (§8) ──────────────────────────────────────────────
def test_gestor_nao_registra_pra_si(app):   # critério 12
    with app.app_context():
        temp, loja = _temp(), _loja('Brooklin')
        g = _func('Gestor', '1', loja)
        trilha = TreinoTrilha(nome='Seg')
        db.session.add(trilha)
        db.session.commit()
        with pytest.raises(ap.AplicacaoError):
            ap.registrar(g, g, trilha, temp, [], 'x' * 30,
                         criado_por_id=None, is_admin=True)


def test_aplicacao_duplicada_rejeitada_e_credita_50(app):   # critério 13
    with app.app_context():
        temp, loja = _temp(), _loja('Brooklin')
        g = _func('Gestor', '1', loja)
        f = _func('Ana', '2', loja)
        f.lider_id = g.id
        trilha = TreinoTrilha(nome='Seg')
        db.session.add(trilha)
        db.session.commit()
        checklist = lideranca.salvar_checklist(
            trilha, 'Prática', ['Aplicou o procedimento'])
        item_id = checklist.itens[0].id
        ap.registrar(g, f, trilha, temp, [item_id],
                     'aplicou tudo certinho ok',
                     criado_por_id=None)
        assert ledger.saldo(f.id, temp.id) == 50
        with pytest.raises(ap.AplicacaoError):
            ap.registrar(g, f, trilha, temp, [item_id],
                         'de novo mesma trilha aqui',
                         criado_por_id=None)


def test_aplicacao_evidencia_curta_rejeitada(app):
    with app.app_context():
        temp, loja = _temp(), _loja('Brooklin')
        g = _func('Gestor', '1', loja)
        f = _func('Ana', '2', loja)
        trilha = TreinoTrilha(nome='Seg')
        db.session.add(trilha)
        db.session.commit()
        with pytest.raises(ap.AplicacaoError):
            ap.registrar(g, f, trilha, temp, [], 'curto', criado_por_id=None)


# ── Ranking (§7) ────────────────────────────────────────────────────────
def test_transferencia_nao_move_historico(app):   # critério 17
    with app.app_context():
        temp, a, b = _temp(), _loja('Brooklin'), _loja('Itaim')
        f = _func('Ana', '2', a)
        ledger.creditar(f, 'VIDEO_CONCLUIDO', 100, temporada=temp,
                        referencia_tipo='video', referencia_id=1)
        # transfere Brooklin -> Itaim
        f.lojas = [b]
        db.session.commit()
        ledger.creditar(f, 'VIDEO_CONCLUIDO', 50, temporada=temp,
                        referencia_tipo='video', referencia_id=2)
        por_unidade = {e.unidade_id: e.pontos for e in
                       TreinoEventoPontos.query.filter_by(
                           funcionario_id=f.id).all()}
        assert por_unidade[a.id] == 100      # ponto ganho na Brooklin FICA lá
        assert por_unidade[b.id] == 50


def test_ranking_unidades_nao_expoe_individual(app):   # critério 19
    with app.app_context():
        temp, a, b = _temp(), _loja('Brooklin'), _loja('Itaim')
        f1 = _func('Ana', '2', a)
        f2 = _func('Bia', '3', b)
        ledger.creditar(f1, 'VIDEO_CONCLUIDO', 100, temporada=temp,
                        referencia_tipo='video', referencia_id=1)
        ledger.creditar(f2, 'VIDEO_CONCLUIDO', 60, temporada=temp,
                        referencia_tipo='video', referencia_id=1)
        r = rk.ranking_unidades(temp.id)
        chaves = set().union(*[set(x) for x in r])
        assert 'funcionario' not in chaves and 'funcionario_id' not in chaves
        assert {'unidade', 'pontos', 'normalizado', 'posicao'} <= chaves


def test_nivel_por_pontos(app):
    with app.app_context():
        assert rk.nivel(0) == 'Bronze'
        assert rk.nivel(300) == 'Prata'
        assert rk.nivel(800) == 'Ouro'
        assert rk.nivel(1500) == 'Diamante'


# ── Recompensas / resgate (§10) ─────────────────────────────────────────
def test_resgate_saldo_insuficiente_e_debito_na_aprovacao(app):   # critério 14
    with app.app_context():
        temp, loja = _temp(), _loja('Brooklin')
        f = _func('Ana', '2', loja)
        ledger.creditar(f, 'AJUSTE_MANUAL', 100, temporada=temp)
        recompensa = TreinoRecompensa(nome='Folga', custo_pontos=60, estoque=1)
        db.session.add(recompensa)
        db.session.commit()

        r1 = rc.solicitar(f, recompensa, temp)
        assert ledger.saldo(f.id, temp.id) == 100     # solicitar NÃO debita
        rc.aprovar(r1, decidido_por_id=None)
        assert ledger.saldo(f.id, temp.id) == 40      # debita na aprovação
        assert recompensa.estoque == 0

        # 2ª tentativa: saldo insuficiente -> recusa, saldo não fica negativo
        recompensa.estoque = 5
        db.session.commit()
        r2 = rc.solicitar(f, recompensa, temp)
        with pytest.raises(rc.ResgateError):
            rc.aprovar(r2, decidido_por_id=None)
        assert ledger.saldo(f.id, temp.id) == 40      # nunca negativo

        # cancelar o 1º devolve os pontos e o estoque
        rc.cancelar(r1, decidido_por_id=None)
        assert ledger.saldo(f.id, temp.id) == 100
        assert recompensa.estoque == 6
