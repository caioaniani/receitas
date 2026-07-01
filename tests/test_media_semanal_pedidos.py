"""Modo MANUAL de pedidos da semana (media_semanal_pedidos): devolve a media por
(loja, produto) POR DIA-DA-SEMANA, distribuida pelos dias LIVRES do horizonte
(dia ja pedido nao recebe — input disabled nao iria no POST), pro admin ajustar.
Reusa o POST de pedidos_semana_gerar.
"""
from datetime import timedelta

from app.extensions import db
from app.models import Loja, PedidoItem, PedidoLoja, Receita
from app.services.previsao_producao import media_semanal_pedidos
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


def _pedido(loja, data_entrega, receita, qtd, status='recebido'):
    p = PedidoLoja(loja_id=loja.id, status=status, data_entrega=data_entrega,
                   data_pedido=data_entrega)
    db.session.add(p)
    db.session.flush()
    db.session.add(PedidoItem(pedido_id=p.id, receita_id=receita.id,
                              quantidade=qtd))
    db.session.commit()
    return p


def _prod(grade, loja_id, rid):
    loja = next((entry for entry in grade['lojas']
                 if entry['loja_id'] == loja_id), None)
    if loja is None:
        return None
    return next((p for p in loja['produtos'] if p['receita_id'] == rid), None)


def test_media_concentra_no_dia_da_semana(app):
    """B4: se a loja sempre pede no MESMO dia-da-semana, a sugestao CONCENTRA
    naquele dia no horizonte — nao espalha igual pelos 7 dias (bug antigo)."""
    loja = _loja()
    r = _receita()
    hoje_d = hoje()
    # pedidos sempre no mesmo dow (multiplos de 7 dias atras = dow de HOJE).
    for sem in (1, 2, 3, 4):
        _pedido(loja, hoje_d - timedelta(days=7 * sem), r, 70)

    grade = media_semanal_pedidos(horizonte_dias=7, janela_semanas=4,
                                  inicio_offset_dias=0)
    p = _prod(grade, loja.id, r.id)
    assert p is not None
    assert p['media_semanal'] == 70.0          # 280 / 4 semanas
    assert len(p['por_dia']) == 7
    # horizonte comeca HOJE (mesmo dow do historico) -> tudo no dia 0, zero
    # nos outros dias-da-semana (que a loja nunca pediu).
    assert p['por_dia'][0] == 70
    assert sum(p['por_dia'][1:]) == 0
    assert sum(p['por_dia']) == 70


def test_media_segue_padrao_por_dia_da_semana(app):
    """B4: dia-da-semana com mais venda historica recebe MAIS no horizonte —
    a distribuicao segue o peso de cada dow, nao e plana."""
    loja = _loja()
    r = _receita()
    hoje_d = hoje()
    # dow de HOJE: 60/sem; dow de AMANHA: 20/sem (janela 3).
    for sem in (1, 2, 3):
        _pedido(loja, hoje_d - timedelta(days=7 * sem), r, 60)        # dow hoje
        _pedido(loja, hoje_d - timedelta(days=7 * sem - 1), r, 20)    # dow amanha

    grade = media_semanal_pedidos(horizonte_dias=7, janela_semanas=3,
                                  inicio_offset_dias=0)
    p = _prod(grade, loja.id, r.id)
    assert p is not None
    assert p['por_dia'][0] == 60                # dia 0 = dow de hoje
    assert p['por_dia'][1] == 20                # dia 1 = dow de amanha
    assert sum(p['por_dia'][2:]) == 0
    assert p['media_semanal'] == 80.0           # 60 + 20 por semana


