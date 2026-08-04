"""Acompanhamento de entrega por progresso + foto obrigatória (01/08/2026).

Operação do Dia dos Pais: motoristas contratados, cliente acompanha por
"parada N de M + previsão", e ENTREGUE só com foto de comprovação (decisão
do dono 01/08/2026 — sem comprovação, "entregue" é só uma palavra)."""
from unittest.mock import patch

from app.extensions import db
from app.utils import hoje


def _driver():
    from app.models import Driver
    d = Driver(nome='João Rota', ativo=True, token='tok123', pin='1234')
    db.session.add(d)
    db.session.commit()
    return d


def _rota(d, n=3):
    from app.models import AtribuicaoEntrega, PedidoOnline
    codes = []
    for i in range(n):
        p = PedidoOnline(codigo=f'RT{i}', nome_cliente=f'C{i}',
                         email_cliente=f'c{i}@x.com', status='pago',
                         modo_entrega='agendada', data_entrega=hoje(),
                         janela_entrega='06:00–10:00', valor_total=100)
        db.session.add(p)
        db.session.commit()
        db.session.add(AtribuicaoEntrega(pedido_code=p.codigo,
                                         driver_id=d.id,
                                         data_entrega=hoje(), ordem=i))
        codes.append(p.codigo)
    db.session.commit()
    return codes


def test_antes_da_rota_sair_fica_em_preparo(app):
    from app.services import rastreio_entrega as svc
    d = _driver()
    codes = _rota(d)
    assert svc.status_do_pedido(codes[0]) == {'fase': 'em_preparo'}


def test_iniciar_rota_dispara_email_uma_vez(app):
    """Idempotente: o 2º clique (ou o duplo clique) não reenvia nada."""
    from app.services import rastreio_entrega as svc
    d = _driver()
    _rota(d, n=2)
    with patch('app.services.email.enviar_pedido_a_caminho',
               return_value={'ok': True}) as env:
        ri, n1 = svc.iniciar_rota(d)
        ri2, n2 = svc.iniciar_rota(d)
    assert n1 == 2 and n2 == 0
    assert ri.id == ri2.id
    assert env.call_count == 2


def test_progresso_avanca_quando_o_motorista_entrega(app):
    """Cada "entregue" move a posição e a ETA de TODOS os da rota."""
    from app.models import AtribuicaoEntrega
    from app.services import rastreio_entrega as svc
    from app.utils import agora
    d = _driver()
    codes = _rota(d, n=3)
    with patch('app.services.email.enviar_pedido_a_caminho',
               return_value={'ok': True}):
        svc.iniciar_rota(d)
    s = svc.status_do_pedido(codes[2])
    assert s['fase'] == 'a_caminho'
    assert s['parada'] == 3 and s['faltam'] == 2
    assert s['driver'] == 'João Rota'
    assert s['eta']                       # tem previsão
    a = AtribuicaoEntrega.query.filter_by(pedido_code=codes[0]).first()
    a.status = 'entregue'
    a.entregue_em = agora()
    db.session.commit()
    s = svc.status_do_pedido(codes[2])
    assert s['parada'] == 2 and s['faltam'] == 1


def test_entregue_mostra_hora(app):
    from app.models import AtribuicaoEntrega
    from app.services import rastreio_entrega as svc
    from app.utils import agora
    d = _driver()
    codes = _rota(d, n=1)
    a = AtribuicaoEntrega.query.filter_by(pedido_code=codes[0]).first()
    a.status = 'entregue'
    a.entregue_em = agora()
    db.session.commit()
    s = svc.status_do_pedido(codes[0])
    assert s['fase'] == 'entregue' and s['entregue_em']


def test_json_publico_do_pedido_leva_o_rastreio(app):
    from app.services import rastreio_entrega as svc  # noqa: F401
    d = _driver()
    codes = _rota(d, n=1)
    with patch('app.services.email.enviar_pedido_a_caminho',
               return_value={'ok': True}):
        from app.services.rastreio_entrega import iniciar_rota
        iniciar_rota(d)
    c = app.test_client()
    j = c.get(f'/loja/pedido/{codes[0]}/status').get_json()
    assert j['rastreio']['fase'] == 'a_caminho'


