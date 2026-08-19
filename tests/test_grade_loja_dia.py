"""Testes da grade loja x dia de uma receita (app.services.previsao_producao.
grade_loja_dia).

Detalha o que o balanco resume por receita:
- firme: pedido REAL nao baixado, por (loja, data_entrega) no horizonte.
- estimado: projecao do previsto, rateada por loja/dia pela participacao
  historica. A soma do estimado num dia fecha no previsto daquele dia.

Trava os pontos de risco: pedido enviado nao conta no firme, cancelado fora,
todas as lojas operacionais viram linha (Industria/inativa fora), e o estimado
bate com o previsto do balanco.
"""
from datetime import timedelta

import pytest

from app.extensions import db
from app.models import Loja, PedidoItem, PedidoLoja, Receita
from app.services.previsao_producao import balanco_industria, grade_loja_dia
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


def _linha(grade, loja_id):
    for l in grade['lojas']:
        if l['loja_id'] == loja_id:
            return l
    return None


def test_receita_inexistente_retorna_none(app):
    assert grade_loja_dia(999999, horizonte_dias=7) is None


def test_firme_celula_com_data(app):
    """Pedido nao baixado entra como firme na celula (loja, data_entrega)."""
    loja = _loja()
    r = _receita()
    d1 = hoje() + timedelta(days=1)
    _pedido(loja, 'pendente', d1, r, 40)

    grade = grade_loja_dia(r.id, horizonte_dias=7)
    linha = _linha(grade, loja.id)
    assert linha is not None
    # celulas[1] = hoje+1
    assert linha['celulas'][1]['firme'] == 40
    assert linha['celulas'][1]['data'] == d1.isoformat()
    assert linha['celulas'][0]['firme'] == 0     # hoje, sem pedido
    assert linha['total_firme'] == 40
    assert grade['total_firme'] == 40
    # totais por dia batem com a celula
    assert grade['totais_dia'][1]['firme'] == 40


def test_firme_ignora_pedido_ja_enviado(app):
    """em_transporte ja baixou o estoque — nao conta no firme (= comprometido
    do balanco)."""
    loja = _loja()
    r = _receita()
    d1 = hoje() + timedelta(days=1)
    _pedido(loja, 'pendente', d1, r, 40)
    _pedido(loja, 'em_transporte', d1, r, 100)

    grade = grade_loja_dia(r.id, horizonte_dias=7)
    assert grade['total_firme'] == 40


def test_cancelado_fora(app):
    """Cancelado nao e demanda real — nem firme nem estimado."""
    loja = _loja()
    r = _receita()
    _pedido(loja, 'cancelado', hoje() + timedelta(days=1), r, 50)
    _pedido(loja, 'cancelado', hoje() - timedelta(days=7), r, 50)

    grade = grade_loja_dia(r.id, horizonte_dias=7)
    assert grade['total_firme'] == 0
    assert grade['total_estimado'] == 0
    assert grade['tem_historico'] is False


def test_estimado_projeta_e_fecha_no_previsto(app):
    """3 ocorrencias do mesmo dia-da-semana -> estimado = media; e o total do
    estimado bate com o previsto do balanco (decomposicao top-down)."""
    loja = _loja()
    r = _receita()
    hoje_d = hoje()
    for semanas in (1, 2, 3):
        _pedido(loja, 'recebido', hoje_d - timedelta(days=7 * semanas), r, 10)

    # Horizonte de 1 dia = so hoje (mesmo dia-da-semana das ocorrencias).
    grade = grade_loja_dia(r.id, horizonte_dias=1, janela_semanas=6)
    linha = _linha(grade, loja.id)
    assert linha['celulas'][0]['firme'] == 0
    assert linha['celulas'][0]['estimado'] == 10
    assert grade['total_estimado'] == 10

    # Bate com o previsto do balanco pra a mesma receita/horizonte.
    bal = balanco_industria(horizonte_dias=1, janela_semanas=6,
                            usar_cache=False)
    it = next(i for i in bal['itens'] if i['receita_id'] == r.id)
    assert grade['total_estimado'] == it['previsto']


def test_estimado_rateado_entre_lojas(app):
    """Previsto do dia rateado entre as lojas pela participacao historica."""
    loja_a = _loja('Loja A')
    loja_b = _loja('Loja B')
    r = _receita()
    hoje_d = hoje()
    # Mesmas 3 datas; A pede 10, B pede 5 -> participacao 2:1.
    for semanas in (1, 2, 3):
        d = hoje_d - timedelta(days=7 * semanas)
        _pedido(loja_a, 'recebido', d, r, 10)
        _pedido(loja_b, 'recebido', d, r, 5)

    grade = grade_loja_dia(r.id, horizonte_dias=1, janela_semanas=6)
    la = _linha(grade, loja_a.id)
    lb = _linha(grade, loja_b.id)
    assert la['celulas'][0]['estimado'] == 10   # previsto_dia 15 * 2/3
    assert lb['celulas'][0]['estimado'] == 5    # previsto_dia 15 * 1/3
    assert grade['total_estimado'] == 15
    # Ordem: maior demanda primeiro.
    assert grade['lojas'][0]['loja_id'] == loja_a.id


