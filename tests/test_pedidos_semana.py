"""Testes da Fatia 2 — geracao de pedidos da semana a partir do historico.

- sugerir_pedidos_semana: propoe itens por (loja, dia de entrega) com a mesma
  matematica da grade; marca ja_tem_pedido onde a loja ja pediu.
- criar_pedidos_rascunho: cria PedidoLoja 'pendente' + itens; pula duplicata.
- rotas GET (preview) e POST (gerar).
"""
from datetime import timedelta

import pytest

from app.extensions import db
from app.models import Loja, PedidoItem, PedidoLoja, Receita
from app.services.pedidos_semana import criar_pedidos_rascunho
from app.services.previsao_producao import sugerir_pedidos_semana
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


def test_loja_intermitente_nao_e_diluida(app):
    """Loja que pede o item em ALGUMAS semanas (intermitente) recebe a previsão
    do tamanho TÍPICO do pedido dela — não a média diluída pelo total da
    operação (o 'pedido picado de cookie' que o dono apontou)."""
    forte = _loja('Forte')
    inter = _loja('Inter')
    r = _receita('Cookie')
    hoje_d = hoje()
    # forte pede 10 toda semana (4 semanas, mesmo dow); inter só em 2 das 4
    for i in (1, 2, 3, 4):
        _pedido(forte, 'recebido', hoje_d - timedelta(days=7 * i), r, 10)
    for i in (1, 2):
        _pedido(inter, 'recebido', hoje_d - timedelta(days=7 * i), r, 10)

    sug = sugerir_pedidos_semana(horizonte_dias=7, janela_semanas=6)
    qtds = {}
    for la in sug['lojas']:
        item = next((it for it in la['dias'][0]['itens']
                     if it['receita_id'] == r.id), None)
        if item:
            qtds[la['loja_id']] = item['qtd']
    assert qtds.get(forte.id) == 10
    assert qtds.get(inter.id) == 10   # diluído daria 5 (20 ÷ 4 datas da operação)


def test_padronizar_qtd():
    """Arredondamento pro lote (pacote) + piso. Não pedir picado."""
    from app.services.previsao_producao import _padronizar_qtd
    assert _padronizar_qtd(7, None, None) == 7       # sem lote -> passthrough
    assert _padronizar_qtd(7, 1, None) == 7          # lote 1 -> passthrough
    assert _padronizar_qtd(9, 50, None) == 50        # <½ pacote -> 1 pacote
    assert _padronizar_qtd(80, 50, None) == 100      # 1,6 -> 2 pacotes
    assert _padronizar_qtd(60, 50, None) == 50       # 1,2 -> 1 pacote
    assert _padronizar_qtd(31, 20, None) == 40       # 1,55 -> 2 pacotes
    assert _padronizar_qtd(26, 50, 250) == 250       # piso (croissant)
    assert _padronizar_qtd(280, 50, 250) == 300      # 5,6 -> 300 (>=250)
    assert _padronizar_qtd(0, 50, 250) == 0          # não pede -> piso não força


def test_pedido_sai_em_lote_padronizado(app):
    """A sugestão por loja sai no pacote da receita (não picado)."""
    loja = _loja('Loja A')
    r = _receita('Pão Francês')
    r.lote_pedido = 50
    db.session.commit()
    hoje_d = hoje()
    for i in (1, 2, 3):
        _pedido(loja, 'recebido', hoje_d - timedelta(days=7 * i), r, 9)
    sug = sugerir_pedidos_semana(horizonte_dias=7, janela_semanas=6)
    la = _loja_out(sug, loja.id)
    item = next(it for it in la['dias'][0]['itens'] if it['receita_id'] == r.id)
    assert item['qtd'] == 50          # 9 -> 1 pacote de 50


def test_pedido_croissant_respeita_minimo(app):
    """Croissant com lote 50 + mínimo 250 -> loja que pede recebe 250/300."""
    loja = _loja('Loja A')
    r = _receita('Croissant Tradicional')
    r.lote_pedido = 50
    r.minimo_pedido = 250
    db.session.commit()
    hoje_d = hoje()
    for i in (1, 2, 3):
        _pedido(loja, 'recebido', hoje_d - timedelta(days=7 * i), r, 30)
    sug = sugerir_pedidos_semana(horizonte_dias=7, janela_semanas=6)
    la = _loja_out(sug, loja.id)
    item = next(it for it in la['dias'][0]['itens'] if it['receita_id'] == r.id)
    assert item['qtd'] == 250         # 30 -> piso 250


