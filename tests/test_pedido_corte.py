"""Corte D+1 às 12h BRT: fronteira, perfis e caminhos de escrita."""
from datetime import datetime, timedelta

import pytest

from app.extensions import db
from app.models import PedidoItem, PedidoLoja, Receita, Usuario
from app.services import pedido_corte
from app.services.pedido_corte import bloqueio_do_corte, corte_ativo
from app.utils import hoje


def _login(client, user):
    with client.session_transaction() as sess:
        sess['_user_id'] = str(user.id)
        sess['_fresh'] = True


def _gerente():
    u = Usuario(nome='ger', login='ger_corte', papel='gerente')
    u.set_senha('x' * 8)
    db.session.add(u)
    db.session.commit()
    return u


def _receita(nome='Croissant Corte'):
    r = Receita(nome=nome, categoria='Croissants', rendimento_qtd=1,
                rendimento_unidade='un', peso_base=100)
    db.session.add(r)
    db.session.commit()
    return r


def _as_12h(monkeypatch):
    """Congela o relógio do serviço às 12:00 de hoje (dentro do corte)."""
    fake = datetime.combine(hoje(), datetime.min.time()).replace(hour=12)
    monkeypatch.setattr(pedido_corte, 'agora', lambda: fake)


# ── unidade ─────────────────────────────────────────────────────────

@pytest.mark.parametrize('hora,minuto,segundo,fechado', [
    (11, 59, 59, False), (12, 0, 0, True), (12, 0, 1, True),
    (18, 59, 0, True), (23, 59, 59, True),
])
def test_corte_ativo_so_amanha_apos_meio_dia(hora, minuto, segundo, fechado):
    base = datetime(2026, 9, 24, hora, minuto, segundo)
    amanha = base.date() + timedelta(days=1)
    assert corte_ativo(amanha, agora_dt=base) is fechado
    assert corte_ativo(amanha + timedelta(days=1), agora_dt=base) is False
    assert corte_ativo(base.date(), agora_dt=base) is False
    assert corte_ativo(None, agora_dt=base) is False


def test_bloqueio_vale_para_todos_perfis(app, admin_user):
    instante = datetime(2026, 9, 24, 12)
    amanha = instante.date() + timedelta(days=1)
    with app.app_context():
        for usuario in (_gerente(), admin_user, None):
            bloqueado, mensagem = bloqueio_do_corte(
                [amanha], user=usuario, agora_dt=instante)
            assert bloqueado is True and '12:00' in mensagem
            assert 'PODE prosseguir' not in mensagem
            assert bloqueio_do_corte(
                [amanha + timedelta(days=1)], user=usuario,
                agora_dt=instante) == (False, None)


# ── web ─────────────────────────────────────────────────────────────

def test_web_novo_pra_amanha_no_corte_gerente_barrado(app, loja,
                                                      monkeypatch):
    with app.app_context():
        _as_12h(monkeypatch)
        ger = _gerente()
        r = _receita()
        rid, lid = r.id, loja.id
        c = app.test_client()
        _login(c, ger)
        resp = c.post('/pedidos/novo', data={
            'loja_id': str(lid),
            'data_entrega': (hoje() + timedelta(days=1)).isoformat(),
            'item_id[]': f'r_{rid}',
            'item_qtd[]': '10',
            'item_estado[]': '',
            'item_obs[]': '',
        }, follow_redirects=True)
        assert 'horário de corte' in resp.get_data(as_text=True)
        assert PedidoLoja.query.filter_by(loja_id=lid).count() == 0


def test_web_novo_pra_amanha_no_corte_admin_barrado(
        app, admin_user, loja, monkeypatch):
    with app.app_context():
        _as_12h(monkeypatch)
        r = _receita()
        rid, lid = r.id, loja.id
        c = app.test_client()
        _login(c, admin_user)
        resp = c.post('/pedidos/novo', data={
            'loja_id': str(lid),
            'data_entrega': (hoje() + timedelta(days=1)).isoformat(),
            'item_id[]': f'r_{rid}',
            'item_qtd[]': '10',
            'item_estado[]': '',
            'item_obs[]': '',
        }, follow_redirects=True)
        assert '12:00' in resp.get_data(as_text=True)
        assert PedidoLoja.query.filter_by(loja_id=lid).count() == 0