# ── Foto obrigatória no entregue (driver) ────────────────────────────────

def _client_driver(app, d):
    c = app.test_client()
    with c.session_transaction() as s:
        s[f'driver_auth_{d.id}'] = True
    return c


def test_entregue_sem_foto_e_recusado(app):
    from app.models import AtribuicaoEntrega
    d = _driver()
    codes = _rota(d, n=1)
    a = AtribuicaoEntrega.query.filter_by(pedido_code=codes[0]).first()
    c = _client_driver(app, d)
    r = c.post(f'/driver/api/{d.token}/status',
               json={'atribuicao_id': a.id, 'status': 'entregue'})
    assert r.status_code == 422
    assert r.get_json()['precisa_foto'] is True
    db.session.expire_all()
    assert a.status != 'entregue'


def test_entregue_com_foto_passa(app):
    from app.models import AtribuicaoEntrega, EntregaFoto
    d = _driver()
    codes = _rota(d, n=1)
    a = AtribuicaoEntrega.query.filter_by(pedido_code=codes[0]).first()
    db.session.add(EntregaFoto(atribuicao_id=a.id, url='https://x/f.jpg',
                               storage_path='/f.jpg', tamanho_bytes=10))
    db.session.commit()
    c = _client_driver(app, d)
    r = c.post(f'/driver/api/{d.token}/status',
               json={'atribuicao_id': a.id, 'status': 'entregue'})
    assert r.status_code == 200 and r.get_json()['ok'] is True


def test_nao_entregue_nao_exige_foto(app):
    """Problema na entrega não pode ficar preso atrás de foto."""
    from app.models import AtribuicaoEntrega
    d = _driver()
    codes = _rota(d, n=1)
    a = AtribuicaoEntrega.query.filter_by(pedido_code=codes[0]).first()
    c = _client_driver(app, d)
    r = c.post(f'/driver/api/{d.token}/status',
               json={'atribuicao_id': a.id, 'status': 'nao_entregue',
                     'motivo_falha': 'ausente'})
    assert r.status_code == 200


# ── /rotas e /driver enxergam pedido do SITE (fix 03/08/2026) ────────────
#
# A aba Rotas e a página do motorista eram da era VNDA: só montavam o pool
# com VNDA + manuais, e com o VNDA aposentado o `erro` derrubava tudo —
# "em /rotas não consigo ver nada do site" (dono, 03/08/2026).

def _admin_client(app):
    from app.models import Usuario
    u = Usuario(nome='Adm', login='adm_rotas', papel='admin')
    u.set_senha('x' * 8)
    db.session.add(u)
    db.session.commit()
    c = app.test_client()
    with c.session_transaction() as s:
        s['_user_id'] = str(u.id)
        s['_fresh'] = True
    return c


def test_api_rotas_ve_pedido_do_site_sem_vnda(app):
    """VNDA aposentado (sem token) não pode cegar a roteirização."""
    from app.models import PedidoOnline
    d = _driver()
    p = PedidoOnline(codigo='SITE1', nome_cliente='Cliente Site',
                     email_cliente='s@x.com', status='pago',
                     modo_entrega='agendada', data_entrega=hoje(),
                     janela_entrega='06:00–10:00', valor_total=100,
                     endereco_entrega='Rua X, 1 - Brooklin')
    db.session.add(p)
    db.session.commit()
    c = _admin_client(app)
    r = c.get(f'/entregas/api/rotas?data={hoje().isoformat()}')
    assert r.status_code == 200
    j = r.get_json()
    assert 'erro' not in j or not j.get('erro')
    # Sem chave do Google (teste) o fallback por CEP poe endereco sem CEP
    # em `sem_cep` — o que importa e o pedido estar no POOL, em qualquer
    # balde (em prod, com a chave, ele e geocodado e roteirizado).
    codes = {pp['code'] for rota in j.get('rotas', [])
             for pp in rota.get('paradas', [])}
    codes |= {pp['code'] for pp in j.get('sem_atribuir', [])}
    codes |= {pp['code'] for pp in j.get('sem_cep', [])}
    assert 'SITE1' in codes
    assert j.get('total_pedidos') == 1
    assert d is not None