def test_fornada_especial_so_fim_de_semana(app):
    """Fornada especial (sex/sáb/dom) não é sugerida em dia de semana, mesmo com
    histórico forte naquele dia-da-semana."""
    loja = _loja('Loja A')
    r = _receita('Focaccia Gorgonzola')
    r.fornada_especial = True
    db.session.commit()
    hoje_d = hoje()
    # um dia de SEMANA (seg–qui) dentro do horizonte de 7 dias
    alvo = next(hoje_d + timedelta(days=i) for i in range(7)
                if (hoje_d + timedelta(days=i)).weekday() < 4)
    for k in (1, 2, 3):
        _pedido(loja, 'recebido', alvo - timedelta(days=7 * k), r, 20)

    sug = sugerir_pedidos_semana(horizonte_dias=7, janela_semanas=6,
                                 inicio_offset_dias=0)
    dia = next(d for d in _loja_out(sug, loja.id)['dias']
               if d['data'] == alvo.isoformat())
    assert all(it['receita_id'] != r.id for it in dia['itens'])   # dia útil: nada

    # controle: sem a flag, a mesma receita SERIA sugerida nesse dia
    r.fornada_especial = False
    db.session.commit()
    sug2 = sugerir_pedidos_semana(horizonte_dias=7, janela_semanas=6,
                                  inicio_offset_dias=0)
    dia2 = next(d for d in _loja_out(sug2, loja.id)['dias']
                if d['data'] == alvo.isoformat())
    assert any(it['receita_id'] == r.id for it in dia2['itens'])


def test_fornada_especial_nao_projeta_producao_em_dia_util(app):
    """Produção: o balanço não projeta fornada especial em dia útil — senão o
    cronograma mandaria produzir focaccia numa terça (demanda que não existe)."""
    from app.services.previsao_producao import balanco_industria
    loja = _loja('Loja A')
    r = _receita('Focaccia')
    r.fornada_especial = True
    db.session.commit()
    hoje_d = hoje()
    for k in range(1, 5):                       # histórico -> soma_total > 0
        _pedido(loja, 'recebido', hoje_d - timedelta(days=k), r, 20)
    # horizonte de 1 dia caindo num dia ÚTIL (seg–qui)
    offset = next(i for i in range(1, 8)
                  if (hoje_d + timedelta(days=i)).weekday() < 4)
    b = balanco_industria(horizonte_dias=1, inicio_offset_dias=offset,
                          usar_cache=False)
    it = next((x for x in b['itens'] if x['receita_id'] == r.id), None)
    assert it is None or it['previsto'] == 0     # dia útil: não projeta


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


def test_rota_automatica_aposentada_redireciona_pra_media(app, admin_user):
    """A tela de previsão automática foi APOSENTADA (01/07): a rota antiga
    redireciona pra a média semanal (tela principal), preservando os params."""
    client = app.test_client()
    _login(client, admin_user)
    resp = client.get('/producao/pedidos-semana?horizonte=7&janela=6&inicio=2')
    assert resp.status_code == 302
    loc = resp.headers['Location']
    assert '/pedidos-semana/media' in loc
    assert 'inicio=2' in loc


def test_rota_gerar_cria_pedido(app, admin_user):
    loja = _loja('Loja A')
    r = _receita()
    d = (hoje() + timedelta(days=1)).isoformat()

    client = app.test_client()
    _login(client, admin_user)
    resp = client.post('/producao/pedidos-semana/gerar', data={
        'gerar_todas': '1',
        'qtd|%d|%s|%d' % (loja.id, d, r.id): '8',
    })
    assert resp.status_code == 302

    p = PedidoLoja.query.filter_by(loja_id=loja.id).first()
    assert p is not None
    assert p.status == 'pendente'
    assert p.itens[0].quantidade == 8


