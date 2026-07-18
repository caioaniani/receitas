"""Falta ENCERRADA pelo padeiro (17/07/2026, decisão do dono).

O padeiro produz menos que o alvo e dá o item por feito: some das telas
DELE (card do dia + ordem de ontem); a diferença fica só na auditoria, onde
o admin decide — ✓ OK (dispensar) ou reagendar pra hoje (devolve pra tela
do padeiro; o reagendar LIMPA o marcador). Estoque credita só o produzido.
"""
from datetime import timedelta

import pytest

from app.extensions import db
from app.utils import hoje


@pytest.fixture
def cliente(app):
    return app.test_client()


def _login(cliente, user):
    with cliente.session_transaction() as sess:
        sess['_user_id'] = str(user.id)
        sess['_fresh'] = True


def _receita(nome='Sourdough Enc'):
    from app.models import Receita
    r = Receita(nome=nome, categoria='Paes', rendimento_qtd=1,
                rendimento_unidade='un', peso_base=100.0, peso_unitario=100.0)
    db.session.add(r)
    db.session.commit()
    return r


def _plano_com_item(receita, data=None, alvo=50, enviado=True):
    from app.models import PlanejamentoItem, PlanejamentoProducao
    p = PlanejamentoProducao(data=data or hoje(), origem='cronograma',
                             enviado_ao_padeiro=enviado)
    db.session.add(p)
    db.session.flush()
    it = PlanejamentoItem(planejamento_id=p.id, receita_id=receita.id,
                          qtd_alvo=alvo, produzido_qtd=0)
    db.session.add(it)
    db.session.commit()
    return p, it


def test_encerrar_marca_e_some_da_tela_do_padeiro(app):
    from app.blueprints.padeiro.routes import _plano_do_dia
    from app.models import EstoqueProducao
    from app.services.producao import produzir_item_plano
    r = _receita()
    _, it = _plano_com_item(r, alvo=50)

    res = produzir_item_plano(it.id, 30, None, encerrar=True)
    assert res['ok'] is True
    assert res['encerrado'] is True
    assert res['falta_restante'] == 20
    assert it.falta_encerrada_em is not None
    # estoque creditou SÓ o produzido de verdade
    ep = EstoqueProducao.query.filter_by(receita_id=r.id).first()
    assert ep is not None and ep.quantidade == 30
    # a tela do padeiro não mostra mais o item (nem no total_falta)
    p = _plano_do_dia(hoje())
    assert p is None or all(
        i['item_id'] != it.id
        for g in p['grupos'] for i in g['itens']) and all(
        i['item_id'] != it.id for i in p['solos'])


def test_parcial_sem_encerrar_continua_na_tela(app):
    """Fornadas em levas: lançou parcial SEM encerrar → item segue visível."""
    from app.blueprints.padeiro.routes import _plano_do_dia
    from app.services.producao import produzir_item_plano
    r = _receita('Pao Levas')
    _, it = _plano_com_item(r, alvo=50)

    res = produzir_item_plano(it.id, 30, None)
    assert res['ok'] is True and res['encerrado'] is False
    assert it.falta_encerrada_em is None
    p = _plano_do_dia(hoje())
    ids = [i['item_id'] for g in p['grupos'] for i in g['itens']] + \
          [i['item_id'] for i in p['solos']]
    assert it.id in ids
    assert p['total_falta'] == 20


def test_encerrar_com_alvo_completo_nao_marca(app):
    """Produziu tudo com encerrar=1 (borda): nada a encerrar, sem marcador."""
    from app.services.producao import produzir_item_plano
    r = _receita('Pao Cheio')
    _, it = _plano_com_item(r, alvo=50)
    res = produzir_item_plano(it.id, 50, None, encerrar=True)
    assert res['ok'] is True and res['encerrado'] is False
    assert it.falta_encerrada_em is None


