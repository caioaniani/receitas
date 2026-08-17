"""Cadeado por dia (🔒) + ordem enviada de volta na tela (dono, 08/07/2026).

1. Dia FECHADO com o cadeado: edição de célula recusada (dia_fechado, 422 na
   rota), "limpar edições manuais" preserva os overrides dele e o reset (↺)
   por linha o pula. Reabrir volta tudo ao normal.
2. Dia ENVIADO cujo grid difere da ordem: a tela mostra o número que o
   padeiro está vendo (marcador 📤) e o aviso "difere do enviado" — enquanto
   o "🔄 atualizar produção" não for clicado.
"""
from datetime import timedelta

import pytest

from app.extensions import db
from app.models import CronogramaOverride, Loja, PedidoItem, PedidoLoja, Receita
from app.utils import hoje


@pytest.fixture(autouse=True)
def _hoje_e_segunda_fixa(congela_hoje):
    """Producao so seg-sex (dono 17/08/2026) tornou o shaping do cronograma
    sensivel ao dia da semana — congela hoje() numa SEGUNDA pros cenarios
    hoje()+N deste arquivo cairem sempre em dia util, em qualquer dia em que
    a suite rode (ver conftest.congela_hoje)."""
    congela_hoje()


def _cenario(nome='Pao Cadeado', loja_nome='Loja Cadeado', qtd=50):
    r = Receita(nome=nome, categoria='Paes', rendimento_qtd=1,
                rendimento_unidade='un', peso_base=1000.0)
    loja = Loja(nome=loja_nome, ativa=True)
    db.session.add_all([r, loja])
    db.session.flush()
    d2 = hoje() + timedelta(days=2)
    p = PedidoLoja(loja_id=loja.id, status='pendente', data_entrega=d2,
                   data_pedido=d2)
    db.session.add(p)
    db.session.flush()
    db.session.add(PedidoItem(pedido_id=p.id, receita_id=r.id, quantidade=qtd))
    db.session.commit()
    return r, d2


def _login(client, user):
    with client.session_transaction() as sess:
        sess['_user_id'] = str(user.id)
        sess['_fresh'] = True


# ── Cadeado por dia ─────────────────────────────────────────────────────

def test_dia_fechado_recusa_edicao_de_celula(app, admin_user):
    from app.services.cronograma_edit import alternar_dia_fechado, editar_celula
    with app.app_context():
        r, d2 = _cenario()
        assert alternar_dia_fechado(d2, admin_user.id) is True
        res = editar_celula(r.id, d2.isoformat(), 80, horizonte_dias=7)
        assert res.get('erro') == 'dia_fechado'
        assert CronogramaOverride.query.count() == 0     # nada salvo

        # reabrir volta a aceitar
        assert alternar_dia_fechado(d2, admin_user.id) is False
        res = editar_celula(r.id, d2.isoformat(), 80, horizonte_dias=7)
        assert res.get('erro') is None
        assert res['total'] == 80


def test_rota_celula_dia_fechado_422(app, admin_user):
    from app.services.cronograma_edit import alternar_dia_fechado
    with app.app_context():
        r, d2 = _cenario()
        alternar_dia_fechado(d2, admin_user.id)
        c = app.test_client()
        _login(c, admin_user)
        resp = c.post('/telaindustriateste/celula',
                      json={'receita_id': r.id, 'data': d2.isoformat(),
                            'qtd': 80, 'horizonte': 7, 'inicio': 0})
        assert resp.status_code == 422
        assert resp.get_json()['erro'] == 'dia_fechado'


def test_limpar_edicoes_preserva_dia_fechado(app, admin_user):
    from app.services.cronograma_edit import (
        alternar_dia_fechado,
        editar_celula,
        limpar_todos_overrides,
    )
    with app.app_context():
        r, d2 = _cenario()
        d3 = d2 + timedelta(days=1)
        # override em d2 (vai ser fechado) e em d3 (fica aberto)
        assert editar_celula(r.id, d2.isoformat(), 80,
                             horizonte_dias=7).get('erro') is None
        assert editar_celula(r.id, d3.isoformat(), 30,
                             horizonte_dias=7).get('erro') is None
        alternar_dia_fechado(d2, admin_user.id)

        apagados, preservados = limpar_todos_overrides()
        assert apagados == 1 and preservados == 1
        restantes = CronogramaOverride.query.all()
        assert len(restantes) == 1
        assert restantes[0].data == d2 and restantes[0].qtd == 80


