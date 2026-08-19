"""Edição de pedidos existentes pela grade da média (aplicar_grade):
dia SEM pedido vira rascunho; dia COM pedido EDITÁVEL (pendente/confirmado,
único) tem os itens sincronizados (ajusta/adiciona/remove com 0, carimba
modificado_em/por); separado+ e dias com 2 pedidos não são tocados."""
from datetime import timedelta

import pytest

from app.extensions import db
from app.models import Loja, PedidoItem, PedidoLoja, Receita
from app.services.pedidos_semana import aplicar_grade
from app.utils import hoje


@pytest.fixture(autouse=True)
def _hoje_e_segunda_fixa(congela_hoje):
    """Producao seg-sex + janela semanal tornaram o motor weekday-sensivel
    — congela numa SEGUNDA fixa (mesma fixture dos arquivos do cronograma;
    caso real 19/08/2026: test_cronograma_edit quebrou na QUARTA porque o
    indice 3 do grid caiu no sabado bloqueado)."""
    congela_hoje()



def _receita(nome='Croissant'):
    r = Receita(nome=nome, categoria='Croissants', rendimento_qtd=1,
                rendimento_unidade='un', peso_base=100.0)
    db.session.add(r)
    db.session.commit()
    return r


def _loja(nome='Loja A'):
    loja = Loja(nome=nome, ativa=True)
    db.session.add(loja)
    db.session.commit()
    return loja


def _pedido(loja, data, itens, status='pendente'):
    """itens = [(receita, qtd)] ou [(receita, qtd, estado)]."""
    p = PedidoLoja(loja_id=loja.id, status=status, data_entrega=data,
                   data_pedido=hoje())
    db.session.add(p)
    db.session.flush()
    for it in itens:
        r, q = it[0], it[1]
        est = it[2] if len(it) > 2 else None
        db.session.add(PedidoItem(pedido_id=p.id, receita_id=r.id,
                                  quantidade=q, estado=est))
    db.session.commit()
    return p


def test_atualiza_qtd_de_pedido_pendente(app, admin_user):
    loja = _loja()
    r = _receita()
    d = hoje() + timedelta(days=1)
    p = _pedido(loja, d, [(r, 100)])

    res = aplicar_grade([{'loja_id': loja.id, 'data_entrega': d,
                          'itens': [{'receita_id': r.id, 'qtd': 80}]}],
                        admin_user.id)
    assert res['atualizados'] == 1
    assert res['itens_ajustados'] == 1
    assert res['criados'] == 0
    item = PedidoItem.query.filter_by(pedido_id=p.id).one()
    assert item.quantidade == 80
    p = db.session.get(PedidoLoja, p.id)
    assert p.modificado_em is not None                 # trilha de auditoria
    assert p.modificado_por_id == admin_user.id


def test_zero_remove_item_e_novo_adiciona(app, admin_user):
    loja = _loja()
    r1 = _receita('Croissant')
    r2 = _receita('Pão')
    d = hoje() + timedelta(days=1)
    p = _pedido(loja, d, [(r1, 50)])

    aplicar_grade([{'loja_id': loja.id, 'data_entrega': d,
                    'itens': [{'receita_id': r1.id, 'qtd': 0},
                              {'receita_id': r2.id, 'qtd': 30}]}],
                  admin_user.id)
    itens = PedidoItem.query.filter_by(pedido_id=p.id).all()
    assert len(itens) == 1
    assert itens[0].receita_id == r2.id and itens[0].quantidade == 30


def test_valor_igual_e_noop_sem_carimbo(app, admin_user):
    """Submeter a grade sem mudar nada não marca o pedido como modificado."""
    loja = _loja()
    r = _receita()
    d = hoje() + timedelta(days=1)
    p = _pedido(loja, d, [(r, 100)])

    res = aplicar_grade([{'loja_id': loja.id, 'data_entrega': d,
                          'itens': [{'receita_id': r.id, 'qtd': 100}]}],
                        admin_user.id)
    assert res['atualizados'] == 0
    assert db.session.get(PedidoLoja, p.id).modificado_em is None


def test_separado_nao_e_tocado(app, admin_user):
    loja = _loja()
    r = _receita()
    d = hoje() + timedelta(days=1)
    p = _pedido(loja, d, [(r, 100)], status='separado')

    res = aplicar_grade([{'loja_id': loja.id, 'data_entrega': d,
                          'itens': [{'receita_id': r.id, 'qtd': 10}]}],
                        admin_user.id)
    assert res['pulados_nao_editavel'] == 1
    assert PedidoItem.query.filter_by(pedido_id=p.id).one().quantidade == 100