def test_web_novo_pra_depois_de_amanha_livre_no_corte(app, loja,
                                                      monkeypatch):
    with app.app_context():
        _as_12h(monkeypatch)
        ger = _gerente()
        r = _receita()
        rid, lid = r.id, loja.id
        c = app.test_client()
        _login(c, ger)
        c.post('/pedidos/novo', data={
            'loja_id': str(lid),
            'data_entrega': (hoje() + timedelta(days=2)).isoformat(),
            'item_id[]': f'r_{rid}',
            'item_qtd[]': '10',
            'item_estado[]': '',
            'item_obs[]': '',
        })
        assert PedidoLoja.query.filter_by(loja_id=lid).count() == 1


def test_web_editar_mover_PRA_amanha_no_corte_barrado(app, loja,
                                                      monkeypatch):
    """Mover um pedido de D+3 pra AMANHÃ depois do corte fura o pré-preparo
    do mesmo jeito — o corte olha a data NOVA também."""
    with app.app_context():
        _as_12h(monkeypatch)
        ger = _gerente()
        r = _receita()
        p = PedidoLoja(loja_id=loja.id,
                       data_entrega=hoje() + timedelta(days=3),
                       status='pendente', criado_por=ger.id)
        db.session.add(p)
        db.session.flush()
        db.session.add(PedidoItem(pedido_id=p.id, receita_id=r.id,
                                  quantidade=5))
        db.session.commit()
        pid, rid = p.id, r.id
        c = app.test_client()
        _login(c, ger)
        c.post(f'/pedidos/{pid}/editar', data={
            'data_entrega': (hoje() + timedelta(days=1)).isoformat(),
            'observacao': '',
            'item_id[]': f'r_{rid}',
            'item_qtd[]': '5',
            'item_estado[]': '',
            'item_obs[]': '',
        }, follow_redirects=True)
        assert (db.session.get(PedidoLoja, pid).data_entrega
                == hoje() + timedelta(days=3))      # não moveu


def test_web_cancelar_amanha_no_corte_barrado(app, loja, monkeypatch):
    with app.app_context():
        _as_12h(monkeypatch)
        ger = _gerente()
        p = PedidoLoja(loja_id=loja.id,
                       data_entrega=hoje() + timedelta(days=1),
                       status='confirmado', criado_por=ger.id)
        db.session.add(p)
        db.session.commit()
        pid = p.id
        c = app.test_client()
        _login(c, ger)
        c.post(f'/pedidos/{pid}/cancelar', follow_redirects=True)
        assert db.session.get(PedidoLoja, pid).status == 'confirmado'


def test_web_editar_amanha_antes_do_corte_livre(app, loja, monkeypatch, pedido_versao_form):
    instante = datetime.combine(hoje(), datetime.min.time()).replace(
        hour=11, minute=59, second=59)
    monkeypatch.setattr(pedido_corte, 'agora', lambda: instante)
    with app.app_context():
        ger = _gerente()
        r = _receita()
        p = PedidoLoja(loja_id=loja.id,
                       data_entrega=hoje() + timedelta(days=1),
                       status='pendente', criado_por=ger.id)
        db.session.add(p)
        db.session.flush()
        db.session.add(PedidoItem(pedido_id=p.id, receita_id=r.id,
                                  quantidade=5))
        db.session.commit()
        pid, rid = p.id, r.id
        c = app.test_client()
        _login(c, ger)
        c.post(f'/pedidos/{pid}/editar', data={
            'versao_edicao': pedido_versao_form(c, pid),
            'data_entrega': (hoje() + timedelta(days=1)).isoformat(),
            'observacao': 'ajuste',
            'item_id[]': f'r_{rid}',
            'item_qtd[]': '8',
            'item_estado[]': '',
            'item_obs[]': '',
        })
        assert db.session.get(PedidoLoja, pid).itens[0].quantidade == 8


# ── copilot (executores — preview re-enviado não fura) ──────────────

def test_copilot_criar_pra_amanha_no_corte_gerente_recusado(
        app, loja, monkeypatch):
    from app.services.copilot import executar_criar_pedido
    with app.app_context():
        _as_12h(monkeypatch)
        ger = _gerente()
        r = _receita()
        res = executar_criar_pedido({
            'loja_id': loja.id,
            'data_entrega': (hoje() + timedelta(days=1)).isoformat(),
            'itens': [{'resolvido': {'tipo': 'receita', 'id': r.id,
                                     'nome': r.nome}, 'quantidade': 5}],
        }, ger)
        assert res['ok'] is False and 'corte' in res['erro']


