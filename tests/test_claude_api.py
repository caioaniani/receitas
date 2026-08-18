"""API read-only do assistente (/api/claude/*) — token via CLAUDE_API_TOKEN.

Sem env → 503 (desligada). Token errado → 401. Token certo → JSON do
cronograma de produção (a mesma conta da tela /telaindustriateste).
"""
from datetime import timedelta

from app.extensions import db
from app.models import Loja, PedidoItem, PedidoLoja, Receita
from app.utils import hoje

TOKEN = 'token-de-teste-bem-longo-123'


def _seed(nome='Sourdough'):
    loja = Loja(nome='Loja A', ativa=True)
    r = Receita(nome=nome, categoria='Paes', rendimento_qtd=1,
                rendimento_unidade='un', peso_base=1000.0)
    db.session.add_all([loja, r])
    db.session.commit()
    p = PedidoLoja(loja_id=loja.id, status='pendente',
                   data_entrega=hoje() + timedelta(days=2),
                   data_pedido=hoje())
    db.session.add(p)
    db.session.flush()
    db.session.add(PedidoItem(pedido_id=p.id, receita_id=r.id, quantidade=40))
    db.session.commit()
    return r


def test_sem_env_responde_503(app):
    app.config['CLAUDE_API_TOKEN'] = ''
    resp = app.test_client().get('/api/claude/cronograma')
    assert resp.status_code == 503


def test_token_errado_401(app):
    app.config['CLAUDE_API_TOKEN'] = TOKEN
    resp = app.test_client().get(
        '/api/claude/cronograma',
        headers={'Authorization': 'Bearer errado'})
    assert resp.status_code == 401


def test_token_certo_devolve_cronograma(app):
    app.config['CLAUDE_API_TOKEN'] = TOKEN
    r = _seed()
    resp = app.test_client().get(
        '/api/claude/cronograma?horizonte=7&inicio=0',
        headers={'Authorization': f'Bearer {TOKEN}'})
    assert resp.status_code == 200
    d = resp.get_json()
    assert d['ok'] is True
    rr = next(x for x in d['receitas'] if x['receita_id'] == r.id)
    assert rr['nome'] == 'Sourdough'
    assert sum(c['qtd'] for c in rr['por_dia']) == 40
    assert len(d['dias']) == 7


def test_token_via_query_tambem_funciona(app):
    app.config['CLAUDE_API_TOKEN'] = TOKEN
    _seed('Baguete')
    resp = app.test_client().get(f'/api/claude/cronograma?token={TOKEN}')
    assert resp.status_code == 200
    assert resp.get_json()['ok'] is True


def test_nao_exige_login_mas_exige_token(app):
    """Rota fora do login_manager (integração por token): sem token → 401,
    nunca redirect pra /auth/login."""
    app.config['CLAUDE_API_TOKEN'] = TOKEN
    resp = app.test_client().get('/api/claude/cronograma')
    assert resp.status_code == 401


def test_pedidos_semana_modo_venda_e_media(app):
    """GET /api/claude/pedidos-semana devolve a grade dos dois motores."""
    from datetime import datetime
    from datetime import time as _t

    from app.models import EstoqueLoja, MovEstoqueLoja

    app.config['CLAUDE_API_TOKEN'] = TOKEN
    r = _seed('Croissant')
    loja = Loja.query.filter_by(nome='Loja A').first()
    el = EstoqueLoja(loja_id=loja.id, receita_id=r.id, quantidade=0)
    db.session.add(el)
    db.session.commit()
    d = hoje() - timedelta(days=7)
    db.session.add(MovEstoqueLoja(
        estoque_loja_id=el.id, tipo='venda_seru', quantidade=10,
        data=datetime.combine(d, _t(12, 0)), referencia='t'))
    db.session.commit()

    client = app.test_client()
    for modo in ('venda', 'media'):
        resp = client.get(f'/api/claude/pedidos-semana?modo={modo}&inicio=0',
                          headers={'Authorization': f'Bearer {TOKEN}'})
        assert resp.status_code == 200
        body = resp.get_json()
        assert body['ok'] is True
        assert body['modo'] == modo
        assert isinstance(body['lojas'], list)


def test_pedidos_semana_exige_token(app):
    app.config['CLAUDE_API_TOKEN'] = TOKEN
    resp = app.test_client().get('/api/claude/pedidos-semana')
    assert resp.status_code == 401