def test_rota_gerar_so_uma_loja(app, admin_user):
    """so_loja gera SÓ a loja escolhida, mesmo com qtd de outras no form (o dono
    pediu enviar loja a loja, não todas de uma vez)."""
    loja_a = _loja('Loja A')
    loja_b = _loja('Loja B')
    r = _receita()
    d = (hoje() + timedelta(days=1)).isoformat()

    client = app.test_client()
    _login(client, admin_user)
    resp = client.post('/producao/pedidos-semana/gerar', data={
        'so_loja': str(loja_a.id),                       # botão "gerar só esta loja"
        'qtd|%d|%s|%d' % (loja_a.id, d, r.id): '8',
        'qtd|%d|%s|%d' % (loja_b.id, d, r.id): '5',      # loja B no form, mas ignorada
    })
    assert resp.status_code == 302
    assert PedidoLoja.query.filter_by(loja_id=loja_a.id).count() == 1
    assert PedidoLoja.query.filter_by(loja_id=loja_b.id).count() == 0   # B não entrou


def test_rota_gerar_todas_exige_acao_explicita(app, admin_user):
    """Gerar TODAS agora exige gerar_todas=1 (o botão manda). Antes o 'todas'
    era o DEFAULT de qualquer POST — e quando o Safari descartava o so_loja
    do botão (bug de confirm() no onclick), 'só esta loja' virava todas."""
    loja_a = _loja('Loja A')
    loja_b = _loja('Loja B')
    r = _receita()
    d = (hoje() + timedelta(days=1)).isoformat()

    client = app.test_client()
    _login(client, admin_user)
    client.post('/producao/pedidos-semana/gerar', data={
        'gerar_todas': '1',
        'qtd|%d|%s|%d' % (loja_a.id, d, r.id): '8',
        'qtd|%d|%s|%d' % (loja_b.id, d, r.id): '5',
    })
    assert PedidoLoja.query.filter_by(loja_id=loja_a.id).count() == 1
    assert PedidoLoja.query.filter_by(loja_id=loja_b.id).count() == 1


def test_rota_gerar_sem_acao_nao_gera_nada(app, admin_user):
    """Regressão do bug de prod (06/07/2026): o Safari descarta o name/value
    do submit button quando o clique passa por confirm() — o POST chegava sem
    so_loja (só os hidden vazios) e a rota gerava TODAS as lojas. Sem ação
    identificada, NADA pode ser gerado (fail-closed) e o usuário é avisado."""
    loja_a = _loja('Loja A')
    loja_b = _loja('Loja B')
    r = _receita()
    d = (hoje() + timedelta(days=1)).isoformat()

    client = app.test_client()
    _login(client, admin_user)
    resp = client.post('/producao/pedidos-semana/gerar', data={
        'so_loja': '', 'so_dia': '', 'gerar_todas': '',   # hiddens sem JS
        'qtd|%d|%s|%d' % (loja_a.id, d, r.id): '8',
        'qtd|%d|%s|%d' % (loja_b.id, d, r.id): '5',
    }, follow_redirects=True)
    assert PedidoLoja.query.count() == 0
    assert 'Nenhum pedido foi gerado' in resp.get_data(as_text=True)


def test_rota_gerar_so_loja_com_hidden_vazio_junto(app, admin_user):
    """O form manda o hidden so_loja (vazio quando o JS não preencheu) E o
    name/value do botão — vale o primeiro valor NÃO-vazio, em qualquer ordem."""
    loja_a = _loja('Loja A')
    loja_b = _loja('Loja B')
    r = _receita()
    d = (hoje() + timedelta(days=1)).isoformat()

    client = app.test_client()
    _login(client, admin_user)
    client.post('/producao/pedidos-semana/gerar', data={
        'so_loja': ['', str(loja_a.id)],                 # hidden vazio + botão
        'qtd|%d|%s|%d' % (loja_a.id, d, r.id): '8',
        'qtd|%d|%s|%d' % (loja_b.id, d, r.id): '5',
    })
    assert PedidoLoja.query.filter_by(loja_id=loja_a.id).count() == 1
    assert PedidoLoja.query.filter_by(loja_id=loja_b.id).count() == 0