def test_auditoria_continua_listando_com_selo(app):
    from app.services.producao import produzir_item_plano
    from app.services.producao_pendente import listar_pendencias
    r = _receita('Pao Auditoria')
    _, it = _plano_com_item(r, data=hoje() - timedelta(days=1), alvo=50)
    produzir_item_plano(it.id, 30, None, encerrar=True)

    dados = listar_pendencias()
    linha = next(x for x in dados['vencido'] if x['item_id'] == it.id)
    assert linha['falta'] == 20
    assert linha['falta_encerrada'] is True


def test_dispensar_encerrado_funciona(app, admin_user):
    """O ✓ OK da auditoria (dispensar) fecha a pendência normalmente."""
    from app.services.producao import produzir_item_plano
    from app.services.producao_pendente import dispensar_item, listar_pendencias
    r = _receita('Pao OK')
    _, it = _plano_com_item(r, data=hoje() - timedelta(days=1), alvo=50)
    produzir_item_plano(it.id, 30, None, encerrar=True)

    res = dispensar_item(it.id, admin_user.id)
    assert res.get('ok') is True
    dados = listar_pendencias()
    assert not [x for x in dados['vencido'] if x['item_id'] == it.id]


def test_reagendar_devolve_pra_tela_do_padeiro(app, admin_user):
    """"Voltar a diferença pro padeiro": reagendar move a falta pro plano de
    HOJE e o item de hoje NÃO fica encerrado (visível pra ele)."""
    from app.blueprints.padeiro.routes import _plano_do_dia
    from app.services.producao import produzir_item_plano
    from app.services.producao_pendente import reagendar_para_hoje
    r = _receita('Pao Volta')
    _, it = _plano_com_item(r, data=hoje() - timedelta(days=1), alvo=50)
    produzir_item_plano(it.id, 30, None, encerrar=True)

    res = reagendar_para_hoje([it.id], admin_user.id)
    assert res['movidos'] == 1 and res['unidades'] == 20
    p = _plano_do_dia(hoje())
    assert p is not None
    itens = [i for g in p['grupos'] for i in g['itens']] + p['solos']
    linha = next(i for i in itens if i['receita_id'] == r.id)
    assert linha['falta'] == 20                  # de volta na tela do padeiro


def test_reagendar_para_item_de_hoje_encerrado_reabre(app, admin_user):
    """Merge no item de HOJE que estava encerrado: o marcador é limpo — sem
    isso a falta devolvida cairia num item oculto e sumiria da tela."""
    from app.blueprints.padeiro.routes import _plano_do_dia
    from app.services.producao import produzir_item_plano
    from app.services.producao_pendente import reagendar_para_hoje
    r = _receita('Pao Merge')
    _, it_ontem = _plano_com_item(r, data=hoje() - timedelta(days=1), alvo=40)
    _, it_hoje = _plano_com_item(r, data=hoje(), alvo=30)
    # padeiro encerra o de HOJE com 10/30; e o de ontem ficou com falta 40
    produzir_item_plano(it_hoje.id, 10, None, encerrar=True)
    assert it_hoje.falta_encerrada_em is not None

    reagendar_para_hoje([it_ontem.id], admin_user.id)
    assert it_hoje.falta_encerrada_em is None    # reaberto
    p = _plano_do_dia(hoje())
    itens = [i for g in p['grupos'] for i in g['itens']] + p['solos']
    linha = next(i for i in itens if i['receita_id'] == r.id)
    # alvo de hoje 30+40=70, produzido 10 -> falta 60 visível
    assert linha['falta'] == 60


def test_rota_produzir_plano_com_encerrar(app, admin_user, cliente):
    from app.models import PlanejamentoItem
    r = _receita('Pao Rota')
    _, it = _plano_com_item(r, alvo=50)
    _login(cliente, admin_user)
    resp = cliente.post(f'/padeiro/produzir-plano/{it.id}',
                        data={'unidades': '30', 'encerrar': '1'},
                        follow_redirects=False)
    assert resp.status_code == 302
    with app.app_context():
        it2 = db.session.get(PlanejamentoItem, it.id)
        assert it2.produzido_qtd == 30
        assert it2.falta_encerrada_em is not None
