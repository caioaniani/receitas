"""Testes da Fatia 2 — geracao de pedidos da semana a partir do historico.

- sugerir_pedidos_semana: propoe itens por (loja, dia de entrega) com a mesma
  matematica da grade; marca ja_tem_pedido onde a loja ja pediu.
- criar_pedidos_rascunho: cria PedidoLoja 'pendente' + itens; pula duplicata.
- rotas GET (preview) e POST (gerar).
"""
from datetime import timedelta

from app.extensions import db
from app.models import Loja, PedidoItem, PedidoLoja, Receita
from app.services.pedidos_semana import criar_pedidos_rascunho
from app.services.previsao_producao import sugerir_pedidos_semana
from app.utils import hoje


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


def _pedido(loja, status, data_entrega, receita, qtd):
    p = PedidoLoja(loja_id=loja.id, status=status, data_entrega=data_entrega,
                   data_pedido=data_entrega)
    db.session.add(p)
    db.session.flush()
    db.session.add(PedidoItem(pedido_id=p.id, receita_id=receita.id,
                              quantidade=qtd))
    db.session.commit()
    return p


def _login(client, admin_user):
    client.post('/auth/login',
                data={'login': admin_user.login, 'senha': '123'},
                follow_redirects=True)


def _loja_out(sug, loja_id):
    return next((l for l in sug['lojas'] if l['loja_id'] == loja_id), None)


def test_sugerir_propoe_por_loja_dia(app):
    """3 ocorrencias no mesmo dia-da-semana -> sugere a media naquele dia."""
    loja = _loja('Loja A')
    r = _receita()
    hoje_d = hoje()
    for semanas in (1, 2, 3):
        _pedido(loja, 'recebido', hoje_d - timedelta(days=7 * semanas), r, 10)

    sug = sugerir_pedidos_semana(horizonte_dias=7, janela_semanas=6)
    la = _loja_out(sug, loja.id)
    assert la is not None
    dia0 = la['dias'][0]                     # hoje (mesmo dow do historico)
    assert dia0['ja_tem_pedido'] is False
    assert any(it['receita_id'] == r.id and it['qtd'] == 10
               for it in dia0['itens'])


def test_receita_insumo_nao_e_sugerida(app):
    """Receita marcada como insumo/etapa (sugerir_pedido_loja=False) — ex: Creme
    de Amêndoas — nunca entra na sugestão, mesmo com histórico forte."""
    loja = _loja('Loja A')
    r = _receita('Creme de Amêndoas')
    r.sugerir_pedido_loja = False
    db.session.commit()
    hoje_d = hoje()
    for semanas in (1, 2, 3):
        _pedido(loja, 'recebido', hoje_d - timedelta(days=7 * semanas), r, 10)

    sug = sugerir_pedidos_semana(horizonte_dias=7, janela_semanas=6)
    la = _loja_out(sug, loja.id)
    assert all(not any(it['receita_id'] == r.id for it in dia['itens'])
               for dia in la['dias'])


def test_pedido_avulso_unico_nao_vira_sugestao(app):
    """Item pedido UMA só vez (avulso/errado) não é sugerido; a partir de 2
    datas distintas, passa a ser (mata o '1 creme de amêndoas')."""
    loja = _loja('Loja A')
    r = _receita('Croissant')
    hoje_d = hoje()
    _pedido(loja, 'recebido', hoje_d - timedelta(days=7), r, 10)   # 1 ocorrência
    sug = sugerir_pedidos_semana(horizonte_dias=7, janela_semanas=6)
    la = _loja_out(sug, loja.id)
    assert all(not any(it['receita_id'] == r.id for it in dia['itens'])
               for dia in la['dias'])

    _pedido(loja, 'recebido', hoje_d - timedelta(days=14), r, 10)  # 2a ocorrência
    sug2 = sugerir_pedidos_semana(horizonte_dias=7, janela_semanas=6)
    la2 = _loja_out(sug2, loja.id)
    assert any(it['receita_id'] == r.id for it in la2['dias'][0]['itens'])