def test_reset_de_linha_pula_dia_fechado(app, admin_user):
    from app.services.cronograma_edit import (
        alternar_dia_fechado,
        editar_celula,
        resetar_receita,
    )
    with app.app_context():
        r, d2 = _cenario()
        d3 = d2 + timedelta(days=1)
        editar_celula(r.id, d2.isoformat(), 80, horizonte_dias=7)
        editar_celula(r.id, d3.isoformat(), 30, horizonte_dias=7)
        alternar_dia_fechado(d2, admin_user.id)

        n, preservados = resetar_receita(r.id, [d2.isoformat(), d3.isoformat()])
        assert n == 1 and preservados == 1               # só o d3; d2 ficou
        restantes = CronogramaOverride.query.all()
        assert len(restantes) == 1 and restantes[0].data == d2


def test_rota_cadeado_toggla_e_exige_admin(app, admin_user):
    """ARMADILHA do app compartilhado: a fixture `app` mantém um app_context
    empurrado o teste inteiro (conftest.py), as requests do test client REUSAM
    esse contexto e o Flask-Login cacheia o usuário em g._login_user — o admin
    da 1ª request vaza pra request do funcionário (falso 200/302). Antes da
    request do 2º usuário, limpe o cache: delattr(g, '_login_user')."""
    from app.models import CronogramaDiaFechado, Usuario
    with app.app_context():
        _, d2 = _cenario()
        func = Usuario(nome='func', login='func-cad', papel='funcionario')
        func.set_senha('123')
        db.session.add(func)
        db.session.commit()
        func_id = func.id

    c = app.test_client()
    _login(c, admin_user)
    c.post('/telaindustriateste/dia/cadeado', data={'data': d2.isoformat()})
    with app.app_context():
        assert CronogramaDiaFechado.query.filter_by(data=d2).count() == 1
    c.post('/telaindustriateste/dia/cadeado', data={'data': d2.isoformat()})
    with app.app_context():
        assert CronogramaDiaFechado.query.filter_by(data=d2).count() == 0

    from flask import g
    if hasattr(g, '_login_user'):                        # limpa o cache do
        delattr(g, '_login_user')                        # admin (ver docstring)
    c2 = app.test_client()
    with c2.session_transaction() as sess:
        sess['_user_id'] = str(func_id)
        sess['_fresh'] = True
    resp = c2.post('/telaindustriateste/dia/cadeado',
                   data={'data': d2.isoformat()})
    assert resp.status_code == 403
    with app.app_context():
        assert CronogramaDiaFechado.query.filter_by(data=d2).count() == 0


def test_enviar_continua_permitido_em_dia_fechado(app, admin_user):
    """O cadeado protege o RASCUNHO; enviar/atualizar produção é o gesto
    explícito e segue funcionando (fechei o dia → envio a ordem)."""
    from app.services.cronograma_edit import alternar_dia_fechado
    from app.services.producao import enviar_plano_do_dia
    with app.app_context():
        r, d2 = _cenario()
        alternar_dia_fechado(d2, admin_user.id)
        plano = enviar_plano_do_dia(d2, admin_user.id, horizonte_dias=7)
        assert plano is not None and plano.enviado_ao_padeiro is True


# ── Ordem enviada de volta na tela ──────────────────────────────────────

def test_grid_mostra_ordem_enviada_quando_difere(app, admin_user):
    """Envia a ordem, edita o grid SEM re-enviar → a tela mostra o marcador
    📤 com o número do padeiro e o aviso 'difere do enviado'."""
    from app.services.cronograma_edit import editar_celula
    from app.services.producao import enviar_plano_do_dia
    with app.app_context():
        r, d2 = _cenario(nome='Pao Difere', loja_nome='Loja Difere')
        plano = enviar_plano_do_dia(d2, admin_user.id, horizonte_dias=7)
        enviado = {it.receita_id: it.qtd_alvo for it in plano.itens}[r.id]

        c = app.test_client()
        _login(c, admin_user)
        # equilibrar=0: ordem criada no modo curva (service default) — a tela
        # na MESMA visão não acusa divergência antes da edição.
        html = c.get('/telaindustriateste/?horizonte=7&equilibrar=0')\
            .get_data(as_text=True)
        assert 'dia-badge dia-difere' not in html

        # edita o grid (rascunho) sem atualizar a produção
        res = editar_celula(r.id, d2.isoformat(), enviado + 25,
                            horizonte_dias=7)
        assert res.get('erro') is None
        html = c.get('/telaindustriateste/?horizonte=7&equilibrar=0')\
            .get_data(as_text=True)
        assert 'dia-badge dia-difere' in html            # badge do cabeçalho
        assert ('📤 %d' % enviado) in html               # o que o padeiro vê
        # compactação (dono 08/07): dia que diverge mostra SÓ o badge âmbar —
        # não empilha "📤 enviado" verde junto (era o que alargava a coluna).
        assert '>📤 enviado<' not in html


