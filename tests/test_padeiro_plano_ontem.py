"""Ordem de produção persiste na tela do padeiro após a meia-noite (03/07/2026).

O padeiro trabalha de madrugada: a ordem do dia D é executada na madrugada de
D+1, e na virada da meia-noite `hoje()` rolava e a ordem SUMIA da tela (a tela
buscava só o plano de data == hoje). Agora a visão "hoje" mostra também a
ordem de ONTEM que ainda tem falta, num card destacado, até ser produzida ou
o admin dispensá-la na auditoria.
"""
from datetime import timedelta

import pytest

from app.extensions import db
from app.models import PlanejamentoItem, PlanejamentoProducao, Receita
from app.utils import agora, hoje


@pytest.fixture
def cliente(app):
    c = app.test_client()
    c.post('/auth/login', data={'login': 'admin', 'senha': '123'})
    return c


def _plano(dia, receita, alvo=40, produzido=0, enviado=True, dispensada=False):
    plano = PlanejamentoProducao(data=dia, origem='cronograma',
                                 status='aprovado',
                                 enviado_ao_padeiro=enviado)
    db.session.add(plano)
    db.session.flush()
    item = PlanejamentoItem(planejamento_id=plano.id, receita_id=receita.id,
                            qtd_alvo=alvo, produzido_qtd=produzido,
                            multiplicador=1,
                            dispensada_em=(agora() if dispensada else None))
    db.session.add(item)
    db.session.commit()
    return plano, item


def _receita(nome):
    r = Receita(nome=nome, categoria='Paes', rendimento_qtd=10,
                rendimento_unidade='un', peso_base=1000.0, peso_unitario=100.0)
    db.session.add(r)
    db.session.commit()
    return r


def test_ordem_de_ontem_em_aberto_aparece_na_visao_de_hoje(app, admin_user, cliente):
    with app.app_context():
        r = _receita('Brioche Madrugada')
        _plano(hoje() - timedelta(days=1), r, alvo=40, produzido=0)
        html = cliente.get('/padeiro/').get_data(as_text=True)
        assert 'Ordem de ONTEM' in html
        assert 'Brioche Madrugada' in html
        # botão Produzir do item de ontem presente (form aponta pro item)
        item = PlanejamentoItem.query.filter_by(receita_id=r.id).one()
        assert f'/padeiro/produzir-plano/{item.id}' in html
        assert '1 item para concluir' in html
        assert '>40</b> un' in html


def test_ordem_de_ontem_produzida_nao_aparece(app, admin_user, cliente):
    with app.app_context():
        r = _receita('Pao Feito Ontem')
        _plano(hoje() - timedelta(days=1), r, alvo=40, produzido=40)
        html = cliente.get('/padeiro/').get_data(as_text=True)
        assert 'Ordem de ONTEM' not in html


def test_ordem_de_ontem_dispensada_nao_aparece(app, admin_user, cliente):
    """Admin deu OK na auditoria → o card de ontem some (fluxo de fechamento
    continua sendo a dispensa; a persistência é só do que está em aberto)."""
    with app.app_context():
        r = _receita('Pao Dispensado')
        _plano(hoje() - timedelta(days=1), r, alvo=40, produzido=0,
               dispensada=True)
        html = cliente.get('/padeiro/').get_data(as_text=True)
        assert 'Ordem de ONTEM' not in html


def test_parcialmente_produzida_mostra_so_a_falta(app, admin_user, cliente):
    with app.app_context():
        r = _receita('Baguete Parcial')
        r2 = _receita('Pao Completo')
        plano, _ = _plano(hoje() - timedelta(days=1), r, alvo=300,
                          produzido=256)
        item2 = PlanejamentoItem(planejamento_id=plano.id, receita_id=r2.id,
                                 qtd_alvo=50, produzido_qtd=50,
                                 multiplicador=1)
        db.session.add(item2)
        db.session.commit()
        html = cliente.get('/padeiro/').get_data(as_text=True)
        assert 'Ordem de ONTEM' in html
        assert 'Baguete Parcial' in html
        assert '1 item para concluir' in html
        assert '>44</b> un' in html
        assert '(já 256/300)' in html
        assert 'Pao Completo' not in html      # item já concluído sai do card


def test_visao_de_outro_dia_nao_mostra_card_de_ontem(app, admin_user, cliente):
    with app.app_context():
        r = _receita('Pao Navegacao')
        _plano(hoje() - timedelta(days=1), r, alvo=40)
        alvo_dia = (hoje() + timedelta(days=1)).isoformat()
        html = cliente.get(f'/padeiro/?data={alvo_dia}').get_data(as_text=True)
        assert 'Ordem de ONTEM' not in html


def test_produzir_item_de_ontem_credita_estoque(app, admin_user, cliente):
    """O botão Produzir do card de ontem funciona: credita estoque e avança
    produzido_qtd — quando zera a falta, o card some."""
    with app.app_context():
        from app.models import EstoqueProducao
        r = _receita('Croissant Virada')
        _plano(hoje() - timedelta(days=1), r, alvo=30, produzido=0)
        item = PlanejamentoItem.query.filter_by(receita_id=r.id).one()
        resp = cliente.post(f'/padeiro/produzir-plano/{item.id}',
                            data={'unidades': '30'})
        assert resp.status_code in (302, 303)
        db.session.expire_all()
        assert PlanejamentoItem.query.get(item.id).produzido_qtd == 30
        ep = EstoqueProducao.query.filter_by(receita_id=r.id).first()
        assert ep is not None and ep.quantidade == 30
        html = cliente.get('/padeiro/').get_data(as_text=True)
        assert 'Ordem de ONTEM' not in html