def test_sugerir_marca_ja_tem_pedido(app):
    """Onde a loja ja tem pedido nao-cancelado, marca ja_tem_pedido."""
    loja = _loja('Loja A')
    r = _receita()
    hoje_d = hoje()
    for semanas in (1, 2, 3):
        _pedido(loja, 'recebido', hoje_d - timedelta(days=7 * semanas), r, 10)
    _pedido(loja, 'pendente', hoje_d + timedelta(days=1), r, 5)

    sug = sugerir_pedidos_semana(horizonte_dias=7, janela_semanas=6)
    la = _loja_out(sug, loja.id)
    assert la['dias'][1]['ja_tem_pedido'] is True   # hoje+1 ja tem pedido


def test_sugerir_so_lojas_operacionais(app):
    """Industria e loja inativa nao entram nas linhas."""
    _loja('Loja A')
    Industria = Loja(nome='Industria', ativa=True)
    inativa = Loja(nome='Loja Inativa', ativa=False)
    db.session.add_all([Industria, inativa])
    db.session.commit()

    sug = sugerir_pedidos_semana(horizonte_dias=7)
    nomes = [l['loja_nome'] for l in sug['lojas']]
    assert 'Loja A' in nomes
    assert 'Industria' not in nomes
    assert 'Loja Inativa' not in nomes


def test_criar_rascunho_cria_pendente(app, admin_user):
    loja = _loja('Loja A')
    r = _receita()
    d = hoje() + timedelta(days=1)
    res = criar_pedidos_rascunho(
        [{'loja_id': loja.id, 'data_entrega': d,
          'itens': [{'receita_id': r.id, 'qtd': 12}]}], admin_user.id)
    assert res['criados'] == 1
    assert res['itens'] == 1

    p = PedidoLoja.query.filter_by(loja_id=loja.id, data_entrega=d).first()
    assert p is not None
    assert p.status == 'pendente'
    assert p.criado_por == admin_user.id
    assert len(p.itens) == 1
    assert p.itens[0].receita_id == r.id
    assert p.itens[0].quantidade == 12


def test_criar_rascunho_pula_existente(app, admin_user):
    """Anti-duplicacao: nao cria onde a loja ja tem pedido nao-cancelado."""
    loja = _loja('Loja A')
    r = _receita()
    d = hoje() + timedelta(days=1)
    _pedido(loja, 'confirmado', d, r, 3)

    res = criar_pedidos_rascunho(
        [{'loja_id': loja.id, 'data_entrega': d,
          'itens': [{'receita_id': r.id, 'qtd': 12}]}], admin_user.id)
    assert res['criados'] == 0
    assert res['pulados_existentes'] == 1
    # so o pedido original existe
    assert PedidoLoja.query.filter_by(loja_id=loja.id).count() == 1


def test_criar_rascunho_ignora_qtd_zero(app, admin_user):
    loja = _loja('Loja A')
    r = _receita()
    d = hoje() + timedelta(days=1)
    res = criar_pedidos_rascunho(
        [{'loja_id': loja.id, 'data_entrega': d,
          'itens': [{'receita_id': r.id, 'qtd': 0}]}], admin_user.id)
    assert res['criados'] == 0
    assert PedidoLoja.query.count() == 0


def test_rota_get_renderiza(app, admin_user):
    loja = _loja('Loja Centro')
    r = _receita()
    hoje_d = hoje()
    for semanas in (1, 2, 3):
        _pedido(loja, 'recebido', hoje_d - timedelta(days=7 * semanas), r, 10)

    client = app.test_client()
    _login(client, admin_user)
    resp = client.get('/producao/pedidos-semana?horizonte=7&janela=6')
    assert resp.status_code == 200
    assert 'Loja Centro' in resp.get_data(as_text=True)


def test_rota_gerar_cria_pedido(app, admin_user):
    loja = _loja('Loja A')
    r = _receita()
    d = (hoje() + timedelta(days=1)).isoformat()

    client = app.test_client()
    _login(client, admin_user)
    resp = client.post('/producao/pedidos-semana/gerar', data={
        'qtd|%d|%s|%d' % (loja.id, d, r.id): '8',
    })
    assert resp.status_code == 302

    p = PedidoLoja.query.filter_by(loja_id=loja.id).first()
    assert p is not None
    assert p.status == 'pendente'
    assert p.itens[0].quantidade == 8