def test_copilot_editar_amanha_no_corte_gerente_recusado(
        app, loja, monkeypatch):
    from app.services.copilot import executar_editar_pedido
    with app.app_context():
        _as_12h(monkeypatch)
        ger = _gerente()
        p = PedidoLoja(loja_id=loja.id,
                       data_entrega=hoje() + timedelta(days=1),
                       status='confirmado', criado_por=ger.id)
        db.session.add(p)
        db.session.commit()
        res = executar_editar_pedido({'pedido_id': p.id,
                                      'observacao': 'muda'}, ger)
        assert res['ok'] is False and 'corte' in res['erro']


def test_copilot_cancelar_amanha_no_corte_gerente_recusado(
        app, loja, monkeypatch):
    """Achado 4 da revisão de 13/08: o executor de mudar_status deixava
    CANCELAR o pedido de amanhã depois do corte — o pré-preparo já calculado
    sumia por um caminho que a rota web bloqueia."""
    from app.services.copilot import executar_mudar_status_pedido
    with app.app_context():
        _as_12h(monkeypatch)
        ger = _gerente()
        p = PedidoLoja(loja_id=loja.id,
                       data_entrega=hoje() + timedelta(days=1),
                       status='confirmado', criado_por=ger.id)
        db.session.add(p)
        db.session.commit()
        res = executar_mudar_status_pedido(
            {'pedido_id': p.id, 'novo_status': 'cancelar'}, ger)
        assert res['ok'] is False and 'corte' in res['erro']
        assert db.session.get(PedidoLoja, p.id).status == 'confirmado'


def test_copilot_cancelar_amanha_no_corte_admin_barrado(
        app, admin_user, loja, monkeypatch):
    from app.services.copilot import executar_mudar_status_pedido
    with app.app_context():
        _as_12h(monkeypatch)
        p = PedidoLoja(loja_id=loja.id,
                       data_entrega=hoje() + timedelta(days=1),
                       status='confirmado', criado_por=admin_user.id)
        db.session.add(p)
        db.session.commit()
        res = executar_mudar_status_pedido(
            {'pedido_id': p.id, 'novo_status': 'cancelar'}, admin_user)
        assert res['ok'] is False
        assert '12:00' in res['erro']
        assert db.session.get(PedidoLoja, p.id).status == 'confirmado'


@pytest.mark.parametrize('acao', ['editar', 'cancelar', 'excluir'])
def test_admin_nao_altera_pedido_fechado(app, loja, admin_user, monkeypatch, acao):
    with app.app_context():
        _as_12h(monkeypatch)
        receita = _receita()
        pedido = PedidoLoja(loja_id=loja.id, status='confirmado',
                            data_entrega=hoje() + timedelta(days=1))
        db.session.add(pedido)
        db.session.flush()
        db.session.add(PedidoItem(pedido_id=pedido.id, receita_id=receita.id,
                                  quantidade=5))
        db.session.commit()
        pid = pedido.id
        client = app.test_client()
        _login(client, admin_user)
        response = client.post(f'/pedidos/{pid}/{acao}', data={
            'data_entrega': (hoje() + timedelta(days=3)).isoformat(),
            'observacao': 'não gravar', 'item_id[]': f'r_{receita.id}',
            'item_qtd[]': '99',
        }, follow_redirects=True)
        assert '12:00' in response.get_data(as_text=True)
        db.session.expire_all()
        pedido = db.session.get(PedidoLoja, pid)
        assert pedido is not None
        assert pedido.status == 'confirmado'
        assert pedido.data_entrega == hoje() + timedelta(days=1)
        assert pedido.itens[0].quantidade == 5
        assert pedido.observacao is None


