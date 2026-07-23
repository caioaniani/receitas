"""Fase 1 do sistema gamificado — LEDGER de pontos (fundação).

Cobre os critérios de aceite que dependem do ledger: idempotência (4), estorno
não apaga (15), e a regra do teto diário (§4.2). Reusa Loja (unidade) e
Funcionario (pessoa), conforme decisão do dono.
"""
from datetime import timedelta

import pytest

from app.extensions import db
from app.models import (
    Funcionario,
    Loja,
    TreinoConfigPontos,
    TreinoEventoPontos,
    TreinoTemporada,
    Usuario,
)
from app.services import treino_ledger as ledger
from app.services import treino_pontos as cfg
from app.utils import hoje


def _temp():
    t = TreinoTemporada(nome='2026-T1', inicio=hoje() - timedelta(days=1),
                        fim=hoje() + timedelta(days=30), status='ATIVA')
    db.session.add(t)
    db.session.commit()
    return t


def _loja(nome='Brooklin'):
    lj = Loja(nome=nome, ativa=True)
    db.session.add(lj)
    db.session.commit()
    return lj


def _func(cpf='111.111.111-11', loja=None):
    f = Funcionario(nome='Fulano', cpf=cpf)
    if loja:
        f.lojas.append(loja)
    db.session.add(f)
    db.session.commit()
    return f


def test_credita_e_saldo(app):
    with app.app_context():
        temp, loja = _temp(), _loja()
        f = _func(loja=loja)
        ev, novo = ledger.creditar(f, 'VIDEO_CONCLUIDO', 10, temporada=temp,
                                   referencia_tipo='video', referencia_id=1)
        assert novo is True and ev.pontos == 10
        assert ev.unidade_id == loja.id           # unidade CONGELADA no evento
        assert ledger.saldo(f.id, temp.id) == 10


def test_idempotente_nao_credita_duas_vezes(app):   # critério 4
    with app.app_context():
        temp = _temp()
        f = _func(loja=_loja())
        ledger.creditar(f, 'VIDEO_CONCLUIDO', 10, temporada=temp,
                        referencia_tipo='video', referencia_id=7)
        _, novo2 = ledger.creditar(f, 'VIDEO_CONCLUIDO', 10, temporada=temp,
                                   referencia_tipo='video', referencia_id=7)
        assert novo2 is False
        assert ledger.saldo(f.id, temp.id) == 10
        assert TreinoEventoPontos.query.filter_by(
            funcionario_id=f.id, tipo='VIDEO_CONCLUIDO').count() == 1


def test_estorno_nao_apaga_original(app):   # critério 15
    with app.app_context():
        temp = _temp()
        f = _func(loja=_loja())
        ev, _ = ledger.creditar(f, 'APLICACAO_PRATICA', 50, temporada=temp,
                                referencia_tipo='aplicacao', referencia_id=3)
        est, estornou = ledger.estornar(ev, criado_por_id=None)
        assert estornou is True
        assert est.pontos == -50 and est.estorno_de_id == ev.id
        assert db.session.get(TreinoEventoPontos, ev.id) is not None   # vivo
        assert ledger.saldo(f.id, temp.id) == 0
        _, de_novo = ledger.estornar(ev)             # idempotente
        assert de_novo is False


def test_teto_diario_zera_o_excedente(app):   # §4.2
    with app.app_context():
        temp = _temp()
        f = _func(loja=_loja())
        for i in range(1, 5):                        # 4 x 50 = 200 (teto)
            ev, _ = ledger.creditar(f, 'APLICACAO_PRATICA', 50, temporada=temp,
                                    referencia_tipo='ap', referencia_id=i)
            assert ev.pontos == 50
        assert ledger.saldo(f.id, temp.id) == 200
        ev5, _ = ledger.creditar(f, 'APLICACAO_PRATICA', 50, temporada=temp,
                                 referencia_tipo='ap', referencia_id=5)
        assert ev5.pontos == 0 and 'teto' in (ev5.observacao or '')
        assert ledger.saldo(f.id, temp.id) == 200    # não passou do teto


def test_ajuste_manual_permite_multiplos_e_ignora_teto(app):
    with app.app_context():
        temp = _temp()
        f = _func(loja=_loja())
        ledger.ajuste_manual(f, 300, 'bonus especial', criado_por_id=None,
                             temporada=temp)          # acima do teto, mas passa
        ledger.ajuste_manual(f, 50, 'outro ajuste', criado_por_id=None,
                             temporada=temp)
        assert ledger.saldo(f.id, temp.id) == 350
        assert TreinoEventoPontos.query.filter_by(
            funcionario_id=f.id, tipo='AJUSTE_MANUAL').count() == 2


def test_ajuste_manual_exige_justificativa(app):
    with app.app_context():
        temp = _temp()
        f = _func(loja=_loja())
        with pytest.raises(ValueError):
            ledger.ajuste_manual(f, 10, '   ', criado_por_id=None,
                                 temporada=temp)


def test_sem_temporada_ativa_recusa(app):
    with app.app_context():
        f = _func(loja=_loja())
        with pytest.raises(ValueError, match='temporada'):
            ledger.creditar(f, 'VIDEO_CONCLUIDO', 10,
                            referencia_tipo='video', referencia_id=1)


def test_papel_treino(app):
    with app.app_context():
        def mk(papel, owner=False):
            u = Usuario(nome='U', login=f'u-{papel}-{owner}', papel=papel)
            u.set_senha('x' * 8)
            u.is_owner = owner
            db.session.add(u)
            db.session.commit()
            return u
        assert ledger.papel_treino(mk('gerente')) == 'GESTOR'
        assert ledger.papel_treino(mk('admin')) == 'ADMIN'
        assert ledger.papel_treino(mk('funcionario')) == 'FUNCIONARIO'
        assert ledger.papel_treino(mk('padeiro', owner=True)) == 'ADMIN'
        assert ledger.papel_treino(None) == 'FUNCIONARIO'


def test_config_valor_default_e_override(app):   # critério 22 (config, não hard-code)
    with app.app_context():
        assert cfg.valor('VIDEO_CONCLUIDO') == 10          # default do código
        db.session.add(TreinoConfigPontos(chave='VIDEO_CONCLUIDO', valor=25))
        db.session.commit()
        assert cfg.valor('VIDEO_CONCLUIDO') == 25          # tabela sobrepõe
        with pytest.raises(KeyError):
            cfg.valor('NAO_EXISTE')