def test_receita_ficha_completa(app):
    """GET /api/claude/receita?nome= devolve cadastro + ingredientes +
    mapeamentos + estoques da receita (match único)."""
    from app.models import EstoqueLoja, EstoqueProducao, VendaMapa

    app.config['CLAUDE_API_TOKEN'] = TOKEN
    r = Receita(nome='Geleia de Teste', categoria='Acompanhamentos',
                rendimento_qtd=10, rendimento_unidade='potes',
                peso_base=400.0, peso_unitario=40.0)
    loja = Loja(nome='Loja B', ativa=True)
    db.session.add_all([r, loja])
    db.session.commit()
    db.session.add_all([
        EstoqueProducao(receita_id=r.id, quantidade=36),
        EstoqueLoja(loja_id=loja.id, receita_id=r.id, quantidade=5),
        VendaMapa(canal='seru', nome_externo='GELEIA MORANGO 40G',
                  receita_id=r.id, fator_quantidade=1.0),
    ])
    db.session.commit()

    resp = app.test_client().get(
        '/api/claude/receita?nome=geleia de teste',
        headers={'Authorization': f'Bearer {TOKEN}'})
    assert resp.status_code == 200
    d = resp.get_json()
    assert d['ok'] is True
    rec = d['receita']
    assert rec['nome'] == 'Geleia de Teste'
    assert rec['rendimento_qtd'] == 10
    assert rec['rendimento_unidade'] == 'potes'
    assert rec['peso_unitario'] == 40.0
    assert rec['estoque_industria'] == [{'quantidade': 36,
                                         'nome_pendente': None}]
    assert rec['estoque_lojas'][0]['loja'] == 'Loja B'
    assert rec['estoque_lojas'][0]['quantidade'] == 5
    assert rec['mapeamentos_venda'][0]['nome_externo'] == 'GELEIA MORANGO 40G'
    assert rec['mapeamentos_venda'][0]['fator_quantidade'] == 1.0


def test_receita_multiplos_matches_devolve_candidatos(app):
    app.config['CLAUDE_API_TOKEN'] = TOKEN
    db.session.add_all([
        Receita(nome='Geleia de Morango', rendimento_qtd=1,
                rendimento_unidade='un', peso_base=100.0),
        Receita(nome='Geleia de Maça', rendimento_qtd=1,
                rendimento_unidade='un', peso_base=100.0),
    ])
    db.session.commit()
    resp = app.test_client().get(
        '/api/claude/receita?nome=geleia',
        headers={'Authorization': f'Bearer {TOKEN}'})
    assert resp.status_code == 200
    d = resp.get_json()
    assert d['multiplos'] is True
    assert {c['nome'] for c in d['candidatos']} == {'Geleia de Morango',
                                                    'Geleia de Maça'}


def test_receita_nao_encontrada_404_e_sem_param_400(app):
    app.config['CLAUDE_API_TOKEN'] = TOKEN
    client = app.test_client()
    h = {'Authorization': f'Bearer {TOKEN}'}
    assert client.get('/api/claude/receita?nome=inexistente-xyz',
                      headers=h).status_code == 404
    assert client.get('/api/claude/receita', headers=h).status_code == 400
    # E token continua obrigatório.
    assert client.get('/api/claude/receita?nome=x').status_code == 401


# ── /loja-vendas-debug: a venda está baixando o estoque? (06/07/2026) ───────

def _seed_vendas_debug():
    """Loja mapeada+confirmada no Seru, snapshot de venda de 30 itens ontem
    (20 de produto mapeado + 10 de pendente) e baixa de 20 no estoque."""
    from datetime import datetime, time

    from app.models import EstoqueLoja, MovEstoqueLoja, SeruLojaMap, VendaMapa, VendaSeruDiaria
    from app.utils import agora
    loja = Loja(nome='Ribeiro do Vale', ativa=True)
    r = Receita(nome='Croissant RV', rendimento_qtd=1,
                rendimento_unidade='un', peso_base=100.0)
    db.session.add_all([loja, r])
    db.session.commit()
    db.session.add(SeruLojaMap(seru_company_name='OPAO RIBEIRO',
                               loja_id=loja.id, confirmado_em=agora()))
    db.session.add(VendaMapa(canal='seru', nome_externo='CROISSANT',
                             receita_id=r.id))
    db.session.add(VendaMapa(canal='seru', nome_externo='CAFE ESPECIAL'))
    ontem = hoje() - timedelta(days=1)
    db.session.add_all([
        VendaSeruDiaria(data=ontem, loja_seru='OPAO RIBEIRO', loja_id=loja.id,
                        seru_nome='CROISSANT', qtd=20, faturamento=200),
        VendaSeruDiaria(data=ontem, loja_seru='OPAO RIBEIRO', loja_id=loja.id,
                        seru_nome='CAFE ESPECIAL', qtd=10, faturamento=80),
    ])
    el = EstoqueLoja(loja_id=loja.id, receita_id=r.id, quantidade=50)
    db.session.add(el)
    db.session.flush()
    db.session.add(MovEstoqueLoja(
        estoque_loja_id=el.id, tipo='venda_seru', quantidade=20,
        data=datetime.combine(ontem, time(15, 0)), referencia='teste'))
    db.session.commit()
    return loja, ontem


def test_loja_vendas_debug_cruza_reportado_com_baixado(app, monkeypatch):
    from app.services import vendas_diarias
    monkeypatch.setattr(vendas_diarias, 'garantir_capturado',
                        lambda *a, **k: None)   # sem API no teste
    app.config['CLAUDE_API_TOKEN'] = TOKEN
    loja, ontem = _seed_vendas_debug()
    resp = app.test_client().get(
        '/api/claude/loja-vendas-debug?loja=ribeiro&dias=3',
        headers={'Authorization': f'Bearer {TOKEN}'})
    assert resp.status_code == 200
    d = resp.get_json()
    assert d['ok'] is True
    assert d['loja']['nome'] == 'Ribeiro do Vale'      # fuzzy resolveu
    assert d['loja_confirmada_no_seru'] is True
    dia = next(x for x in d['dias'] if x['data'] == ontem.isoformat())
    assert dia['seru_reportado_itens'] == 30
    assert dia['baixas_por_tipo'].get('venda_seru') == 20
    # O gap é explicado pelo mapa: o CAFE (pendente) vendeu 10 e não baixa.
    cafe = next(p for p in d['produtos'] if p['seru_nome'] == 'CAFE ESPECIAL')
    assert cafe['estado_map'] == 'pendente'
    assert d['itens_vendidos_sem_baixa_por_mapa'] == 10