def test_formulario_aberto_antes_do_corte_nao_salva_depois(
        app, loja, admin_user, monkeypatch):
    with app.app_context():
        instante = [datetime.combine(hoje(), datetime.min.time()).replace(
            hour=11, minute=59, second=59)]
        monkeypatch.setattr(pedido_corte, 'agora', lambda: instante[0])
        receita = _receita()
        pedido = PedidoLoja(loja_id=loja.id, status='confirmado',
                            data_entrega=hoje() + timedelta(days=1))
        db.session.add(pedido)
        db.session.flush()
        db.session.add(PedidoItem(pedido_id=pedido.id, receita_id=receita.id,
                                  quantidade=5))
        db.session.commit()
        pid = pedido.id
        client = app.test_client()
        _login(client, admin_user)
        assert client.get(f'/pedidos/{pid}/editar').status_code == 200
        instante[0] = instante[0].replace(hour=12, minute=0, second=0)
        assert client.get(f'/pedidos/{pid}/editar').status_code == 302
        client.post(f'/pedidos/{pid}/editar', data={
            'data_entrega': pedido.data_entrega.isoformat(),
            'item_id[]': f'r_{receita.id}', 'item_qtd[]': '20',
        })
        assert db.session.get(PedidoLoja, pid).itens[0].quantidade == 5
        html = client.get(f'/pedidos/{pid}').get_data(as_text=True)
        assert f'/pedidos/{pid}/editar' not in html
        assert f'/pedidos/{pid}/excluir' not in html
        assert f'/pedidos/{pid}/cancelar' not in html
        assert f'/pedidos/{pid}/separar' in html


@pytest.mark.parametrize('status,esperado', [
    ('pendente', 'confirmado'), ('cancelado', 'cancelado'),
    ('em_transporte', 'em_transporte'), ('entregue', 'entregue'),
])
def test_confirmar_nao_reativa_cancelado_nem_regride_logistica(
        app, loja, admin_user, monkeypatch, status, esperado):
    with app.app_context():
        _as_12h(monkeypatch)
        pedido = PedidoLoja(loja_id=loja.id, status=status,
                            data_entrega=hoje() + timedelta(days=1))
        db.session.add(pedido)
        db.session.commit()
        pid = pedido.id
        client = app.test_client()
        _login(client, admin_user)
        client.post(f'/pedidos/{pid}/confirmar')
        assert db.session.get(PedidoLoja, pid).status == esperado


@pytest.mark.parametrize('caminho', ['novo', 'sugestao', 'copilot'])
def test_espera_pela_trava_atravessa_corte(
        app, loja, admin_user, monkeypatch, caminho):
    from app.blueprints.pedidos import routes
    from app.services import pedido_lock
    from app.services.copilot import executar_criar_pedido
    with app.app_context():
        receita = _receita()
        data = hoje() + timedelta(days=1)
        instante = [datetime.combine(hoje(), datetime.min.time()).replace(
            hour=11, minute=59, second=59)]
        monkeypatch.setattr(pedido_corte, 'agora', lambda: instante[0])
        def esperar(lojas):
            instante[0] = instante[0].replace(hour=12, minute=0, second=0)
        monkeypatch.setattr(routes, 'travar_pedidos_lojas', esperar)
        monkeypatch.setattr(pedido_lock, 'travar_pedidos_lojas', esperar)
        if caminho == 'copilot':
            res = executar_criar_pedido({
                'loja_id': loja.id, 'data_entrega': data.isoformat(),
                'itens': [{'resolvido': {'tipo': 'receita', 'id': receita.id,
                                         'nome': receita.nome}, 'quantidade': 5}],
            }, admin_user)
            assert res['ok'] is False and '12:00' in res['erro']
        else:
            client = app.test_client()
            _login(client, admin_user)
            url = ('/pedidos/novo' if caminho == 'novo' else
                   f'/pedidos/lojas/{loja.id}/sugerir-pedido')
            res = client.post(url, data={
                'loja_id': str(loja.id), 'data_entrega': data.isoformat(),
                'item_id[]': f'r_{receita.id}', 'item_ref[]': f'receita:{receita.id}',
                'item_qtd[]': '5',
            }, follow_redirects=True)
            assert '12:00' in res.get_data(as_text=True)
        assert PedidoLoja.query.count() == 0