def test_lista_todas_lojas_operacionais(app):
    """Todas as lojas operacionais viram linha (mesmo zeradas). Industria e
    inativa ficam de fora — igual ao breakdown do balanco."""
    loja_a = _loja('Loja A')
    _loja('Loja B')                       # zerada, mas deve aparecer
    inativa = Loja(nome='Loja Inativa', ativa=False)
    industria = Loja(nome='Industria', ativa=True)
    db.session.add_all([inativa, industria])
    db.session.commit()

    r = _receita()
    _pedido(loja_a, 'pendente', hoje() + timedelta(days=1), r, 50)

    grade = grade_loja_dia(r.id, horizonte_dias=7)
    nomes = [l['loja_nome'] for l in grade['lojas']]
    assert 'Loja A' in nomes
    assert 'Loja B' in nomes
    assert 'Industria' not in nomes
    assert 'Loja Inativa' not in nomes


def test_colunas_cobrem_o_horizonte(app):
    """Uma coluna por dia do horizonte, comecando em hoje."""
    _loja()
    r = _receita()
    grade = grade_loja_dia(r.id, horizonte_dias=5)
    assert len(grade['dias']) == 5
    assert grade['dias'][0]['data'] == hoje().isoformat()
    assert grade['dias'][4]['data'] == (hoje() + timedelta(days=4)).isoformat()


def _login(client, admin_user):
    client.post('/auth/login',
                data={'login': admin_user.login, 'senha': '123'},
                follow_redirects=True)


def test_rota_renderiza(app, admin_user):
    """GET /producao/painel/receita/<id> renderiza a grade (template valido)."""
    loja = _loja('Loja Centro')
    r = _receita('Pão Francês')
    _pedido(loja, 'pendente', hoje() + timedelta(days=1), r, 40)

    client = app.test_client()
    _login(client, admin_user)
    resp = client.get('/producao/painel/receita/%d?horizonte=7&janela=6' % r.id)
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert 'Pão Francês' in body
    assert 'Loja Centro' in body


def test_rota_receita_inexistente_redireciona(app, admin_user):
    client = app.test_client()
    _login(client, admin_user)
    resp = client.get('/producao/painel/receita/999999')
    assert resp.status_code == 302
    assert '/producao/' in resp.headers['Location']


def test_rota_partial_retorna_so_o_fragmento(app, admin_user):
    """?partial=1 -> fragmento da grade (drop-down inline do balanco): tem a
    tabela mas NAO o layout da pagina (sem doctype, sem 'Voltar ao balanco')."""
    loja = _loja('Loja Centro')
    r = _receita('Pão Francês')
    _pedido(loja, 'pendente', hoje() + timedelta(days=1), r, 40)

    client = app.test_client()
    _login(client, admin_user)
    resp = client.get('/producao/painel/receita/%d?horizonte=7&partial=1' % r.id)
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert 'Loja Centro' in body            # conteudo da grade presente
    assert 'data-grade-css' in body         # estilo da grade (dedupe no painel)
    assert '<!DOCTYPE' not in body          # nao e pagina inteira
    assert 'Voltar ao balanço' not in body  # cabecalho so existe no standalone


def test_rota_partial_via_xhr(app, admin_user):
    """X-Requested-With tambem dispara o fragmento (sem ?partial)."""
    loja = _loja('Loja Centro')
    r = _receita('Pão Francês')
    _pedido(loja, 'pendente', hoje() + timedelta(days=1), r, 40)

    client = app.test_client()
    _login(client, admin_user)
    resp = client.get('/producao/painel/receita/%d' % r.id,
                      headers={'X-Requested-With': 'XMLHttpRequest'})
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert 'Loja Centro' in body
    assert '<!DOCTYPE' not in body


def test_rota_partial_inexistente_retorna_404_fragmento(app, admin_user):
    """Receita inexistente no modo partial -> 404 (fragmento de erro), nao
    redirect (que poluiria o drop-down com a pagina inteira)."""
    client = app.test_client()
    _login(client, admin_user)
    resp = client.get('/producao/painel/receita/999999?partial=1')
    assert resp.status_code == 404
    assert 'não encontrada' in resp.get_data(as_text=True).lower()