def test_api_rotas_nao_roteiriza_retirada(app):
    """Retirada o cliente busca na loja — motoboy não vai."""
    from app.models import PedidoOnline
    _driver()
    p = PedidoOnline(codigo='RETIR1', nome_cliente='Cliente Ret',
                     email_cliente='r@x.com', status='pago',
                     modo_entrega='retirada', data_entrega=hoje(),
                     janela_entrega='06:00–10:00', valor_total=50)
    db.session.add(p)
    db.session.commit()
    c = _admin_client(app)
    j = c.get(f'/entregas/api/rotas?data={hoje().isoformat()}').get_json()
    codes = {pp['code'] for rota in j.get('rotas', [])
             for pp in rota.get('paradas', [])}
    codes |= {pp['code'] for pp in j.get('sem_atribuir', [])}
    codes |= {pp['code'] for pp in j.get('sem_cep', [])}
    assert 'RETIR1' not in codes
    assert j.get('total_pedidos') == 0


def test_driver_ve_pedido_do_site_sem_vnda(app):
    from app.models import AtribuicaoEntrega, PedidoOnline
    d = _driver()
    p = PedidoOnline(codigo='SITE2', nome_cliente='Cliente Site',
                     email_cliente='s2@x.com', status='pago',
                     modo_entrega='agendada', data_entrega=hoje(),
                     janela_entrega='06:00–10:00', valor_total=100,
                     endereco_entrega='Rua Y, 2')
    db.session.add(p)
    db.session.commit()
    db.session.add(AtribuicaoEntrega(pedido_code='SITE2', driver_id=d.id,
                                     data_entrega=hoje(), ordem=1))
    db.session.commit()
    c = _client_driver(app, d)
    r = c.get(f'/driver/api/{d.token}/pedidos')
    assert r.status_code == 200          # antes: 502 (erro do VNDA)
    j = r.get_json()
    assert [pp['code'] for pp in j['pedidos']] == ['SITE2']


def test_api_atribuidos_lista_pedido_do_site(app):
    """O caso do print do dono (03/08): o MAPA da aba Operação mostrava os
    pedidos do dia 09 (via /api/rotas, corrigido) e a LISTA dizia "nenhum
    pedido" — porque a lista vem de /api/atribuidos, um TERCEIRO endpoint
    da era VNDA que tinha ficado cego."""
    from app.models import PedidoOnline
    p = PedidoOnline(codigo='LISTA1', nome_cliente='Cliente Lista',
                     email_cliente='l@x.com', status='pago',
                     modo_entrega='agendada', data_entrega=hoje(),
                     janela_entrega='06:00–10:00', valor_total=450,
                     endereco_entrega='Rua Z, 3')
    db.session.add(p)
    db.session.commit()
    c = _admin_client(app)
    j = c.get(f'/entregas/api/atribuidos?data={hoje().isoformat()}').get_json()
    assert 'erro' not in j or not j.get('erro')
    codes = [pp['code'] for pp in j.get('sem_driver', [])]
    assert 'LISTA1' in codes