def test_atualizar_producao_zera_o_difere(app, admin_user):
    from app.services.cronograma_edit import editar_celula
    from app.services.producao import enviar_plano_do_dia
    with app.app_context():
        r, d2 = _cenario(nome='Pao Sync', loja_nome='Loja Sync')
        plano = enviar_plano_do_dia(d2, admin_user.id, horizonte_dias=7)
        enviado = {it.receita_id: it.qtd_alvo for it in plano.itens}[r.id]
        editar_celula(r.id, d2.isoformat(), enviado + 25, horizonte_dias=7)

        # "🔄 atualizar produção" = re-enviar → grid e ordem batem de novo
        enviar_plano_do_dia(d2, admin_user.id, horizonte_dias=7)
        c = app.test_client()
        _login(c, admin_user)
        html = c.get('/telaindustriateste/?horizonte=7').get_data(as_text=True)
        assert 'dia-badge dia-difere' not in html


def test_mao_dupla_do_editar_plano_nao_grava_override_em_dia_fechado(
        app, admin_user):
    """Achado da revisão 08/07: o POST do /padeiro/plano/editar espelhava a
    ordem no CronogramaOverride SEM checar o cadeado — editar a ordem num dia
    fechado sobrescrevia o rascunho protegido. Editar a ORDEM segue permitido
    (gesto explícito); o espelho no rascunho é que é pulado."""
    from app.services.cronograma_edit import alternar_dia_fechado, editar_celula
    from app.services.producao import enviar_plano_do_dia
    with app.app_context():
        r, d2 = _cenario(nome='Pao Mao Dupla', loja_nome='Loja Mao Dupla')
        assert editar_celula(r.id, d2.isoformat(), 80,
                             horizonte_dias=7).get('erro') is None
        plano = enviar_plano_do_dia(d2, admin_user.id, horizonte_dias=7)
        item = plano.itens[0]
        alternar_dia_fechado(d2, admin_user.id)
        item_id, rid = item.id, r.id

    c = app.test_client()
    _login(c, admin_user)
    resp = c.post('/padeiro/plano/editar?data=%s' % d2.isoformat(),
                  data={'data': d2.isoformat(), 'alvo_%d' % item_id: '30'},
                  follow_redirects=False)
    assert resp.status_code in (302, 303)

    with app.app_context():
        from app.models import CronogramaOverride, PlanejamentoItem
        # a ORDEM mudou (gesto explícito, permitido)...
        assert PlanejamentoItem.query.get(item_id).qtd_alvo == 30
        # ...mas o override do dia fechado ficou intacto (80, não 30)
        ov = CronogramaOverride.query.filter_by(
            receita_id=rid, data=d2).first()
        assert ov is not None and ov.qtd == 80


def test_item_dispensado_nao_gera_difere_permanente(app, admin_user):
    """Achado da revisão 08/07: item DISPENSADO com demanda no grid gerava
    '⚠ difere do enviado' que o '🔄 atualizar produção' nunca limpa (o sync
    mantém dispensada_em). Dispensa é decisão explícita — fica fora da
    comparação dos dois lados."""
    from app.services.producao import enviar_plano_do_dia
    from app.utils import agora
    with app.app_context():
        r, d2 = _cenario(nome='Pao Dispensado', loja_nome='Loja Dispensa')
        plano = enviar_plano_do_dia(d2, admin_user.id, horizonte_dias=7)
        item = plano.itens[0]
        item.dispensada_em = agora()
        db.session.commit()

    c = app.test_client()
    _login(c, admin_user)
    html = c.get('/telaindustriateste/?horizonte=7').get_data(as_text=True)
    assert 'difere do enviado' not in html


# ── Desfazer edições — voltar à ordem enviada (dono, 08/07/2026) ─────────