def test_dia_travado_nao_recebe_parcela(app):
    """B2: o balanceamento so cai em dias LIVRES. Um dow travado (loja ja pediu)
    recebe 0 — a parcela nao some no POST nem e re-jogada no dia travado."""
    loja = _loja()
    r = _receita()
    hoje_d = hoje()
    # historico em 2 dias-da-semana: dow de hoje (50/sem) e dow de amanha (50/sem)
    for sem in (1, 2):
        _pedido(loja, hoje_d - timedelta(days=7 * sem), r, 50)        # dow hoje
        _pedido(loja, hoje_d - timedelta(days=7 * sem - 1), r, 50)    # dow amanha
    # a loja JA pediu HOJE -> dia 0 (dow de hoje) travado
    _pedido(loja, hoje_d, r, 999, status='pendente')

    grade = media_semanal_pedidos(horizonte_dias=7, janela_semanas=2,
                                  inicio_offset_dias=0)
    loja_out = next(e for e in grade['lojas'] if e['loja_id'] == loja.id)
    assert hoje_d.isoformat() in loja_out['ja_tem']
    p = _prod(grade, loja.id, r.id)
    assert p is not None
    assert p['por_dia'][0] == 0                 # dia travado: nada
    assert p['por_dia'][1] == 50               # dia livre (dow amanha): a parcela
    assert sum(p['por_dia']) == 50             # nada perdido nem re-jogado


def test_media_usa_total_da_janela(app):
    """A media e o TOTAL na janela / nº de semanas — varias semanas com
    quantidades diferentes."""
    loja = _loja()
    r = _receita()
    hoje_d = hoje()
    # 3 semanas: 100, 50, 30 -> total 180 / 6 semanas (janela) = 30/sem
    for sem, q in ((1, 100), (2, 50), (3, 30)):
        _pedido(loja, hoje_d - timedelta(days=7 * sem), r, q)

    grade = media_semanal_pedidos(horizonte_dias=7, janela_semanas=6)
    p = _prod(grade, loja.id, r.id)
    assert p['media_semanal'] == 30.0
    assert sum(p['por_dia']) == 30


def test_loja_sem_historico_nao_aparece(app):
    """Loja/produto sem pedido na janela nao entra na grade."""
    _loja('Sem Historico')
    _receita('Pao')
    grade = media_semanal_pedidos(horizonte_dias=7, janela_semanas=6)
    assert grade['lojas'] == []


def test_marca_dias_ja_pedidos(app):
    """Dia no horizonte em que a loja ja tem pedido vem em `ja_tem` (a tela
    trava; o gerar pula)."""
    loja = _loja()
    r = _receita()
    hoje_d = hoje()
    for sem in (1, 2):
        _pedido(loja, hoje_d - timedelta(days=7 * sem), r, 70)
    # pedido futuro (no horizonte) -> marca o dia
    futuro = hoje_d + timedelta(days=2)
    _pedido(loja, futuro, r, 5, status='pendente')

    grade = media_semanal_pedidos(horizonte_dias=7, janela_semanas=6,
                                  inicio_offset_dias=0)
    loja_out = next(entry for entry in grade['lojas']
                    if entry['loja_id'] == loja.id)
    assert futuro.isoformat() in loja_out['ja_tem']


def test_expoe_lote_da_receita(app):
    """Cada produto carrega o `lote_pedido` da receita (caixa) — a tela usa pra
    arredondar ao dividir a entrega entre dias escolhidos."""
    loja = _loja()
    r = _receita('Croissant Tradicional')
    r.lote_pedido = 50
    db.session.commit()
    hoje_d = hoje()
    for sem in (1, 2):
        _pedido(loja, hoje_d - timedelta(days=7 * sem), r, 550)

    grade = media_semanal_pedidos(horizonte_dias=7, janela_semanas=6)
    p = _prod(grade, loja.id, r.id)
    assert p is not None
    assert p['lote'] == 50


def test_lote_ausente_vem_zero(app):
    """Receita sem lote_pedido -> lote 0 (a tela divide sem arredondar)."""
    loja = _loja()
    r = _receita()
    hoje_d = hoje()
    _pedido(loja, hoje_d - timedelta(days=7), r, 70)
    grade = media_semanal_pedidos(horizonte_dias=7, janela_semanas=6)
    p = _prod(grade, loja.id, r.id)
    assert p['lote'] == 0