def test_api_produtos_conta_pedido_do_site(app):
    """Aba Produtos = o que separar/produzir no dia. Sem os pedidos do
    site, no Dia dos Pais ela diria "nada a produzir"."""
    from app.models import PedidoOnline, PedidoOnlineItem, Receita
    r = Receita(nome='Cesta Pais Recheada', categoria='Cestas',
                rendimento_qtd=1, rendimento_unidade='un', peso_base=1000)
    db.session.add(r)
    db.session.commit()
    p = PedidoOnline(codigo='PROD1', nome_cliente='C', email_cliente='p@x.com',
                     status='pago', modo_entrega='agendada',
                     data_entrega=hoje(), janela_entrega='06:00–10:00',
                     valor_total=450)
    db.session.add(p)
    db.session.flush()
    db.session.add(PedidoOnlineItem(pedido_id=p.id, kind='receita',
                                    receita_id=r.id, nome=r.nome,
                                    preco_unitario=450, quantidade=2,
                                    subtotal=900))
    db.session.commit()
    c = _admin_client(app)
    j = c.get(f'/entregas/api/produtos?data={hoje().isoformat()}').get_json()
    nomes = {v['nome']: v for v in j.get('vendidos', [])}
    assert any('Cesta Pais' in n for n in nomes), j.get('vendidos')


def test_resetar_atribuicoes_alcanca_pedido_do_site(app):
    from app.models import AtribuicaoEntrega, PedidoOnline
    d = _driver()
    p = PedidoOnline(codigo='RST1', nome_cliente='C', email_cliente='r2@x.com',
                     status='pago', modo_entrega='agendada',
                     data_entrega=hoje(), janela_entrega='06:00–10:00',
                     valor_total=100)
    db.session.add(p)
    db.session.commit()
    db.session.add(AtribuicaoEntrega(pedido_code='RST1', driver_id=d.id,
                                     data_entrega=hoje(), ordem=1))
    db.session.commit()
    c = _admin_client(app)
    r = c.post('/entregas/api/atribuicao/reset',
               json={'data': hoje().isoformat()})
    j = r.get_json()
    assert j['ok'] is True and j['removidas'] >= 1
    assert AtribuicaoEntrega.query.filter_by(pedido_code='RST1').count() == 0


# ── Payload MAGRO da aba Operação (03/08/2026, pedido do dono) ───────────

def _pedido_com_itens(codigo='MAGRO1'):
    from app.models import PedidoOnline, PedidoOnlineItem, Receita
    r = Receita(nome=f'Pao {codigo}', categoria='Paes', rendimento_qtd=1,
                rendimento_unidade='un', peso_base=100)
    db.session.add(r)
    db.session.commit()
    p = PedidoOnline(codigo=codigo, nome_cliente='C', email_cliente='m@x.com',
                     status='pago', modo_entrega='agendada',
                     data_entrega=hoje(), janela_entrega='06:00–10:00',
                     valor_total=100, cartinha='Feliz dia dos pais, pai!')
    db.session.add(p)
    db.session.flush()
    db.session.add(PedidoOnlineItem(pedido_id=p.id, kind='receita',
                                    receita_id=r.id, nome=r.nome,
                                    preco_unitario=50, quantidade=2,
                                    subtotal=100))
    db.session.commit()
    return p


def test_lista_da_operacao_vem_magra(app):
    """Sem itens nem cartinha — o card não os usa e, com 150 pedidos, isso
    é a diferença entre um poll leve e centenas de KB + N+1."""
    _pedido_com_itens('MAGRO1')
    c = _admin_client(app)
    j = c.get(f'/entregas/api/atribuidos?data={hoje().isoformat()}').get_json()
    p = [x for x in j['sem_driver'] if x['code'] == 'MAGRO1'][0]
    assert p['itens'] == []
    assert p['cartinha_vnda'] == ''
    assert 'cartinha' not in p or not p.get('cartinha')
    # O que o card USA continua lá:
    assert p['destinatario'] and p['endereco'] is not None
    assert p['periodo'] == '06:00–10:00'
    assert p['e_presente'] is True       # selo 🎁 continua (tem cartinha)


def test_impressao_por_codes_sai_completa(app):
    """A impressão NÃO pode herdar o payload magro: o papel do motorista
    precisa dos itens e da cartinha. O caminho por codes rebusca cheio."""
    _pedido_com_itens('CHEIO1')
    c = _admin_client(app)
    r = c.get(f'/entregas/imprimir?codes=CHEIO1&vias=cliente,motorista'
              f'&data={hoje().isoformat()}')
    html = r.data.decode()
    assert 'Pao CHEIO1' in html          # item aparece
    # Cartinha sai na via do CLIENTE (a do motorista omite DE PROPÓSITO —
    # design pré-existente do template, linha 3 do imprimir.html).
    assert 'Feliz dia dos pais' in html


