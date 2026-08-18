"""Modo MANUAL de pedidos da semana (media_semanal_pedidos): devolve a media por
(loja, produto) POR DIA-DA-SEMANA, distribuida pelos dias LIVRES do horizonte
(dia ja pedido nao recebe — input disabled nao iria no POST), pro admin ajustar.
Reusa o POST de pedidos_semana_gerar.
"""
from datetime import timedelta

from app.extensions import db
from app.models import EstoqueLoja, Loja, PedidoItem, PedidoLoja, Receita
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


def _estoque(loja, receita, q, qres=0):
    e = EstoqueLoja(loja_id=loja.id, receita_id=receita.id,
                    quantidade=q, quantidade_reservada=qres)
    db.session.add(e)
    db.session.commit()
    return e


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


def test_media_recencia_ponderada_com_zeros(app):
    """Fase 1 (02/07/2026): a media e RECENCIA-ponderada (meia-vida 21d) com o
    denominador contando as datas do dow DESDE a 1a ocorrencia — mesma
    matematica do balanco. 3 semanas com 100/50/30: a mais recente pesa mais
    (antes era total/janela uniforme = 30, que diluia o sinal)."""
    loja = _loja()
    r = _receita()
    hoje_d = hoje()
    for sem, q in ((1, 100), (2, 50), (3, 30)):
        _pedido(loja, hoje_d - timedelta(days=7 * sem), r, q)

    grade = media_semanal_pedidos(horizonte_dias=7, janela_semanas=6)
    p = _prod(grade, loja.id, r.id)
    # Mesma formula do motor: peso 0.5**(dias/21); denominador = as 3 datas
    # do dow desde a 1a ocorrencia (nao ha gaps). Sem cap (100 < 2.5*mediana).
    pesos = [0.5 ** (7 * sem / 21) for sem in (1, 2, 3)]
    esperado = ((100 * pesos[0] + 50 * pesos[1] + 30 * pesos[2])
                / sum(pesos))
    assert p['media_semanal'] == round(esperado, 1)
    assert sum(p['por_dia']) == int(round(esperado))


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
    entre os dias, e nao marca abaixo_lote. Com a media de recencia, UMA
    ocorrencia na semana passada conta cheia (600) — o decaimento so aparece
    conforme semanas SEM pedido se acumulam no denominador."""
    loja = _loja()
    r = _receita('Croissant')
    r.lote_pedido = 50
    db.session.commit()
    hoje_d = hoje()
    _pedido(loja, hoje_d - timedelta(days=7), r, 600)
    grade = media_semanal_pedidos(horizonte_dias=7, janela_semanas=6)
    p = _prod(grade, loja.id, r.id)
    assert p is not None
    assert p['abaixo_lote'] is False
    assert all(v % 50 == 0 for v in p['por_dia'])   # so caixas inteiras
    assert sum(p['por_dia']) == 600                  # 12 caixas de 50


def test_abaixo_da_caixa_mostra_media_real_sem_forcar(app):
    """Demanda < 1 caixa: aparece com a media REAL, abaixo_lote=True, e NAO eh
    forcada pra 1 caixa (item lento nao super-pedido)."""
    loja = _loja()
    r = _receita('Item Lento')
    r.lote_pedido = 50
    db.session.commit()
    hoje_d = hoje()
    # 18 numa ocorrencia recente -> media 18 (< caixa 50)
    _pedido(loja, hoje_d - timedelta(days=7), r, 18)
    grade = media_semanal_pedidos(horizonte_dias=7, janela_semanas=6)
    p = _prod(grade, loja.id, r.id)
    assert p is not None
    assert p['abaixo_lote'] is True
    assert sum(p['por_dia']) == 18                   # media real, nao forcada a 50
    assert sum(p['por_dia']) < p['lote']


def test_estoque_atual_disponivel(app):
    """Cada produto carrega o estoque DISPONIVEL da loja (fisico - reservado),
    mesma conta da tela venda+estoque. So pra mostrar; nao mexe na media."""
    loja = _loja()
    r = _receita()
    hoje_d = hoje()
    for sem in (1, 2):
        _pedido(loja, hoje_d - timedelta(days=7 * sem), r, 70)
    # 40 fisico, 15 reservado -> 25 disponivel
    _estoque(loja, r, 40, qres=15)

    grade = media_semanal_pedidos(horizonte_dias=7, janela_semanas=6)
    p = _prod(grade, loja.id, r.id)
    assert p is not None
    assert p['estoque_atual'] == 25
    assert p['media_semanal'] == 70.0                # 2x70 no dow; estoque nao mexe


def test_estoque_atual_zero_sem_linha(app):
    """Sem EstoqueLoja pra o par (loja, receita) -> estoque_atual = 0."""
    loja = _loja()
    r = _receita()
    _pedido(loja, hoje() - timedelta(days=7), r, 70)
    grade = media_semanal_pedidos(horizonte_dias=7, janela_semanas=6)
    p = _prod(grade, loja.id, r.id)
    assert p is not None
    assert p['estoque_atual'] == 0


def test_rota_renderiza(app, admin_user):
    app.config['UI_V2_ENABLED'] = False  # contrato da tela CLASSICA (viva via cookie ui_classic/?legacy=1)
    loja = _loja('Loja Centro')
    r = _receita('Pão Francês')
    hoje_d = hoje()
    for sem in (1, 2, 3):
        _pedido(loja, hoje_d - timedelta(days=7 * sem), r, 100)
    _estoque(loja, r, 33, qres=0)               # coluna Estoque mostra o disponivel

    client = app.test_client()
    client.post('/auth/login', data={'login': admin_user.login, 'senha': '123'},
                follow_redirects=True)
    resp = client.get('/producao/pedidos-semana/media?horizonte=7&janela=6&inicio=0')
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert 'Média semanal' in body or 'média semanal' in body
    assert 'Loja Centro' in body
    assert 'Pão Francês' in body
    # coluna ESTOQUE (disponivel da loja) igual a tela venda+estoque
    assert 'Estoque' in body
    assert 'td class="estoque"' in body
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
    # ação explícita via hidden + confirm no listener (nunca onclick inline):
    # o Safari descarta o name/value do botão quando o onclick tem confirm()
    assert 'id="form-gerar-grade"' in body
    assert 'name="gerar_todas"' in body
    assert 'data-confirm=' in body
    assert 'onclick="return confirm' not in body
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


def test_dia_travado_expoe_o_que_foi_pedido(app):
    """Dia com pedido existente: a grade expõe `ja_pedido` (o pedido REAL do
    dia) pra célula travada mostrar o encomendado em vez de um 0 apagado."""
    loja = _loja()
    r = _receita()
    hoje_d = hoje()
    # historico em DOIS dows (hoje + amanha) — com so o dow travado o produto
    # sai da grade (total_alocar=0), regra pre-existente da tela.
    for sem in (1, 2):
        _pedido(loja, hoje_d - timedelta(days=7 * sem), r, 50)        # dow hoje
        _pedido(loja, hoje_d - timedelta(days=7 * sem - 1), r, 50)    # dow amanha
    _pedido(loja, hoje_d, r, 999, status='pendente')    # já pedido HOJE

    grade = media_semanal_pedidos(horizonte_dias=7, janela_semanas=2,
                                  inicio_offset_dias=0)
    p = _prod(grade, loja.id, r.id)
    assert p is not None
    assert p['ja_pedido'][0] == 999                     # o pedido real do dia
    assert sum(p['ja_pedido'][1:]) == 0
    assert p['por_dia'][0] == 0                         # travado: sem sugestão


def test_rota_mostra_pedido_real_no_dia_travado(app, admin_user):
    """A célula travada renderiza o valor JÁ PEDIDO (com estilo tem-pedido),
    não a sugestão."""
    loja = _loja('Loja Centro')
    r = _receita('Pão Francês')
    hoje_d = hoje()
    for sem in (1, 2):
        _pedido(loja, hoje_d - timedelta(days=7 * sem), r, 50)
    _pedido(loja, hoje_d + timedelta(days=2), r, 77, status='pendente')

    client = app.test_client()
    client.post('/auth/login', data={'login': admin_user.login, 'senha': '123'},
                follow_redirects=True)
    resp = client.get('/producao/pedidos-semana/media?horizonte=7&janela=6&inicio=0')
    body = resp.get_data(as_text=True)
    assert 'value="77"' in body                         # o pedido real aparece
    assert 'tem-pedido' in body                         # com o estilo próprio
    assert 'FOI pedido' in body                         # tooltip explica


def test_item_so_com_pedido_no_dia_travado_aparece(app):
    """Produto cuja única atividade cai em dia travado NÃO some mais: aparece
    zerado com o ja_pedido preenchido (pedido do dono 02/07). Cobre também o
    item SEM histórico que já tem pedido futuro."""
    loja = _loja()
    r = _receita()
    hoje_d = hoje()
    # histórico só no dow de HOJE — e hoje está travado (pedido feito)
    for sem in (1, 2):
        _pedido(loja, hoje_d - timedelta(days=7 * sem), r, 50)
    _pedido(loja, hoje_d, r, 999, status='pendente')
    # item NOVO sem histórico nenhum, mas com pedido amanhã
    r2 = _receita('Item Novo')
    _pedido(loja, hoje_d + timedelta(days=1), r2, 33, status='pendente')

    grade = media_semanal_pedidos(horizonte_dias=7, janela_semanas=2,
                                  inicio_offset_dias=0)
    p = _prod(grade, loja.id, r.id)
    assert p is not None                         # não some mais
    assert p['ja_pedido'][0] == 999
    assert sum(p['por_dia']) == 0                # sem sugestão (dias travados)
    p2 = _prod(grade, loja.id, r2.id)
    assert p2 is not None                        # sem histórico, mas pedido
    assert p2['ja_pedido'][1] == 33
    assert p2['media_semanal'] == 0


# ── auto-save (ajax=1): salva a coluna ao terminar de digitar ──────────────
def test_gerar_ajax_atualiza_pedido_e_retorna_json(app, admin_user):
    """POST com ajax=1 + so_dia atualiza o pedido existente e responde JSON
    (sem redirect) — é o caminho do auto-save da tela da média."""
    loja = _loja('Loja Ajax')
    r = _receita('Pão Ajax')
    d = hoje() + timedelta(days=2)
    ped = _pedido(loja, d, r, 10, status='pendente')     # editável

    client = app.test_client()
    client.post('/auth/login', data={'login': admin_user.login, 'senha': '123'},
                follow_redirects=True)
    resp = client.post('/producao/pedidos-semana/gerar', data={
        'origem': 'media', 'ajax': '1',
        'so_dia': '%d|%s' % (loja.id, d.isoformat()),
        'qtd|%d|%s|%d' % (loja.id, d.isoformat(), r.id): '25',
    })
    assert resp.status_code == 200                       # JSON, não 302
    j = resp.get_json()
    assert j['ok'] is True and j['mudou'] is True
    it = PedidoItem.query.filter_by(pedido_id=ped.id, receita_id=r.id).first()
    assert it.quantidade == 25                           # pedido atualizado


def test_gerar_ajax_sem_mudanca_avisa(app, admin_user):
    """Coluna igual ao pedido -> mudou=False (indicador '✓ sem mudança')."""
    loja = _loja('Loja Igual')
    r = _receita('Pão Igual')
    d = hoje() + timedelta(days=2)
    _pedido(loja, d, r, 10, status='pendente')

    client = app.test_client()
    client.post('/auth/login', data={'login': admin_user.login, 'senha': '123'},
                follow_redirects=True)
    resp = client.post('/producao/pedidos-semana/gerar', data={
        'origem': 'media', 'ajax': '1',
        'so_dia': '%d|%s' % (loja.id, d.isoformat()),
        'qtd|%d|%s|%d' % (loja.id, d.isoformat(), r.id): '10',   # mesmo valor
    })
    assert resp.status_code == 200
    j = resp.get_json()
    assert j['ok'] is True and j['mudou'] is False


def test_rota_media_tem_autosave_no_js(app, admin_user):
    """A tela carrega o bloco de auto-save (debounce + fetch ajax=1)."""
    loja = _loja('Loja JS')
    r = _receita('Pão JS')
    for sem in (1, 2):
        _pedido(loja, hoje() - timedelta(days=7 * sem), r, 50)
    client = app.test_client()
    client.post('/auth/login', data={'login': admin_user.login, 'senha': '123'},
                follow_redirects=True)
    body = client.get('/producao/pedidos-semana/media').get_data(as_text=True)
    assert "fd.set('ajax', '1')" in body                 # fetch do auto-save
    assert 'salvando' in body                            # indicador de status


# ── minimo_pedido no motor de média (11/07/2026, aprovado pelo dono) ───────
def _historico_todos_os_dias(loja, receita, qtd, semanas=6):
    hoje_d = hoje()
    for sem in range(1, semanas + 1):
        for dow in range(7):
            d = hoje_d - timedelta(days=7 * sem)
            d = d - timedelta(days=d.weekday()) + timedelta(days=dow)
            if d < hoje_d:
                _pedido(loja, d, receita, qtd)


def test_minimo_concentra_entregas_sem_mudar_o_total(app):
    """Dia com sugestão abaixo do mínimo é FUNDIDO no dia de maior alocação:
    o total da semana não muda, e nenhum dia fica entre 0 e o mínimo."""
    loja = _loja()
    r = _receita()
    r.minimo_pedido = 10
    db.session.commit()
    _historico_todos_os_dias(loja, r, 4)        # média ~4/dia (< mínimo 10)

    grade = media_semanal_pedidos(horizonte_dias=7, janela_semanas=6,
                                  inicio_offset_dias=1)
    p = _prod(grade, loja.id, r.id)
    assert p is not None
    total = sum(p['por_dia'])
    assert total >= 10                          # semana fecha o mínimo
    assert all(v == 0 or v >= 10 for v in p['por_dia'])
    assert p['abaixo_minimo'] is False
    # o total continua o da média (fusão não infla): ~4*7 = 28 +- arred.
    assert total <= 30


def test_semana_abaixo_do_minimo_nao_forca_e_marca(app):
    """Semana inteira abaixo do mínimo: mantém a demanda real (não infla)
    e marca `abaixo_minimo` — mesma decisão do 'abaixo da caixa'."""
    loja = _loja()
    r = _receita()
    r.minimo_pedido = 50
    db.session.commit()
    _historico_todos_os_dias(loja, r, 2)        # ~14/semana < mínimo 50

    grade = media_semanal_pedidos(horizonte_dias=7, janela_semanas=6,
                                  inicio_offset_dias=1)
    p = _prod(grade, loja.id, r.id)
    assert p is not None
    assert 0 < sum(p['por_dia']) < 50           # demanda real preservada
    assert p['abaixo_minimo'] is True


def test_minimo_respeita_caixa(app):
    """Fusão preserva múltiplos de caixa (soma de múltiplos é múltiplo)."""
    loja = _loja()
    r = _receita()
    r.minimo_pedido = 12
    r.lote_pedido = 6
    db.session.commit()
    _historico_todos_os_dias(loja, r, 4)        # ~28/sem -> caixas de 6

    grade = media_semanal_pedidos(horizonte_dias=7, janela_semanas=6,
                                  inicio_offset_dias=1)
    p = _prod(grade, loja.id, r.id)
    assert p is not None
    assert all(v % 6 == 0 for v in p['por_dia'])
    assert all(v == 0 or v >= 12 for v in p['por_dia'])


def test_sem_minimo_comportamento_intacto(app):
    """Receita sem minimo_pedido: distribuição idêntica à de antes."""
    loja = _loja()
    r = _receita()
    _historico_todos_os_dias(loja, r, 4)
    grade = media_semanal_pedidos(horizonte_dias=7, janela_semanas=6,
                                  inicio_offset_dias=1)
    p = _prod(grade, loja.id, r.id)
    assert p is not None
    assert p['abaixo_minimo'] is False
    assert sum(1 for v in p['por_dia'] if v > 0) >= 6   # segue diluído