def test_gravacao_atravessa_corte_desfaz_itens_e_data_original(
        app, loja, admin_user, monkeypatch):
    from app.services.copilot import executar_editar_pedido
    with app.app_context():
        receita = _receita()
        amanha = hoje() + timedelta(days=1)
        pedido = PedidoLoja(loja_id=loja.id, status='confirmado', data_entrega=amanha)
        db.session.add(pedido)
        db.session.flush()
        db.session.add(PedidoItem(pedido_id=pedido.id, receita_id=receita.id,
                                  quantidade=5))
        db.session.commit()
        pid = pedido.id
        instante = [datetime.combine(hoje(), datetime.min.time()).replace(
            hour=11, minute=59, second=59)]
        monkeypatch.setattr(pedido_corte, 'agora', lambda: instante[0])
        flush_original = db.session.flush
        def flush_lento(*args, **kwargs):
            res = flush_original(*args, **kwargs)
            instante[0] = instante[0].replace(hour=12, minute=0, second=0)
            return res
        monkeypatch.setattr(db.session, 'flush', flush_lento)
        res = executar_editar_pedido({
            'pedido_id': pid, 'data_entrega': (amanha + timedelta(days=1)).isoformat(),
            'observacao': 'não gravar',
            'itens': [{'resolvido': {'tipo': 'receita', 'id': receita.id},
                       'quantidade': 25}],
        }, admin_user)
        assert res['ok'] is False and '12:00' in res['erro']
        db.session.expire_all()
        salvo = db.session.get(PedidoLoja, pid)
        assert salvo.data_entrega == amanha
        assert salvo.observacao is None
        assert salvo.itens[0].quantidade == 5


@pytest.mark.parametrize('falha', ['mp', 'lote'])
def test_copilot_validacao_recusada_nao_deixa_edicao_pendente(
        app, loja, admin_user, monkeypatch, falha):
    from app.models import MateriaPrima
    from app.services import pedido_lote
    from app.services.copilot import executar_editar_pedido
    with app.app_context():
        receita = _receita()
        mp = MateriaPrima(nome='Não permitida no pedido', unidade='un',
                          sugerir_pedido_loja=False)
        db.session.add(mp)
        amanha = hoje() + timedelta(days=1)
        pedido = PedidoLoja(loja_id=loja.id, status='confirmado', data_entrega=amanha)
        db.session.add(pedido)
        db.session.flush()
        db.session.add(PedidoItem(pedido_id=pedido.id, receita_id=receita.id,
                                  quantidade=5))
        db.session.commit()
        pid = pedido.id
        instante = [datetime.combine(hoje(), datetime.min.time()).replace(
            hour=11, minute=59, second=59)]
        monkeypatch.setattr(pedido_corte, 'agora', lambda: instante[0])
        if falha == 'lote':
            def validar_lote(itens):
                instante[0] = instante[0].replace(hour=12, minute=0, second=0)
                return ['Quantidade fora do lote']
            monkeypatch.setattr(pedido_lote, 'violacoes_por_ids', validar_lote)
        item = {'tipo': 'mp', 'id': mp.id} if falha == 'mp' else {
            'tipo': 'receita', 'id': receita.id}
        res = executar_editar_pedido({
            'pedido_id': pid, 'data_entrega': (amanha + timedelta(days=1)).isoformat(),
            'observacao': 'não gravar',
            'itens': [{'resolvido': item, 'quantidade': 25}],
        }, admin_user)
        assert res['ok'] is False
        # O dispatcher registra também a tentativa recusada e faz commit.
        # Isso não pode persistir a edição parcial deixada pelo executor.
        db.session.commit()
        db.session.expire_all()
        salvo = db.session.get(PedidoLoja, pid)
        assert salvo.data_entrega == amanha
        assert salvo.observacao is None
        assert salvo.itens[0].quantidade == 5


def test_excecao_no_executor_nao_persiste_remocao_de_itens(
        app, loja, admin_user, monkeypatch, pedidos_antes_do_corte):
    from app.services.copilot import executar_editar_pedido
    with app.app_context():
        receita = _receita()
        pedido = PedidoLoja(loja_id=loja.id, status='confirmado',
                            data_entrega=hoje() + timedelta(days=1))
        db.session.add(pedido)
        db.session.flush()
        db.session.add(PedidoItem(pedido_id=pedido.id, receita_id=receita.id,
                                  quantidade=5))
        db.session.commit()
        pid = pedido.id
        with pytest.raises(ValueError):
            executar_editar_pedido({
                'pedido_id': pid, 'observacao': 'não gravar',
                'itens': [{'resolvido': {'tipo': 'receita', 'id': receita.id},
                           'quantidade': 'inválida'}],
            }, admin_user)
        _as_12h(monkeypatch)
        db.session.commit()
        db.session.expire_all()
        salvo = db.session.get(PedidoLoja, pid)
        assert salvo.observacao is None
        assert salvo.itens[0].quantidade == 5