def test_loja_vendas_debug_loja_desconhecida_404(app):
    app.config['CLAUDE_API_TOKEN'] = TOKEN
    resp = app.test_client().get(
        '/api/claude/loja-vendas-debug?loja=nao-existe-xyz',
        headers={'Authorization': f'Bearer {TOKEN}'})
    assert resp.status_code == 404
    assert 'lojas' in resp.get_json()


def test_seru_companies_agrupa_por_id_e_nome(app, monkeypatch):
    """Sonda ao vivo dos companies do Seru (incidente do renome 07/07/2026):
    agrupa por (id, name) pra revelar renome — mesmo id, nome novo."""
    from app.services import seru
    pedidos = [
        {'company': {'id': 77, 'name': 'O PAO RIBEIRO NOVO'},
         'createdAt': '2026-07-06T10:00:00Z'},
        {'company': {'id': 77, 'name': 'O PAO RIBEIRO NOVO'},
         'createdAt': '2026-07-07T09:00:00Z'},
        {'company': {'id': 55, 'name': 'O PAO PADARIA'},
         'createdAt': '2026-07-07T08:00:00Z'},
    ]
    monkeypatch.setattr(seru, 'listar_pedidos_completo',
                        lambda *a, **k: pedidos)
    app.config['CLAUDE_API_TOKEN'] = TOKEN
    resp = app.test_client().get(
        '/api/claude/seru-companies?dias=2',
        headers={'Authorization': f'Bearer {TOKEN}'})
    assert resp.status_code == 200
    d = resp.get_json()
    assert d['ok'] is True and d['total_pedidos'] == 3
    top = d['companies'][0]
    assert top == {'id': 77, 'name': 'O PAO RIBEIRO NOVO', 'n_pedidos': 2,
                   'documents': [],
                   'pedidos_por_dia': {'2026-07-06': 1, '2026-07-07': 1}}
    assert d['exemplo_company'] == {'id': 77, 'name': 'O PAO RIBEIRO NOVO'}


def test_seru_companies_api_fora_502(app, monkeypatch):
    from app.services import seru

    def _boom(*a, **k):
        raise RuntimeError('sem rede')
    monkeypatch.setattr(seru, 'listar_pedidos_completo', _boom)
    app.config['CLAUDE_API_TOKEN'] = TOKEN
    resp = app.test_client().get(
        '/api/claude/seru-companies',
        headers={'Authorization': f'Bearer {TOKEN}'})
    assert resp.status_code == 502
    assert 'sem rede' in resp.get_json()['erro']


def test_pedidos_dia_lista_todos_os_status(app):
    """A sonda /pedidos-dia mostra TODOS os status da data (inclusive os que
    somem das abas de /pedidos — caso real 08/07: pedido 'entregue' com
    data_entrega futura)."""
    from datetime import timedelta

    app.config['CLAUDE_API_TOKEN'] = TOKEN
    amanha = hoje() + timedelta(days=1)
    loja_a = Loja(nome='Loja Anesio X', ativa=True)
    loja_b = Loja(nome='Loja Nebraska X', ativa=True)
    r = Receita(nome='Cinnamon Roll X', categoria='Doces', rendimento_qtd=1,
                rendimento_unidade='un', peso_base=100.0)
    db.session.add_all([loja_a, loja_b, r])
    db.session.commit()
    p1 = PedidoLoja(loja_id=loja_a.id, status='entregue',
                    data_entrega=amanha, data_pedido=hoje())
    p2 = PedidoLoja(loja_id=loja_b.id, status='confirmado',
                    data_entrega=amanha, data_pedido=hoje())
    p3 = PedidoLoja(loja_id=loja_a.id, status='cancelado',
                    data_entrega=amanha, data_pedido=hoje())
    db.session.add_all([p1, p2, p3])
    db.session.flush()
    db.session.add(PedidoItem(pedido_id=p1.id, receita_id=r.id, quantidade=35))
    db.session.commit()

    client = app.test_client()
    d = client.get(f'/api/claude/pedidos-dia?data={amanha.isoformat()}',
                   headers={'Authorization': f'Bearer {TOKEN}'}).get_json()
    assert d['ok'] is True and d['total'] == 3
    por_status = {p['status'] for p in d['pedidos']}
    assert por_status == {'entregue', 'confirmado', 'cancelado'}
    entregue = next(p for p in d['pedidos'] if p['status'] == 'entregue')
    assert entregue['loja'] == 'Loja Anesio X'
    assert entregue['itens'] == [{'nome': 'Cinnamon Roll X', 'qtd': 35}]

    # filtro por loja (fuzzy) + data invalida
    d2 = client.get(
        f'/api/claude/pedidos-dia?data={amanha.isoformat()}&loja=nebraska x',
        headers={'Authorization': f'Bearer {TOKEN}'}).get_json()
    assert d2['total'] == 1 and d2['pedidos'][0]['status'] == 'confirmado'
    resp = client.get('/api/claude/pedidos-dia?data=xx',
                      headers={'Authorization': f'Bearer {TOKEN}'})
    assert resp.status_code == 400