def test_painel_segue_completo(app):
    """O painel separa pedido — precisa de itens e cartinha."""
    _pedido_com_itens('PAINEL1')
    c = _admin_client(app)
    j = c.get(f'/entregas/api/painel?data={hoje().isoformat()}').get_json()
    p = [x for x in j['pedidos'] if x['code'] == 'PAINEL1'][0]
    assert len(p['itens']) == 1
    assert 'Feliz dia dos pais' in (p.get('cartinha') or p['cartinha_vnda'])


# ── Botão "Iniciar rota" (driver) + página do cliente (04/08/2026) ───────

def test_api_pedidos_do_driver_expoe_estado_da_rota(app):
    """O front decide entre o botão "Iniciar rota" e o selo "iniciada às
    HH:MM" pelo campo `rota` do /pedidos."""
    d = _driver()
    _rota(d, n=1)
    c = _client_driver(app, d)
    j = c.get(f'/driver/api/{d.token}/pedidos').get_json()
    assert j['rota'] == {'iniciada': False, 'iniciada_em': None}
    with patch('app.services.email.enviar_pedido_a_caminho',
               return_value={'ok': True}):
        r = c.post(f'/driver/api/{d.token}/iniciar-rota'
                   f'?data={hoje().isoformat()}')
    assert r.get_json()['ok'] is True
    j = c.get(f'/driver/api/{d.token}/pedidos').get_json()
    assert j['rota']['iniciada'] is True and j['rota']['iniciada_em']


def test_pagina_do_pedido_mostra_bloco_de_rastreio(app):
    """Pedido pago de ENTREGA ganha o bloco "Acompanhe sua entrega" com o
    estado inicial embutido (o polling de 30s continua no navegador)."""
    d = _driver()
    codes = _rota(d, n=1)
    with patch('app.services.email.enviar_pedido_a_caminho',
               return_value={'ok': True}):
        from app.services.rastreio_entrega import iniciar_rota
        iniciar_rota(d)
    c = app.test_client()
    r = c.get(f'/loja/pedido/{codes[0]}')
    assert r.status_code == 200
    html = r.data.decode()
    assert 'Acompanhe sua entrega' in html
    assert 'id="rastreio-inicial"' in html
    assert '"a_caminho"' in html          # estado inicial do servidor


def test_pagina_de_retirada_nao_mostra_rastreio(app):
    """Retirada o cliente busca na loja — não há entrega pra acompanhar."""
    from app.models import PedidoOnline
    p = PedidoOnline(codigo='RETRAS1', nome_cliente='C',
                     email_cliente='rr@x.com', status='pago',
                     modo_entrega='retirada', data_entrega=hoje(),
                     janela_entrega='06:00–10:00', valor_total=50)
    db.session.add(p)
    db.session.commit()
    c = app.test_client()
    r = c.get('/loja/pedido/RETRAS1')
    assert r.status_code == 200
    assert 'Acompanhe sua entrega' not in r.data.decode()


def test_vnda_pedidos_curto_circuito_por_default(app, monkeypatch):
    """VNDA aposentado: sem VNDA_PEDIDOS=1, devolve vazio NA HORA, sem rede
    e sem 'erro' (era até 25s de spinner por carregamento da Operação)."""
    from app.services import vnda
    monkeypatch.delenv('VNDA_PEDIDOS', raising=False)
    chamou = {'n': 0}
    monkeypatch.setattr(vnda, '_get_paginado', lambda *a, **k: chamou.__setitem__('n', 1),
                        raising=False)
    with app.app_context():
        out = vnda.buscar_pedidos_do_dia(hoje())
    assert out == {'pedidos': []}
    assert chamou['n'] == 0