def test_dois_pedidos_no_dia_nao_toca(app, admin_user):
    loja = _loja()
    r = _receita()
    d = hoje() + timedelta(days=1)
    _pedido(loja, d, [(r, 40)])
    _pedido(loja, d, [(r, 60)])

    res = aplicar_grade([{'loja_id': loja.id, 'data_entrega': d,
                          'itens': [{'receita_id': r.id, 'qtd': 10}]}],
                        admin_user.id)
    assert res['pulados_multiplos'] == 1
    assert sorted(i.quantidade for i in PedidoItem.query.all()) == [40, 60]


def test_item_com_estados_e_ambiguo_nao_mexe(app, admin_user):
    """Receita com 2 linhas no pedido (assado + backup): a grade mostra a soma;
    ajustar seria ambíguo — não toca e reporta."""
    loja = _loja()
    r = _receita()
    d = hoje() + timedelta(days=1)
    p = _pedido(loja, d, [(r, 30, 'assado'), (r, 20, 'backup')])

    res = aplicar_grade([{'loja_id': loja.id, 'data_entrega': d,
                          'itens': [{'receita_id': r.id, 'qtd': 10}]}],
                        admin_user.id)
    assert res['itens_ambiguos'] == 1
    assert res['atualizados'] == 0
    assert sorted(i.quantidade for i in
                  PedidoItem.query.filter_by(pedido_id=p.id)) == [20, 30]


def test_dia_sem_pedido_cria_rascunho(app, admin_user):
    """Regressão: o caminho de criação continua o mesmo (zeros ignorados)."""
    loja = _loja()
    r = _receita()
    r2 = _receita('Pão')
    d = hoje() + timedelta(days=2)
    res = aplicar_grade([{'loja_id': loja.id, 'data_entrega': d,
                          'itens': [{'receita_id': r.id, 'qtd': 25},
                                    {'receita_id': r2.id, 'qtd': 0}]}],
                        admin_user.id)
    assert res['criados'] == 1 and res['itens'] == 1
    p = PedidoLoja.query.one()
    assert p.status == 'pendente'
    assert PedidoItem.query.filter_by(pedido_id=p.id).one().quantidade == 25


# ── grade da média expõe editáveis + rota atualiza ──────────────────────────
def test_media_expoe_dias_editaveis(app):
    from app.services.previsao_producao import media_semanal_pedidos
    loja = _loja()
    r = _receita()
    hoje_d = hoje()
    for sem in (1, 2):
        db.session.add(PedidoLoja(loja_id=loja.id, status='recebido',
                                  data_entrega=hoje_d - timedelta(days=7 * sem),
                                  data_pedido=hoje_d))
        db.session.flush()
        db.session.add(PedidoItem(
            pedido_id=PedidoLoja.query.order_by(
                PedidoLoja.id.desc()).first().id,
            receita_id=r.id, quantidade=50))
    db.session.commit()
    d_edit = hoje_d + timedelta(days=1)
    d_trava = hoje_d + timedelta(days=2)
    _pedido(loja, d_edit, [(r, 10)], status='pendente')
    _pedido(loja, d_trava, [(r, 10)], status='separado')

    grade = media_semanal_pedidos(horizonte_dias=7, janela_semanas=6,
                                  inicio_offset_dias=0)
    lj = next(e for e in grade['lojas'] if e['loja_id'] == loja.id)
    assert d_edit.isoformat() in lj['editaveis']
    assert d_trava.isoformat() not in lj['editaveis']
    assert d_trava.isoformat() in lj['ja_tem']


def test_rota_gerar_atualiza_pedido_existente(app, admin_user):
    loja = _loja()
    r = _receita()
    d = hoje() + timedelta(days=1)
    p = _pedido(loja, d, [(r, 100)])
    client = app.test_client()
    client.post('/auth/login', data={'login': admin_user.login, 'senha': '123'},
                follow_redirects=True)
    resp = client.post('/producao/pedidos-semana/gerar', data={
        'origem': 'media', 'so_loja': str(loja.id),
        'qtd|%d|%s|%d' % (loja.id, d.isoformat(), r.id): '77'})
    assert resp.status_code == 302
    assert PedidoItem.query.filter_by(pedido_id=p.id).one().quantidade == 77