def test_pedidos_dia_exige_token(app):
    app.config['CLAUDE_API_TOKEN'] = TOKEN
    assert app.test_client().get('/api/claude/pedidos-dia').status_code == 401


def test_frete_debug_roda_etapas_sem_500(app, monkeypatch):
    """A sonda /frete-debug desempacota o retorno das etapas do geocode. Trava
    o 500 de 09/07/2026: `_geocodificar_cep` passou a devolver 4 tuplas (com a
    ref de cidade) e o `_etapa` desempacotava 3 → ValueError → 500 em prod."""
    import requests

    from app.services import frete as frete_svc

    class _Resp:
        status_code = 200

        def __init__(self, payload):
            self._p = payload

        def json(self):
            return self._p

    # BrasilAPI resolve o CEP mas SEM coordenada (caminho da 4-tupla no _etapa);
    # Nominatim devolve o nó certo. Nenhuma chamada real de rede.
    def fake_get(url, **kw):
        if 'brasilapi' in url:
            return _Resp({'street': 'Alameda Porcelana',
                          'neighborhood': 'Cerâmica',
                          'city': 'São Caetano do Sul',
                          'location': {'coordinates': {}}})
        return _Resp([{'lat': '-23.6261', 'lon': '-46.5776',
                       'display_name': 'Alameda Porcelana, São Caetano do Sul',
                       'address': {'city': 'São Caetano do Sul',
                                   'postcode': '08671-035'}}])

    monkeypatch.setattr(requests, 'get', fake_get)
    monkeypatch.setattr(frete_svc.requests, 'get', fake_get)
    app.config['CLAUDE_API_TOKEN'] = TOKEN
    resp = app.test_client().get(
        '/api/claude/frete-debug?q=Alameda Porcelana, São Caetano do Sul, 09531-150',
        headers={'Authorization': f'Bearer {TOKEN}'})
    assert resp.status_code == 200
    d = resp.get_json()
    assert d['ok'] is True
    # a etapa da BrasilAPI (4-tupla) foi montada sem estourar
    assert d['etapas']['brasilapi_cep']['coords'] is None
    # e o oficial cotou (cidade bate, ignora o CEP torto do OSM)
    assert d['oficial']['ok'] is True and d['oficial']['fora_area'] is False


def test_tiny_danfe_debug_exige_token(app):
    app.config['CLAUDE_API_TOKEN'] = TOKEN
    c = app.test_client()
    assert c.get('/api/claude/tiny-danfe-debug?id=909').status_code == 401


def test_tiny_danfe_debug_mostra_estrutura(app):
    """Com token, a sonda devolve os campos de link e (quando o PDF falha)
    os candidatos de PDF da página do Olist. Tiny/HTTP mockados."""
    from unittest.mock import patch
    app.config['CLAUDE_API_TOKEN'] = TOKEN
    app.config['TINY_API_TOKEN'] = 'tok'

    class _R:
        status_code = 200
        headers = {'Content-Type': 'text/html; charset=utf-8'}
        url = 'https://erp.olist.com/doc.view?id=x'
        text = '<iframe src="/nfe/danfe_1.pdf"></iframe>'
        content = b'<html>danfe</html>'
    c = app.test_client()
    # Simula a conversão HTML→PDF FALHANDO pra exercitar o branch de
    # diagnóstico (candidatos de PDF nativo). No caminho feliz o weasyprint
    # converte e pdf_ok=True — coberto em test_tiny_danfe.py.
    with patch('app.services.tiny._get',
               return_value={'status': 'OK',
                             'link_nfe': 'https://erp.olist.com/doc.view?id=x'}), \
         patch('app.services.tiny_nf._html_para_pdf', return_value=None), \
         patch('requests.get', return_value=_R()):
        r = c.get('/api/claude/tiny-danfe-debug?id=909358497',
                  headers={'Authorization': f'Bearer {TOKEN}'})
    j = r.get_json()
    assert j['ok'] is True
    assert j['pdf_ok'] is False
    assert j['campos_link']['link_nfe'].endswith('id=x')
    # A sonda testou os candidatos de PDF nativo (nenhum é PDF neste mock).
    assert isinstance(j['candidatos_pdf_nativo'], list)
    assert j['candidatos_pdf_nativo']
    assert all(t.get('eh_pdf') is False for t in j['candidatos_pdf_nativo'])
    assert 'accept_pdf' in j


