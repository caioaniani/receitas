"""Zerar os congelados da indústria para recontagem manual (dono, 08/10/2026)."""
from decimal import Decimal

from app.extensions import db
from app.models import (
    EstoqueProducao,
    FaltaSaidaIndustria,
    MovEstoqueProducao,
    Receita,
    SaldoResidualMassa,
)
from app.services import congelados_zerar
from app.services.estoque_congelados import entrada_producao
from app.services.estoque_massa import registrar_entrada_massa, saldo_bolas


def _receita(nome):
    r = Receita(nome=nome, categoria='Viennoiserie', rendimento_qtd=1,
                rendimento_unidade='un', peso_base=100.0, peso_unitario=90.0)
    db.session.add(r)
    db.session.commit()
    return r


def _cliente(app, user):
    c = app.test_client()
    with c.session_transaction() as sess:
        sess['_user_id'] = str(user.id)
        sess['_fresh'] = True
    return c


def _saldo(rec):
    return sum(ep.quantidade or 0
               for ep in EstoqueProducao.query.filter_by(receita_id=rec.id))


def test_dry_run_lista_e_nao_mexe(app, owner_user):
    rec = _receita('Croissant Z')
    entrada_producao(receita_id=rec.id, quantidade=1343, usuario_id=owner_user.id)
    db.session.commit()
    res = congelados_zerar.zerar(executar=False, usuario_id=owner_user.id)
    assert [i['quantidade'] for i in res['itens']] == [1343]
    assert res['zerados'] == 0
    assert _saldo(rec) == 1343


def test_executar_zera_com_movimento_e_encerra_faltas(app, owner_user):
    rec = _receita('Danish Z')
    entrada_producao(receita_id=rec.id, quantidade=509, usuario_id=owner_user.id)
    pend = EstoqueProducao(nome_pendente='Item sem cadastro', quantidade=7)
    db.session.add(pend)
    db.session.flush()
    ep = EstoqueProducao.query.filter_by(receita_id=rec.id).first()
    falta = FaltaSaidaIndustria(receita_id=rec.id, quantidade=10)
    db.session.add(falta)
    db.session.commit()

    res = congelados_zerar.zerar(executar=True, usuario_id=owner_user.id)
    db.session.commit()
    assert res['zerados'] == 2
    assert res['faltas_encerradas'] == 1
    assert _saldo(rec) == 0
    assert db.session.get(EstoqueProducao, pend.id).quantidade == 0
    mov = (MovEstoqueProducao.query
           .filter_by(estoque_producao_id=ep.id, tipo='ajuste_conferencia').one())
    assert mov.quantidade == -509
    assert congelados_zerar.REFERENCIA in mov.referencia
    db.session.refresh(falta)
    assert falta.motivo_encerramento == 'contagem'
    # Segunda rodada não tem o que zerar.
    assert congelados_zerar.zerar(executar=True, usuario_id=owner_user.id)['zerados'] == 0


def test_massa_para_folhar_zera_bolas_e_residuo(app, owner_user):
    rec = Receita(nome='Massa para folhar', categoria='Viennoiserie',
                  peso_base=2000, peso_unitario=3580, rendimento_qtd=1,
                  rendimento_unidade='un')
    db.session.add(rec)
    db.session.flush()
    registrar_entrada_massa(rec, 44900, None, 'Batimento')
    db.session.commit()
    assert saldo_bolas(rec) > 12

    res = congelados_zerar.zerar(executar=True, usuario_id=owner_user.id)
    db.session.commit()
    assert [i['unidade'] for i in res['itens']] == ['bolas']
    assert saldo_bolas(rec) == 0
    resto = db.session.get(SaldoResidualMassa, rec.id)
    assert resto is None or Decimal(resto.g) == 0


def test_rota_recusa_admin_comum(app, admin_user):
    assert _cliente(app, admin_user).get('/admin/congelados-zerar').status_code == 403


def test_rota_do_dono_previa_e_executa(app, owner_user):
    rec = _receita('Pain Z')
    entrada_producao(receita_id=rec.id, quantidade=347, usuario_id=owner_user.id)
    db.session.commit()
    dono = _cliente(app, owner_user)
    r = dono.get('/admin/congelados-zerar')
    assert r.status_code == 200 and r.get_json()['executado'] is False
    assert _saldo(rec) == 347
    r = dono.get('/admin/congelados-zerar?executar=1')
    assert r.get_json()['zerados'] == 1
    db.session.expire_all()
    assert _saldo(rec) == 0