def test_lote_distribui_caixas_inteiras(app):
    """Media que fecha >= 1 caixa -> por_dia em MULTIPLOS do lote, balanceado
    entre os dias, e nao marca abaixo_lote."""
    loja = _loja()
    r = _receita('Croissant')
    r.lote_pedido = 50
    db.session.commit()
    hoje_d = hoje()
    # 600 numa ocorrencia, janela 6 -> media 100/sem; horizonte 7 -> 100 un
    _pedido(loja, hoje_d - timedelta(days=7), r, 600)
    grade = media_semanal_pedidos(horizonte_dias=7, janela_semanas=6)
    p = _prod(grade, loja.id, r.id)
    assert p is not None
    assert p['abaixo_lote'] is False
    assert all(v % 50 == 0 for v in p['por_dia'])   # so caixas inteiras
    assert sum(p['por_dia']) == 100                  # 2 caixas de 50


def test_abaixo_da_caixa_mostra_media_real_sem_forcar(app):
    """Demanda < 1 caixa: aparece com a media REAL, abaixo_lote=True, e NAO eh
    forcada pra 1 caixa (item lento nao super-pedido)."""
    loja = _loja()
    r = _receita('Item Lento')
    r.lote_pedido = 6
    db.session.commit()
    hoje_d = hoje()
    # 18 numa ocorrencia, janela 6 -> media 3/sem (< caixa 6)
    _pedido(loja, hoje_d - timedelta(days=7), r, 18)
    grade = media_semanal_pedidos(horizonte_dias=7, janela_semanas=6)
    p = _prod(grade, loja.id, r.id)
    assert p is not None
    assert p['abaixo_lote'] is True
    assert sum(p['por_dia']) == 3                    # media real, nao forcada a 6
    assert sum(p['por_dia']) < p['lote']


def test_rota_renderiza(app, admin_user):
    loja = _loja('Loja Centro')
    r = _receita('Pão Francês')
    hoje_d = hoje()
    for sem in (1, 2, 3):
        _pedido(loja, hoje_d - timedelta(days=7 * sem), r, 100)

    client = app.test_client()
    client.post('/auth/login', data={'login': admin_user.login, 'senha': '123'},
                follow_redirects=True)
    resp = client.get('/producao/pedidos-semana/media?horizonte=7&janela=6&inicio=0')
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert 'Média semanal' in body or 'média semanal' in body
    assert 'Loja Centro' in body
    assert 'Pão Francês' in body
    # UI de dividir entrega entre dias escolhidos (1 produto, loja ou tudo)
    assert 'btn-dividir' in body
    assert 'btn-dividir-loja' in body
    assert 'btn-dividir-todos' in body
    assert 'modalDividir' in body
    assert 'data-lote' in body
    # geração POR LOJA (botão no card) + origem pra voltar pra esta tela
    assert 'Gerar só esta loja' in body
    assert 'name="so_loja" value="%d"' % loja.id in body
    assert 'name="origem" value="media"' in body
    # atalho direto no menu lateral (PRODUÇÃO → Pedidos da semana)
    assert '/producao/pedidos-semana/media' in body
    assert 'bi-cart-plus' in body


def test_gerar_origem_media_volta_pra_media(app, admin_user):
    """Gerar da tela de média (origem=media) redireciona de volta pra média,
    não pra automática."""
    loja = _loja('Loja A')
    r = _receita('Pão')
    d = (hoje() + timedelta(days=1)).isoformat()
    client = app.test_client()
    client.post('/auth/login', data={'login': admin_user.login, 'senha': '123'},
                follow_redirects=True)
    resp = client.post('/producao/pedidos-semana/gerar', data={
        'origem': 'media', 'so_loja': str(loja.id),
        'qtd|%d|%s|%d' % (loja.id, d, r.id): '8'})
    assert resp.status_code == 302
    assert '/pedidos-semana/media' in resp.headers['Location']