def test_deploy_info_exige_token_e_responde(app, monkeypatch):
    """Sonda /deploy: diz qual commit está no ar (procedimento de 2 commits
    de schema — confirma o deploy do ALTER sem depender do dono)."""
    app.config['CLAUDE_API_TOKEN'] = TOKEN
    client = app.test_client()
    assert client.get('/api/claude/deploy').status_code == 401
    monkeypatch.setenv('RAILWAY_GIT_COMMIT_SHA', 'abc123')
    resp = client.get('/api/claude/deploy',
                      headers={'Authorization': f'Bearer {TOKEN}'})
    assert resp.status_code == 200
    d = resp.get_json()
    assert d['ok'] is True and d['commit'] == 'abc123'


def test_custos_devolve_receitas_produtos_e_mps(app):
    """Sonda /custos (13/07/2026, planilha "Custos faltantes"): custo
    calculado de receita, custo de produto (composição/direto) e custo
    unitário de MP com última entrada precificada."""
    from datetime import datetime

    from app.models import (
        MateriaPrima,
        MovimentacaoEstoque,
        Produto,
        ProdutoItem,
        ReceitaIngrediente,
    )

    app.config['CLAUDE_API_TOKEN'] = TOKEN
    client = app.test_client()
    assert client.get('/api/claude/custos').status_code == 401

    mp = MateriaPrima(nome='Nutella Balde 3kg', unidade='un',
                      custo_por_kg=120.0, fornecedor='Atacadao')
    rec = Receita(nome='Croissant Teste', categoria='Paes',
                  rendimento_qtd=10, rendimento_unidade='un',
                  peso_base=1000.0)
    db.session.add_all([mp, rec])
    db.session.flush()
    db.session.add(ReceitaIngrediente(receita_id=rec.id, tipo='mp_un',
                                      ingrediente_nome=mp.nome,
                                      porcentagem=2.0, eh_base=False))
    prod = Produto(nome='Kit Teste', ativo=True, custo_embalagem=2.0)
    db.session.add(prod)
    db.session.flush()
    db.session.add(ProdutoItem(produto_id=prod.id, tipo='mp',
                               materia_prima_id=mp.id,
                               item_nome=mp.nome, quantidade=1))
    db.session.add(MovimentacaoEstoque(
        materia_prima_id=mp.id, tipo='entrada', quantidade=3,
        preco_unitario=118.5, data=datetime(2026, 7, 10, 9, 0)))
    db.session.commit()

    resp = client.get('/api/claude/custos',
                      headers={'Authorization': f'Bearer {TOKEN}'})
    assert resp.status_code == 200
    d = resp.get_json()
    assert d['ok'] is True

    r = next(x for x in d['receitas'] if x['nome'] == 'Croissant Teste')
    assert r['custo_unitario'] > 0

    p = next(x for x in d['produtos'] if x['nome'] == 'Kit Teste')
    assert p['n_itens'] == 1 and p['custo'] is not None
    # 1 un de MP a R$120 + R$2 de embalagem
    assert abs(p['custo'] - 122.0) < 0.01

    m = next(x for x in d['materias_primas'] if x['nome'] == 'Nutella Balde 3kg')
    assert m['custo_unitario'] == 120.0
    assert m['ultima_entrada']['preco_unitario'] == 118.5


def test_site_metricas_funil_e_faturamento(app):
    """Sonda /site-metricas (13/07/2026): funil por criado_em, faturamento
    por pago_em, ticket, clientes e flags de rastreio."""
    from datetime import timedelta as _td

    from app.models import Cliente, PedidoOnline
    from app.utils import agora

    app.config['CLAUDE_API_TOKEN'] = TOKEN
    client = app.test_client()
    assert client.get('/api/claude/site-metricas').status_code == 401

    cli = Cliente(nome='Maria', email='met@x.com')
    db.session.add(cli)
    db.session.flush()
    agora_dt = agora()
    pago = PedidoOnline(cliente_id=cli.id, nome_cliente='Maria',
                        email_cliente='met@x.com', modo_entrega='retirada',
                        status='pago', valor_total=50, subtotal=50,
                        criado_em=agora_dt - _td(days=1),
                        pago_em=agora_dt - _td(days=1))
    aband = PedidoOnline(cliente_id=cli.id, nome_cliente='Maria',
                         email_cliente='met@x.com', modo_entrega='entrega',
                         status='aguardando_pagamento', valor_total=30,
                         subtotal=30, criado_em=agora_dt - _td(days=1))
    db.session.add_all([pago, aband])
    db.session.commit()

    resp = client.get('/api/claude/site-metricas?dias=7',
                      headers={'Authorization': f'Bearer {TOKEN}'})
    assert resp.status_code == 200
    d = resp.get_json()
    assert d['ok'] is True
    assert d['funil']['criados'] == 2
    assert d['funil']['pagos'] == 1
    assert d['funil']['abandonados'] == 1
    assert d['funil']['conversao_pct'] == 50.0
    assert d['faturamento']['total'] == 50.0
    assert d['faturamento']['n_pedidos'] == 1
    assert d['ticket_medio'] == 50.0
    assert d['clientes']['total'] == 1
    assert d['modos_entrega'] == {'retirada': 1}
    assert 'ga4_configurado' in d['rastreio']


