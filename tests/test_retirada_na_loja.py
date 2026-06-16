"""Detecção de pedido de RETIRADA NA LOJA (16/06/2026).

Bug do dono: pedidos de retirada não apareciam em lugar nenhum (nem na tela
`/entregas/`, nem no painel `/entregas/painel`). Causa: `_normalizar_pedido`
no `vnda.py` não tinha detector pra retirada — pedidos chegavam misturados,
sem endereço, e o painel filtrava silenciosamente.

Fix: detector `_is_retirada(order)` que casa pedidos do VNDA (`delivery_type:
'retirar-na-loja'` ou `shipping_label: 'Retire na loja'`, confirmado em
pedido real EF1B2AE877). Endereço sobrescrito pra "Anésio Pinto Rosa, 78 —
Itaim (retirada)" — única loja que faz pickup. Flag `retirada` propaga pra
APIs `/entregas/api/pedidos` e `/entregas/api/painel`.
"""


def test_detecta_pedido_real_do_dono():
    """Pedido EF1B2AE877 (16/06/2026, isabelmusa@gmail.com): caso real
    extraído do JSON do VNDA. Tem que dar match nos 2 campos que vieram."""
    from app.services.vnda import _is_retirada
    ped = {
        'code': 'EF1B2AE877',
        'delivery_type': 'retirar-na-loja',
        'shipping_label': 'Retire na loja',
    }
    assert _is_retirada(ped) is True


def test_detecta_so_pelo_delivery_type():
    """Se VNDA mudar o label mas manter o delivery_type, ainda detectamos."""
    from app.services.vnda import _is_retirada
    assert _is_retirada({'delivery_type': 'retirar-na-loja'}) is True
    assert _is_retirada({'delivery_type': 'RETIRAR-NA-LOJA'}) is True


def test_detecta_so_pelo_shipping_label():
    """Se VNDA mudar o delivery_type mas manter o label, ainda detectamos."""
    from app.services.vnda import _is_retirada
    assert _is_retirada({'shipping_label': 'Retire na loja'}) is True
    assert _is_retirada({'shipping_label': 'Retirada na loja'}) is True
    assert _is_retirada({'shipping_label': 'RETIRADA'}) is True


def test_NAO_detecta_entrega_normal():
    """Pedido normal de entrega não pode disparar — risco de regressão grave
    (esconderia entrega de verdade da equipe)."""
    from app.services.vnda import _is_retirada
    casos = [
        {},
        {'delivery_type': 'standard'},
        {'shipping_label': 'Entrega normal'},
        {'delivery_type': 'expressa', 'shipping_label': 'Entrega em 1 hora'},
        {'shipping_method': 'Lalamove'},  # nem deve olhar esse campo
    ]
    for c in casos:
        assert _is_retirada(c) is False, f'falso positivo em: {c}'


def test_normalizacao_inclui_flag_retirada():
    """Flag `retirada` precisa estar no dict serializado — front depende."""
    from app.services.vnda import _normalizar_pedido
    ped = {
        'code': 'X1', 'delivery_type': 'retirar-na-loja',
        'shipping_label': 'Retire na loja', 'items': [], 'total': 100.0,
    }
    n = _normalizar_pedido(ped)
    assert n['retirada'] is True

    ped_normal = {'code': 'X2', 'delivery_type': 'standard', 'items': []}
    n2 = _normalizar_pedido(ped_normal)
    assert n2['retirada'] is False


def test_normalizacao_sobrescreve_endereco_pra_anesio():
    """Em retirada, endereço do cliente vem vazio. Substituímos pelo
    endereço da loja que faz pickup (Anésio Pinto Rosa) — único lugar que
    aceita retirada de pedido do site, conforme CLAUDE.md."""
    from app.services.vnda import _normalizar_pedido
    ped = {
        'code': 'X1', 'delivery_type': 'retirar-na-loja',
        'shipping_label': 'Retire na loja', 'items': [], 'total': 100.0,
    }
    n = _normalizar_pedido(ped)
    assert 'Anésio Pinto Rosa' in n['endereco']
    assert 'retirada' in n['endereco'].lower()


def test_normalizacao_NAO_mexe_no_endereco_de_entrega_normal():
    """Garantia paranoica: entrega normal mantém endereço do cliente."""
    from app.services.vnda import _normalizar_pedido
    ped = {
        'code': 'Y1', 'delivery_type': 'standard',
        'shipping_address': {
            'street_name': 'Rua Aspicuelta',
            'street_number': '500',
            'city': 'São Paulo',
        },
        'items': [], 'total': 50.0,
    }
    n = _normalizar_pedido(ped)
    assert 'Anésio' not in (n['endereco'] or '')
    assert 'Aspicuelta' in (n['endereco'] or '')


# ── Propagação pra a API do painel ────────────────────────────────────

def test_api_painel_devolve_flag_retirada_no_pedido(app):
    """`/entregas/api/painel` precisa expor `retirada: bool` em cada pedido —
    front (`painel.html`) usa pra decidir a coluna e esconder o Lalamove."""
    from unittest.mock import patch

    from app.extensions import db
    from app.models import Usuario
    u = Usuario(nome='Adm', login='adm', papel='admin')
    u.set_senha('x' * 8)
    db.session.add(u)
    db.session.commit()
    c = app.test_client()
    with c.session_transaction() as s:
        s['_user_id'] = str(u.id)
        s['_fresh'] = True

    ped_retirada = {
        'code': 'REAL-RETIR', 'destinatario': 'Isabel', 'endereco': '',
        'periodo': '', 'expresso': False, 'retirada': True,
        'comprador': 'Isabel', 'telefone': '', 'cartinha_vnda': '',
        'itens': [], 'total': 100.0,
    }
    ped_normal = {
        'code': 'REAL-ENT', 'destinatario': 'João', 'endereco': 'Rua X',
        'periodo': '8h às 9h', 'expresso': False, 'retirada': False,
        'comprador': 'João', 'telefone': '', 'cartinha_vnda': '',
        'itens': [], 'total': 50.0,
    }
    with patch('app.services.vnda.buscar_pedidos_do_dia',
                return_value={'pedidos': [ped_retirada, ped_normal]}):
        r = c.get('/entregas/api/painel').get_json()

    por_code = {p['code']: p for p in r['pedidos']}
    assert por_code['REAL-RETIR']['retirada'] is True
    assert por_code['REAL-ENT']['retirada'] is False