def test_reverter_traz_grid_de_volta_a_ordem_enviada(app, admin_user):
    """Envia a ordem, edita o grid (difere), aperta desfazer → grid volta ao
    qtd_alvo da ordem e o 'difere' some. Inverso do 'atualizar produção'."""
    from app.services.cronograma_edit import (
        editar_celula,
        reverter_dia_para_ordem_enviada,
    )
    from app.services.producao import enviar_plano_do_dia
    with app.app_context():
        r, d2 = _cenario(nome='Pao Reverter', loja_nome='Loja Reverter')
        plano = enviar_plano_do_dia(d2, admin_user.id, horizonte_dias=7)
        enviado = {it.receita_id: it.qtd_alvo for it in plano.itens}[r.id]

        editar_celula(r.id, d2.isoformat(), enviado + 40, horizonte_dias=7)
        c = app.test_client()
        _login(c, admin_user)
        html = c.get('/telaindustriateste/?horizonte=7').get_data(as_text=True)
        assert 'dia-badge dia-difere' in html            # difere antes

        res = reverter_dia_para_ordem_enviada(d2, horizonte_dias=7)
        assert res['ok'] and res['n'] >= 1
        # o override agora reproduz o alvo (sem extra) → difere some
        from app.models import CronogramaOverride
        ov = CronogramaOverride.query.filter_by(receita_id=r.id, data=d2).first()
        assert ov is not None and ov.qtd == enviado
        html = c.get('/telaindustriateste/?horizonte=7').get_data(as_text=True)
        assert 'dia-badge dia-difere' not in html         # difere sumiu
        # a ORDEM (o que o padeiro vê) não mudou
        db.session.expire_all()
        assert {it.receita_id: it.qtd_alvo
                for it in plano.itens}[r.id] == enviado


def test_reverter_zera_receita_adicionada_depois(app, admin_user):
    """Receita que o grid passou a mostrar mas não está na ordem é zerada."""
    from app.services.cronograma_edit import (
        editar_celula,
        reverter_dia_para_ordem_enviada,
    )
    from app.services.producao import enviar_plano_do_dia
    with app.app_context():
        r, d2 = _cenario(nome='Pao Base R2', loja_nome='Loja R2')
        # 2ª receita SEM pedido — entra no grid via edição manual depois
        extra = Receita(nome='Extra R2', categoria='Paes', rendimento_qtd=1,
                        rendimento_unidade='un', peso_base=1000.0)
        db.session.add(extra)
        db.session.commit()
        enviar_plano_do_dia(d2, admin_user.id, horizonte_dias=7)
        # adiciona a extra ao grid (não estava na ordem)
        editar_celula(extra.id, d2.isoformat(), 30, horizonte_dias=7)
        extra_id = extra.id

    with app.app_context():
        res = reverter_dia_para_ordem_enviada(d2, horizonte_dias=7)
        assert res['ok']
        from app.models import CronogramaOverride
        ov = CronogramaOverride.query.filter_by(
            receita_id=extra_id, data=d2).first()
        assert ov is not None and ov.qtd == 0        # zerada (não era da ordem)


def test_reverter_recusa_dia_fechado(app, admin_user):
    from app.services.cronograma_edit import (
        alternar_dia_fechado,
        reverter_dia_para_ordem_enviada,
    )
    from app.services.producao import enviar_plano_do_dia
    with app.app_context():
        r, d2 = _cenario(nome='Pao RF', loja_nome='Loja RF')
        enviar_plano_do_dia(d2, admin_user.id, horizonte_dias=7)
        alternar_dia_fechado(d2, admin_user.id)
        res = reverter_dia_para_ordem_enviada(d2, horizonte_dias=7)
        assert res['ok'] is False and res['erro'] == 'dia_fechado'


def test_reverter_sem_ordem_recusa(app, admin_user):
    from app.services.cronograma_edit import reverter_dia_para_ordem_enviada
    with app.app_context():
        r, d2 = _cenario(nome='Pao SO', loja_nome='Loja SO')
        # nunca enviado
        res = reverter_dia_para_ordem_enviada(d2, horizonte_dias=7)
        assert res['ok'] is False and res['erro'] == 'sem_ordem'


def test_rota_reverter_ordem(app, admin_user):
    from app.services.cronograma_edit import editar_celula
    from app.services.producao import enviar_plano_do_dia
    with app.app_context():
        r, d2 = _cenario(nome='Pao Rota Rev', loja_nome='Loja Rota Rev')
        plano = enviar_plano_do_dia(d2, admin_user.id, horizonte_dias=7)
        enviado = {it.receita_id: it.qtd_alvo for it in plano.itens}[r.id]
        editar_celula(r.id, d2.isoformat(), enviado + 40, horizonte_dias=7)

    c = app.test_client()
    _login(c, admin_user)
    resp = c.post('/telaindustriateste/reverter-ordem',
                  data={'data': d2.isoformat(), 'horizonte': 7, 'inicio': 0},
                  follow_redirects=True)
    assert resp.status_code == 200
    with app.app_context():
        from app.models import CronogramaOverride
        ov = CronogramaOverride.query.filter_by(receita_id=r.id, data=d2).first()
        assert ov is not None and ov.qtd == enviado