def test_auditoria_baixa_pedidos_classifica(app):
    """Sonda /auditoria-baixa-pedidos (14/07/2026): pedido enviado COM
    movimento = ok; pedido enviado SEM movimento = sem_movimento; falta
    registrada aparece no agregado por item."""
    from app.models import EstoqueProducao, MovEstoqueProducao

    app.config['CLAUDE_API_TOKEN'] = TOKEN
    client = app.test_client()
    assert client.get('/api/claude/auditoria-baixa-pedidos').status_code == 401

    r = _seed('Croissant Aud')          # pedido 40 un, pendente
    loja = Loja.query.filter_by(nome='Loja A').first()
    p1 = PedidoLoja.query.first()
    p1.status = 'em_transporte'
    ep = EstoqueProducao(receita_id=r.id, quantidade=0)
    db.session.add(ep)
    db.session.flush()
    # Baixou 30 e registrou 10 de falta → com_falta (30+10 == 40).
    db.session.add_all([
        MovEstoqueProducao(estoque_producao_id=ep.id, tipo='saida_pedido',
                           quantidade=30,
                           referencia=f'Pedido #{p1.id} → Loja A'),
        MovEstoqueProducao(estoque_producao_id=ep.id,
                           tipo='saida_pedido_sem_estoque', quantidade=10,
                           referencia=f'Pedido #{p1.id} → Loja A'),
    ])
    # Segundo pedido enviado SEM nenhum movimento (escapou da baixa).
    p2 = PedidoLoja(loja_id=loja.id, status='entregue',
                    data_entrega=hoje(), data_pedido=hoje())
    db.session.add(p2)
    db.session.flush()
    db.session.add(PedidoItem(pedido_id=p2.id, receita_id=r.id,
                              quantidade=5))
    db.session.commit()

    resp = client.get('/api/claude/auditoria-baixa-pedidos?dias=7',
                      headers={'Authorization': f'Bearer {TOKEN}'})
    assert resp.status_code == 200
    d = resp.get_json()
    assert d['ok'] is True
    assert d['resumo']['pedidos_analisados'] == 2
    assert d['resumo']['com_falta'] == 1
    assert d['resumo']['sem_movimento'] == 1
    probl = {x['pedido_id']: x for x in d['pedidos_problema']}
    assert probl[p1.id]['classificacao'] == 'com_falta'
    assert probl[p1.id]['baixado'] == 30
    assert probl[p2.id]['classificacao'] == 'sem_movimento'
    assert d['faltas_por_item'] == [{'item': 'Croissant Aud', 'faltou': 10}]


def test_pedidos_site_lista_cancelados(app):
    """Sonda /pedidos-site (15/07/2026): linha do tempo de status/cobranças
    pra investigar cancelamento no cliente errado."""
    from decimal import Decimal

    from app.models import PedidoOnline
    from app.utils import agora

    app.config['CLAUDE_API_TOKEN'] = TOKEN
    client = app.test_client()
    assert client.get('/api/claude/pedidos-site').status_code == 401

    p = PedidoOnline(codigo='CANC01', status='cancelado',
                     nome_cliente='Maria', email_cliente='c@x.com',
                     modo_entrega='retirada', valor_total=Decimal('30'),
                     subtotal=Decimal('30'), criado_em=agora(),
                     cancelado_em=agora(),
                     motivo_cancelamento='cancelado_admin')
    db.session.add(p)
    db.session.commit()

    resp = client.get('/api/claude/pedidos-site?dias=2&status=cancelado',
                      headers={'Authorization': f'Bearer {TOKEN}'})
    assert resp.status_code == 200
    d = resp.get_json()
    assert d['ok'] is True
    ped = next(x for x in d['pedidos'] if x['codigo'] == 'CANC01')
    assert ped['motivo_cancelamento'] == 'cancelado_admin'
    assert ped['cancelado_em'] is not None


# ── /projetos: o quadro da tela /projetos legível pro assistente ─────────

def _seed_projeto(nome='Sistema v2'):
    from app.models import Projeto, ProjetoArea, TarefaProjeto
    area = ProjetoArea(nome='Empresa', tipo='empresa')
    db.session.add(area)
    db.session.flush()
    p = Projeto(area_id=area.id, nome=nome, status='fazendo',
                prioridade='alta', observacao='Plano da versão 2')
    db.session.add(p)
    db.session.flush()
    db.session.add_all([
        TarefaProjeto(projeto_id=p.id, nome='Nova vitrine', status='a_fazer',
                      ordem=1),
        TarefaProjeto(projeto_id=p.id, nome='Migrar app', status='feito',
                      ordem=2),
    ])
    db.session.commit()
    return p


def test_projetos_busca_por_nome_devolve_completo(app):
    app.config['CLAUDE_API_TOKEN'] = TOKEN
    _seed_projeto()
    c = app.test_client()
    d = c.get('/api/claude/projetos?nome=v2',
              headers={'Authorization': f'Bearer {TOKEN}'}).get_json()
    assert d['ok'] is True
    pj = d['projeto']
    assert pj['nome'] == 'Sistema v2'
    assert pj['area'] == 'Empresa'
    assert pj['observacao'] == 'Plano da versão 2'
    assert [t['nome'] for t in pj['tarefas']] == ['Nova vitrine', 'Migrar app']
    assert pj['tarefas_abertas'] == 1


