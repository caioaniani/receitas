"""Imprimir entregas: tela /entregas/ e /entregas/painel agora abrem pra
todo usuario logado (decisao 11/06/2026); /entregas/imprimir gera 1 folha
A4 por pedido por via; via do entregador omite valores e cartinha."""
from unittest.mock import patch


def _login(c, login='admin', senha='123'):
    c.post('/auth/login', data={'login': login, 'senha': senha})


def _user(app, papel='funcionario', login='func1', loja_id=None):
    from app.extensions import db
    from app.models import Usuario
    with app.app_context():
        u = Usuario(nome='Func', login=login, papel=papel, loja_id=loja_id)
        u.set_senha('senha123')
        db.session.add(u)
        db.session.commit()
        return u.id


def _pedidos_fake():
    return [
        {'code': 'VND-1', 'destinatario': 'Ana', 'endereco': 'Rua A, 1',
         'telefone': '11 99999-1111', 'periodo': '10h-12h', 'expresso': False,
         'cartinha_vnda': 'Feliz aniversario!', 'observacao': 'portaria 1',
         'valor_total': 360.0,
         'itens': [{'nome': 'Cesta Bonjour', 'quantidade': 1,
                    'valor_unitario': 215, 'valor_total': 215},
                   {'nome': 'Croissant Tradicional', 'quantidade': 5,
                    'valor_unitario': 29, 'valor_total': 145}]},
        {'code': 'VND-2', 'destinatario': 'Bruno', 'endereco': 'Rua B, 2',
         'telefone': '', 'expresso': True, 'observacao': '',
         'valor_total': 50.0,
         'itens': [{'nome': 'Sourdough Tradicional', 'quantidade': 1,
                    'valor_total': 50}]},
    ]


def _mock_carregamento():
    """Patcha as 3 chamadas que /imprimir faz pro VNDA/cartinhas/drivers."""
    return [
        patch('app.blueprints.entregas.routes.vnda.buscar_pedidos_do_dia',
              return_value={'pedidos': _pedidos_fake()}),
        patch('app.blueprints.entregas.routes._injetar_pedidos_locais',
              side_effect=lambda target, res: res),
        patch('app.blueprints.entregas.routes._carregar_overrides_full',
              return_value={}),
    ]


def test_botao_imprimir_existe_no_topo_da_tela(app, admin_user):
    """Independente da aba ativa (Operacao/legado), o botao 'imprimir
    selecionados' fica no topo e consome os checkboxes existentes
    (op-check da Operacao OU chk-imprimir do legado)."""
    c = app.test_client()
    _login(c)
    r = c.get('/entregas/')
    body = r.data
    assert b'btn-imprimir-sel' in body
    assert b'imprimir selecionados' in body
    assert b'modalImprimirVias' in body
    assert b'sel-imprimir-info' in body


def test_js_le_op_check_e_chk_imprimir(app):
    """codesSelecionados() considera os dois seletores — nao quebra quando
    o usuario esta na aba Operacao (que ja desenha op-check)."""
    import pathlib
    js = pathlib.Path('app/static/js/entregas.js').read_text()
    assert '.op-check:checked, .chk-imprimir:checked' in js


def test_funcionario_sem_loja_acessa_entregas_e_painel(app):
    """Decisao 11/06/2026: /entregas/ e /entregas/painel sao pra TODOS os
    usuarios logados — antes funcionario sem loja_id levava 403."""
    uid = _user(app, papel='funcionario', loja_id=None)
    c = app.test_client()
    _login(c, login='func1', senha='senha123')
    with c.session_transaction() as s:
        s['_user_id'] = str(uid)
        s['_fresh'] = True
    assert c.get('/entregas/').status_code == 200
    assert c.get('/entregas/painel').status_code == 200


def test_imprimir_renderiza_via_cliente_com_cartinha_e_valor(app, admin_user):
    c = app.test_client()
    _login(c)
    mocks = _mock_carregamento()
    [m.start() for m in mocks]
    try:
        r = c.get('/entregas/imprimir?codes=VND-1&vias=cliente'
                  '&data=2026-06-11')
    finally:
        [m.stop() for m in mocks]
    assert r.status_code == 200
    body = r.data
    assert b'via do cliente' in body
    assert b'via do entregador' not in body
    assert 'Ana'.encode() in body
    assert 'Feliz aniversario!'.encode() in body          # cartinha
    assert 'R$ 360,00'.encode() in body                    # total
    assert 'R$ 215,00'.encode() in body                    # valor por item
    assert b'page-break-after: always' in body