def test_rota_media_renderiza_celula_editavel(app, admin_user):
    """Dia com pedido pendente: célula azul HABILITADA; dia separado: disabled."""
    loja = _loja()
    r = _receita()
    hoje_d = hoje()
    for sem in (1, 2):
        p_h = PedidoLoja(loja_id=loja.id, status='recebido',
                         data_entrega=hoje_d - timedelta(days=7 * sem),
                         data_pedido=hoje_d)
        db.session.add(p_h)
        db.session.flush()
        db.session.add(PedidoItem(pedido_id=p_h.id, receita_id=r.id,
                                  quantidade=50))
    db.session.commit()
    _pedido(loja, hoje_d + timedelta(days=1), [(r, 88)], status='pendente')

    client = app.test_client()
    client.post('/auth/login', data={'login': admin_user.login, 'senha': '123'},
                follow_redirects=True)
    body = client.get('/producao/pedidos-semana/media?horizonte=7&janela=6'
                      '&inicio=0').get_data(as_text=True)
    assert 'ATUALIZAR o pedido' in body                # tooltip da célula editável
    assert 'value="88"' in body                        # valor do pedido na célula


# ── botão "↻ atualizar" por dia (so_dia) + edição na tela venda+estoque ─────
def test_so_dia_atualiza_apenas_aquele_dia(app, admin_user):
    """O botão do cabeçalho manda a grade toda, mas só o (loja, dia) do botão
    é aplicado — os outros dias/lojas ficam intactos."""
    loja = _loja()
    r = _receita()
    d1 = hoje() + timedelta(days=1)
    d2 = hoje() + timedelta(days=2)
    p1 = _pedido(loja, d1, [(r, 100)])
    p2 = _pedido(loja, d2, [(r, 200)])

    client = app.test_client()
    client.post('/auth/login', data={'login': admin_user.login, 'senha': '123'},
                follow_redirects=True)
    resp = client.post('/producao/pedidos-semana/gerar', data={
        'origem': 'media',
        'so_dia': '%d|%s' % (loja.id, d1.isoformat()),
        'qtd|%d|%s|%d' % (loja.id, d1.isoformat(), r.id): '70',
        'qtd|%d|%s|%d' % (loja.id, d2.isoformat(), r.id): '999',  # ignorado
    })
    assert resp.status_code == 302
    assert PedidoItem.query.filter_by(pedido_id=p1.id).one().quantidade == 70
    assert PedidoItem.query.filter_by(pedido_id=p2.id).one().quantidade == 200


def test_estoque_expoe_editaveis_e_renderiza_botao(app, admin_user):
    """A tela venda+estoque também destrava dia editável e mostra o botão
    ↻ atualizar no cabeçalho do dia."""
    from datetime import datetime, time

    from app.models import EstoqueLoja, MovEstoqueLoja
    from app.services.previsao_producao import sugerir_pedidos_por_venda
    loja = _loja()
    r = _receita()
    el = EstoqueLoja(loja_id=loja.id, receita_id=r.id, quantidade=5)
    db.session.add(el)
    db.session.flush()
    db.session.add(MovEstoqueLoja(
        estoque_loja_id=el.id, tipo='venda_seru', quantidade=3,
        data=datetime.combine(hoje() - timedelta(days=7), time(12, 0)),
        referencia='t'))
    db.session.commit()
    d = hoje() + timedelta(days=1)
    _pedido(loja, d, [(r, 44)], status='pendente')

    grade = sugerir_pedidos_por_venda(horizonte_dias=7, janela_semanas=6,
                                      inicio_offset_dias=0)
    lj = next(e for e in grade['lojas'] if e['loja_id'] == loja.id)
    assert d.isoformat() in lj['editaveis']

    client = app.test_client()
    client.post('/auth/login', data={'login': admin_user.login, 'senha': '123'},
                follow_redirects=True)
    body = client.get('/producao/pedidos-semana/estoque?horizonte=7&janela=6'
                      '&inicio=0').get_data(as_text=True)
    assert 'btn-atualizar-dia' in body
    assert 'so_dia' in body
    assert 'value="44"' in body                       # pedido na célula azul


def test_media_renderiza_botao_atualizar_dia(app, admin_user):
    loja = _loja()
    r = _receita()
    hoje_d = hoje()
    for sem in (1, 2):
        _pedido(loja, hoje_d - timedelta(days=7 * sem), [(r, 50)],
                status='recebido')
    _pedido(loja, hoje_d + timedelta(days=1), [(r, 10)], status='pendente')

    client = app.test_client()
    client.post('/auth/login', data={'login': admin_user.login, 'senha': '123'},
                follow_redirects=True)
    body = client.get('/producao/pedidos-semana/media?horizonte=7&janela=6'
                      '&inicio=0').get_data(as_text=True)
    assert 'btn-atualizar-dia' in body
    assert '%d|%s' % (loja.id, (hoje_d + timedelta(days=1)).isoformat()) in body