def test_projetos_sem_nome_lista_resumo(app):
    app.config['CLAUDE_API_TOKEN'] = TOKEN
    _seed_projeto()
    c = app.test_client()
    d = c.get('/api/claude/projetos',
              headers={'Authorization': f'Bearer {TOKEN}'}).get_json()
    assert d['ok'] is True
    assert d['projetos'][0]['nome'] == 'Sistema v2'
    assert d['projetos'][0]['tarefas_total'] == 2


def test_projetos_varios_matches_devolve_candidatos(app):
    app.config['CLAUDE_API_TOKEN'] = TOKEN
    _seed_projeto('Sistema v2')
    from app.models import Projeto, ProjetoArea
    area = ProjetoArea.query.first()
    db.session.add(Projeto(area_id=area.id, nome='Site v2'))
    db.session.commit()
    c = app.test_client()
    d = c.get('/api/claude/projetos?nome=v2',
              headers={'Authorization': f'Bearer {TOKEN}'}).get_json()
    assert d['ok'] is True
    assert {x['nome'] for x in d['candidatos']} == {'Sistema v2', 'Site v2'}


def test_projetos_nome_sem_match_404(app):
    app.config['CLAUDE_API_TOKEN'] = TOKEN
    c = app.test_client()
    r = c.get('/api/claude/projetos?nome=inexistente',
              headers={'Authorization': f'Bearer {TOKEN}'})
    assert r.status_code == 404


def test_vendas_snapshot_por_dia_e_filtro(app):
    """Sonda do card 'Por loja (PDV)' (18/07/2026): expõe o
    faturamento_pedidos POR DIA do snapshot, com filtro por company —
    audita de fora se um número da tela é soma de período."""
    from datetime import timedelta as _td

    from app.models import VendaSeruDiaLoja
    from app.utils import hoje as _hoje
    app.config['CLAUDE_API_TOKEN'] = TOKEN
    h = _hoje()
    db.session.add_all([
        VendaSeruDiaLoja(data=h, loja_seru='NEBRASKA', n_pedidos=88,
                         faturamento=3000, faturamento_pedidos=3327.07),
        VendaSeruDiaLoja(data=h - _td(days=1), loja_seru='NEBRASKA',
                         n_pedidos=90, faturamento=3100,
                         faturamento_pedidos=3500.00),
        VendaSeruDiaLoja(data=h, loja_seru='PADARIA', n_pedidos=160,
                         faturamento=3000, faturamento_pedidos=3100.00),
    ])
    db.session.commit()
    resp = app.test_client().get(
        '/api/claude/vendas-snapshot?dias=3&loja=nebraska',
        headers={'Authorization': f'Bearer {TOKEN}'})
    assert resp.status_code == 200
    d = resp.get_json()
    assert d['ok'] is True
    assert len(d['linhas']) == 2                       # PADARIA filtrada fora
    assert {ln['faturamento_pedidos'] for ln in d['linhas']} == {3327.07, 3500.0}
    assert d['soma_por_loja_na_janela'] == {'NEBRASKA': 6827.07}


def test_vendas_snapshot_exige_token(app):
    app.config['CLAUDE_API_TOKEN'] = TOKEN
    assert app.test_client().get(
        '/api/claude/vendas-snapshot').status_code == 401


# ── /api/claude/funcionarios (lote de assinatura do RI, 05/08/2026) ──

def test_funcionarios_lista_ativos_com_canais_da_ficha(app):
    from app.models import Funcionario, Loja
    app.config['CLAUDE_API_TOKEN'] = TOKEN
    loja = Loja(nome='Ribeiro do Vale', ativa=True)
    a = Funcionario(nome='Ana Silva', cpf='111.111.111-11',
                    funcao='Atendente', email='ana@x.com',
                    telefone='11999998888', ativo=True)
    b = Funcionario(nome='Bruno Souza', cpf='222.222.222-22',
                    funcao='Padeiro', ativo=True)          # sem email/telefone
    c = Funcionario(nome='Carla Lima', cpf='333.333.333-33',
                    ativo=False)                            # desligada
    a.lojas.append(loja)
    db.session.add_all([loja, a, b, c])
    db.session.commit()
    resp = app.test_client().get(
        '/api/claude/funcionarios',
        headers={'Authorization': f'Bearer {TOKEN}'})
    assert resp.status_code == 200
    d = resp.get_json()
    assert d['ok'] is True and d['total'] == 2              # desligada fora
    assert d['sem_email'] == 1 and d['sem_telefone'] == 1
    ana = next(x for x in d['funcionarios'] if x['nome'] == 'Ana Silva')
    assert ana['email'] == 'ana@x.com'
    assert ana['telefone'] == '11999998888'
    assert ana['lojas'] == ['Ribeiro do Vale']


def test_funcionarios_todos_inclui_desligados(app):
    from app.models import Funcionario
    app.config['CLAUDE_API_TOKEN'] = TOKEN
    db.session.add(Funcionario(nome='Carla Lima', cpf='333.333.333-33',
                               ativo=False))
    db.session.commit()
    resp = app.test_client().get(
        '/api/claude/funcionarios?todos=1',
        headers={'Authorization': f'Bearer {TOKEN}'})
    nomes = [x['nome'] for x in resp.get_json()['funcionarios']]
    assert 'Carla Lima' in nomes