def test_rota_gerar_sem_acao_ajax_responde_400(app, admin_user):
    """No caminho ajax (auto-save) a falta de ação vira JSON 400, não flash."""
    loja = _loja('Loja A')
    r = _receita()
    d = (hoje() + timedelta(days=1)).isoformat()

    client = app.test_client()
    _login(client, admin_user)
    resp = client.post('/producao/pedidos-semana/gerar', data={
        'ajax': '1',
        'qtd|%d|%s|%d' % (loja.id, d, r.id): '8',
    })
    assert resp.status_code == 400
    assert resp.get_json()['ok'] is False
    assert PedidoLoja.query.count() == 0


# ── Entrega antecipada (finalizado antes da data) não trava o dia ──────
#
# Caso real Anesio 08/07/2026: pedido de EMERGÊNCIA criado de madrugada saiu
# no caminhão de hoje, mas nasceu datado de amanhã (não-admin não data pro
# mesmo dia) e foi marcado 'entregue' às 6h30. A grade travava amanhã e o
# dono não conseguia fazer o pedido REAL da data.

def test_entrega_antecipada_nao_trava_dia_na_grade_media(app):
    from app.services.previsao_producao import media_semanal_pedidos
    loja = _loja('Loja Anesio T')
    r = _receita('Cinnamon T')
    amanha = hoje() + timedelta(days=1)
    # histórico no MESMO dia-da-semana de amanhã (senão a média de amanhã é 0,
    # o produto sai da grade e a loja some)
    for semanas in (1, 2, 3):
        _pedido(loja, 'recebido', amanha - timedelta(days=7 * semanas), r, 10)
    # pedido de emergência: data futura, JÁ entregue
    _pedido(loja, 'entregue', amanha, r, 35)

    sug = media_semanal_pedidos(horizonte_dias=2, janela_semanas=6,
                                inicio_offset_dias=1)
    lj = _loja_out(sug, loja.id)
    assert amanha.isoformat() not in lj['ja_tem']       # dia LIVRE
    assert amanha.isoformat() not in lj['editaveis']


def test_entregue_no_proprio_dia_continua_travando(app):
    """Não regride: pedido entregue NO dia (data <= hoje) segue ocupando o
    dia — é a trava anti-duplicação de sempre."""
    from app.services.previsao_producao import media_semanal_pedidos
    loja = _loja('Loja B T')
    r = _receita('Croissant T')
    hoje_d = hoje()
    for semanas in (1, 2, 3):
        _pedido(loja, 'recebido', hoje_d - timedelta(days=7 * semanas), r, 10)
    _pedido(loja, 'entregue', hoje_d, r, 20)            # entregue HOJE, de hoje

    sug = media_semanal_pedidos(horizonte_dias=2, janela_semanas=6,
                                inicio_offset_dias=0)
    lj = _loja_out(sug, loja.id)
    assert hoje_d.isoformat() in lj['ja_tem']           # continua travado
    assert hoje_d.isoformat() not in lj['editaveis']


def test_gerar_cria_pedido_real_apesar_do_antecipado(app):
    """aplicar_grade cria o pedido do dia mesmo com o antecipado lá."""
    from app.services.pedidos_semana import aplicar_grade
    loja = _loja('Loja C T')
    r = _receita('Sourdough T')
    amanha = hoje() + timedelta(days=1)
    _pedido(loja, 'entregue', amanha, r, 35)            # emergência já entregue

    res = aplicar_grade([{'loja_id': loja.id, 'data_entrega': amanha,
                          'itens': [{'receita_id': r.id, 'qtd': 12}]}],
                        user_id=None)
    assert res['criados'] == 1
    novos = (PedidoLoja.query
             .filter_by(loja_id=loja.id, data_entrega=amanha,
                        status='pendente').all())
    assert len(novos) == 1
    assert novos[0].itens[0].quantidade == 12


def test_grade_venda_estoque_tambem_ignora_antecipado(app):
    from app.services.previsao_producao import sugerir_pedidos_por_venda
    loja = _loja('Loja D T')
    r = _receita('Brioche T')
    amanha = hoje() + timedelta(days=1)
    _pedido(loja, 'entregue', amanha, r, 35)

    sug = sugerir_pedidos_por_venda(horizonte_dias=2, janela_semanas=6,
                                    inicio_offset_dias=1)
    lj = _loja_out(sug, loja.id)
    # loja pode nem aparecer (sem venda/estoque) — se aparecer, dia livre
    if lj is not None:
        assert amanha.isoformat() not in lj['ja_tem']