def test_imprimir_via_motorista_omite_valores_e_cartinha(app, admin_user):
    c = app.test_client()
    _login(c)
    mocks = _mock_carregamento()
    [m.start() for m in mocks]
    try:
        r = c.get('/entregas/imprimir?codes=VND-1&vias=motorista'
                  '&data=2026-06-11')
    finally:
        [m.stop() for m in mocks]
    assert r.status_code == 200
    body = r.data
    assert b'via do entregador' in body
    assert b'via do cliente' not in body
    # entregador precisa: destinatario, endereco, telefone, itens, janela
    assert 'Ana'.encode() in body
    assert b'Rua A, 1' in body
    assert b'99999-1111' in body
    assert 'Cesta Bonjour'.encode() in body
    # NAO precisa: cartinha, valor item, total, driver atribuido
    assert 'Feliz aniversario!'.encode() not in body
    assert b'R$ 215' not in body
    assert b'R$ 360' not in body
    # tem campo de conferencia (assinatura)
    assert b'assinatura' in body


def test_imprimir_duas_vias_gera_duas_folhas_por_pedido(app, admin_user):
    """codes=VND-1,VND-2 + vias=cliente,motorista → 4 page-breaks (1
    sobra como last-child sem break, mas ainda 4 folhas no DOM)."""
    c = app.test_client()
    _login(c)
    mocks = _mock_carregamento()
    [m.start() for m in mocks]
    try:
        r = c.get('/entregas/imprimir?codes=VND-1,VND-2'
                  '&vias=cliente,motorista&data=2026-06-11')
    finally:
        [m.stop() for m in mocks]
    body = r.data.decode()
    assert body.count('class="folha"') == 4   # 2 pedidos × 2 vias
    # ordem: cliente VND-1, motorista VND-1, cliente VND-2, motorista VND-2
    i_a_cli = body.index('VND-1') < body.index('VND-2')
    assert i_a_cli   # pedido 1 antes do 2
    # expresso aparece destacado pro motorista
    assert 'EXPRESSO' in body


def test_imprimir_codes_invalido_devolve_pagina_vazia(app, admin_user):
    c = app.test_client()
    _login(c)
    mocks = _mock_carregamento()
    [m.start() for m in mocks]
    try:
        r = c.get('/entregas/imprimir?codes=NAO-EXISTE&data=2026-06-11')
    finally:
        [m.stop() for m in mocks]
    assert r.status_code == 200
    assert 'Nenhum pedido selecionado'.encode() in r.data


def test_imprimir_aguenta_quantidade_none(app, admin_user):
    """Bug real (11/06/2026): selecionei TODOS e a impressao deu 500. Causa:
    algum item tem quantidade=None — sum(attribute=) do Jinja explode.
    O template agora soma defensivo (None→0)."""
    c = app.test_client()
    _login(c)

    pedido_quebrado = {
        'code': 'VND-Q', 'destinatario': 'Z', 'endereco': 'Rua Q',
        'telefone': '', 'periodo': '', 'expresso': False,
        'valor_total': 100.0,
        'itens': [
            {'nome': 'Item ok', 'quantidade': 2, 'valor_total': 60},
            {'nome': 'Item sem qtd', 'quantidade': None,
             'valor_total': 40},
            {'nome': 'Item sem valor', 'quantidade': 1,
             'valor_total': None, 'valor_unitario': None},
        ],
    }
    mocks = [
        patch('app.blueprints.entregas.routes.vnda.buscar_pedidos_do_dia',
              return_value={'pedidos': [pedido_quebrado]}),
        patch('app.blueprints.entregas.routes._injetar_pedidos_locais',
              side_effect=lambda target, res: res),
        patch('app.blueprints.entregas.routes._carregar_overrides_full',
              return_value={}),
    ]
    [m.start() for m in mocks]
    try:
        r = c.get('/entregas/imprimir?codes=VND-Q&data=2026-06-11')
    finally:
        [m.stop() for m in mocks]
    assert r.status_code == 200
    body = r.data.decode()
    assert 'Item ok' in body
    assert 'Item sem qtd' in body
    # contagem deve somar so o que existe (2 + 0 + 1 = 3) — sem TypeError
    assert '(3 itens)' in body


def test_js_escuta_op_check_secao_pra_selecionar_todos(app):
    """Bug real (11/06/2026): 'selecionar todos da secao' nao habilitava o
    botao. Causa: op-check-secao seta .checked nos filhos por atribuicao
    direta, sem disparar 'change'. Listener agora escuta op-check-secao
    explicitamente + click como recurso final."""
    import pathlib
    js = pathlib.Path('app/static/js/entregas.js').read_text()
    assert "'op-check-secao'" in js
    assert 'op-check-secao' in js and 'atualizarBarraImprimir' in js


def test_imprimir_default_via_cliente(app, admin_user):
    c = app.test_client()
    _login(c)
    mocks = _mock_carregamento()
    [m.start() for m in mocks]
    try:
        r = c.get('/entregas/imprimir?codes=VND-1&data=2026-06-11')
    finally:
        [m.stop() for m in mocks]
    body = r.data
    assert b'via do cliente' in body
    assert b'via do entregador' not in body