def test_funcionarios_exige_token(app):
    app.config['CLAUDE_API_TOKEN'] = TOKEN
    assert app.test_client().get(
        '/api/claude/funcionarios').status_code == 401


# ---------------------------------------------------------------------------
# /api/claude/pedidos-itens — auditoria "granola em potes x gramas" (18/08)
# ---------------------------------------------------------------------------

def _seed_pedidos_granola():
    loja_a = Loja(nome='Loja Anesio', ativa=True)
    loja_b = Loja(nome='Loja Nebraska', ativa=True)
    granola = Receita(nome='Produção - Granola Artesanal 1000g',
                      categoria='Produção', rendimento_qtd=15300,
                      rendimento_unidade='', peso_base=4000.0)
    outra = Receita(nome='Sourdough Tradicional', categoria='Paes',
                    rendimento_qtd=1, rendimento_unidade='un',
                    peso_base=1000.0)
    db.session.add_all([loja_a, loja_b, granola, outra])
    db.session.commit()
    p1 = PedidoLoja(loja_id=loja_a.id, status='entregue',
                    data_entrega=hoje() - timedelta(days=3),
                    data_pedido=hoje() - timedelta(days=4))
    p2 = PedidoLoja(loja_id=loja_b.id, status='pendente',
                    data_entrega=hoje() + timedelta(days=1),
                    data_pedido=hoje())
    velho = PedidoLoja(loja_id=loja_a.id, status='entregue',
                       data_entrega=hoje() - timedelta(days=200),
                       data_pedido=hoje() - timedelta(days=201))
    db.session.add_all([p1, p2, velho])
    db.session.flush()
    db.session.add_all([
        # lancado em POTES (suspeito) na Anesio
        PedidoItem(pedido_id=p1.id, receita_id=granola.id, quantidade=5),
        # lancado em GRAMAS (correto) na Nebraska
        PedidoItem(pedido_id=p2.id, receita_id=granola.id, quantidade=5000),
        # fora da janela
        PedidoItem(pedido_id=velho.id, receita_id=granola.id, quantidade=3),
        # outro item no mesmo pedido — nao pode aparecer
        PedidoItem(pedido_id=p1.id, receita_id=outra.id, quantidade=40),
    ])
    db.session.commit()
    return loja_a, loja_b, p1, p2


def test_pedidos_itens_exige_token(app):
    app.config['CLAUDE_API_TOKEN'] = TOKEN
    assert app.test_client().get(
        '/api/claude/pedidos-itens?item=granola').status_code == 401


def test_pedidos_itens_sem_item_400(app):
    app.config['CLAUDE_API_TOKEN'] = TOKEN
    h = {'Authorization': f'Bearer {TOKEN}'}
    c = app.test_client()
    assert c.get('/api/claude/pedidos-itens', headers=h).status_code == 400
    assert c.get('/api/claude/pedidos-itens?item=ab',
                 headers=h).status_code == 400


def test_pedidos_itens_lista_pedidos_do_item(app):
    app.config['CLAUDE_API_TOKEN'] = TOKEN
    loja_a, loja_b, p1, p2 = _seed_pedidos_granola()
    resp = app.test_client().get(
        '/api/claude/pedidos-itens?item=Granola%20Artesanal%201000&dias=90',
        headers={'Authorization': f'Bearer {TOKEN}'})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data['ok'] is True and data['n'] == 2
    por_pedido = {x['pedido_id']: x for x in data['itens']}
    assert set(por_pedido) == {p1.id, p2.id}  # o velho fica fora da janela
    assert por_pedido[p1.id]['quantidade'] == 5
    assert por_pedido[p1.id]['loja'] == 'Loja Anesio'
    assert por_pedido[p2.id]['quantidade'] == 5000
    # so o item pedido — o Sourdough do mesmo pedido nao vaza
    assert all('Granola' in x['item'] for x in data['itens'])


def test_pedidos_itens_filtro_por_loja(app):
    app.config['CLAUDE_API_TOKEN'] = TOKEN
    loja_a, loja_b, p1, p2 = _seed_pedidos_granola()
    h = {'Authorization': f'Bearer {TOKEN}'}
    c = app.test_client()
    data = c.get('/api/claude/pedidos-itens?item=granola&dias=90'
                 f'&loja={loja_b.id}', headers=h).get_json()
    assert [x['pedido_id'] for x in data['itens']] == [p2.id]
    assert c.get('/api/claude/pedidos-itens?item=granola&loja=inexistente',
                 headers=h).status_code == 404


def test_pedidos_itens_trecho_sem_match(app):
    app.config['CLAUDE_API_TOKEN'] = TOKEN
    resp = app.test_client().get(
        '/api/claude/pedidos-itens?item=naoexiste',
        headers={'Authorization': f'Bearer {TOKEN}'})
    data = resp.get_json()
    assert data['ok'] is True and data['itens'] == []
    assert 'aviso' in data
