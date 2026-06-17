"""Checkout da loja online (Fase 3).

Foco: INTEGRIDADE DE DINHEIRO (servidor manda no preço e no frete, nunca o
cliente), regras de data/corte 17h, e o fluxo de criação do PedidoOnline.
"""
from datetime import datetime
from decimal import Decimal
from unittest.mock import patch

FRETE_OK = {'ok': True, 'valor': 15.0, 'gratis': False, 'fora_area': False,
            'distancia_km': 3.4, 'endereco': 'Rua X, Moema', 'aviso': ''}
FRETE_FORA = {'ok': True, 'fora_area': True, 'distancia_km': 22.0,
              'endereco': 'Longe', 'aviso': ''}
FRETE_ERRO = {'ok': False, 'erro': 'nao_encontrado'}


def _admin_logado(app):
    from app.extensions import db
    from app.models import Usuario
    u = Usuario(nome='Admin', login='adm', papel='admin')
    u.set_senha('x' * 8)
    db.session.add(u)
    db.session.commit()
    c = app.test_client()
    with c.session_transaction() as s:
        s['_user_id'] = str(u.id)
        s['_fresh'] = True
    return c


def _produto_pub(db, nome='Box Mimo', preco=20.0):
    from app.models import Produto
    p = Produto(nome=nome, categoria='Cestas', preco_site=preco,
                imagem_dropbox_url='https://x/p.jpg', ativo=True)
    db.session.add(p)
    db.session.commit()
    return p


def _loja(db, nome='Brooklin'):
    from app.models import Loja
    loja = Loja(nome=nome, endereco='Ribeiro do Vale, 455', ativa=True)
    db.session.add(loja)
    db.session.commit()
    return loja


# ── Datas / corte 17h ────────────────────────────────────────────────

def test_datas_agendada_inclui_hoje_se_ha_janela(app):
    """Às 10h ainda há janelas hoje (12:00+ com lead 2h) — hoje entra."""
    from app.services import loja_checkout
    with app.app_context():
        base = datetime(2026, 6, 17, 10, 0)
        datas = loja_checkout.datas_disponiveis('agendada', base=base)
        assert datas[0].isoformat() == '2026-06-17'  # hoje
        assert datas[1].isoformat() == '2026-06-18'  # amanhã (contíguo)


def test_datas_agendada_pula_hoje_quando_tarde(app):
    """Tarde demais (18:30): nenhuma janela cabe hoje -> começa amanhã."""
    from app.services import loja_checkout
    with app.app_context():
        base = datetime(2026, 6, 17, 18, 30)
        datas = loja_checkout.datas_disponiveis('agendada', base=base)
        assert datas[0].isoformat() == '2026-06-18'  # amanhã


def test_express_so_hoje_e_dentro_do_horario(app):
    from app.services import loja_checkout
    with app.app_context():
        dentro = datetime(2026, 6, 17, 11, 0)
        fora = datetime(2026, 6, 17, 22, 0)
        assert loja_checkout.datas_disponiveis('express', base=dentro) == [dentro.date()]
        assert loja_checkout.datas_disponiveis('express', base=fora) == []
        assert loja_checkout.express_disponivel(dentro) is True
        assert loja_checkout.express_disponivel(fora) is False


# ── montar_itens: preço vem do catálogo, não do cliente ──────────────

def test_montar_itens_usa_preco_do_catalogo_ignora_cliente(app):
    from app.extensions import db
    from app.services import loja_checkout
    with app.app_context():
        p = _produto_pub(db, preco=20.0)
        # Cliente tenta forjar preço 0.01 — tem que ser IGNORADO.
        raw = [{'kind': 'produto', 'id': p.id, 'qtd': 2, 'preco': 0.01}]
        itens, avisos = loja_checkout.montar_itens(raw)
        assert len(itens) == 1
        assert itens[0]['preco'] == Decimal('20.00')  # do catálogo
        assert itens[0]['subtotal'] == Decimal('40.00')
        assert itens[0]['produto_id'] == p.id


def test_montar_itens_descarta_item_inexistente(app):
    from app.services import loja_checkout
    with app.app_context():
        raw = [{'kind': 'produto', 'id': 999999, 'qtd': 1}]
        itens, avisos = loja_checkout.montar_itens(raw)
        assert itens == []
        assert avisos  # avisa que saiu de catálogo


# ── criar_pedido ─────────────────────────────────────────────────────

def _form(**kw):
    # CPF de teste válido (passa no DV padrão): 529.982.247-25
    base = {'nome': 'Maria', 'email': 'maria@x.com', 'telefone': '11999',
            'cpf': '52998224725', 'aceite_lgpd': '1'}
    # Endereço estruturado pros modos de entrega
    base.setdefault('logradouro', 'Rua X')
    base.setdefault('numero', '10')
    base.setdefault('bairro', 'Moema')
    base.setdefault('cidade', 'São Paulo')
    base.setdefault('uf', 'SP')
    base.setdefault('cep', '04077000')
    base.setdefault('endereco', 'Rua X, 10, Moema')  # legado
    base.update(kw)
    return base


def test_criar_pedido_retirada_ok(app):
    from app.extensions import db
    from app.services import loja_checkout
    with app.app_context():
        p = _produto_pub(db, preco=20.0)
        loja = _loja(db)
        base = datetime(2026, 6, 17, 10, 0)
        data = loja_checkout.datas_disponiveis('retirada', base=base)[0].isoformat()
        form = _form(modo_entrega='retirada', loja_id=str(loja.id),
                     data_entrega=data, janela_entrega='08:00–09:00')
        pedido, erros = loja_checkout.criar_pedido(
            form, [{'kind': 'produto', 'id': p.id, 'qtd': 2}], base=base)
        assert erros == []
        assert pedido.status == 'aguardando_pagamento'
        assert pedido.frete_valor == Decimal('0.00')
        assert pedido.subtotal == Decimal('40.00')
        assert pedido.valor_total == Decimal('40.00')
        assert pedido.loja_retirada_id == loja.id
        assert len(pedido.itens) == 1


def test_criar_pedido_agendada_recomputa_frete_no_servidor(app):
    from app.extensions import db
    from app.services import loja_checkout
    with app.app_context():
        p = _produto_pub(db, preco=30.0)
        base = datetime(2026, 6, 17, 10, 0)
        data = loja_checkout.datas_disponiveis('agendada', base=base)[0].isoformat()
        form = _form(modo_entrega='agendada', endereco='Rua X, 1, Moema',
                     cep='04077000', data_entrega=data, janela_entrega='12:00–13:00')
        with patch('app.services.frete.consultar_frete', return_value=FRETE_OK):
            pedido, erros = loja_checkout.criar_pedido(
                form, [{'kind': 'produto', 'id': p.id, 'qtd': 1, 'preco': 0.01}],
                base=base)
        assert erros == []
        # Frete do servidor (15), preço do catálogo (30) — ignora cliente.
        assert pedido.frete_valor == Decimal('15.0')
        assert pedido.subtotal == Decimal('30.00')
        assert pedido.valor_total == Decimal('45.0')
        assert pedido.distancia_km == 3.4


def test_criar_pedido_fora_de_area_falha(app):
    from app.extensions import db
    from app.services import loja_checkout
    with app.app_context():
        p = _produto_pub(db)
        base = datetime(2026, 6, 17, 10, 0)
        data = loja_checkout.datas_disponiveis('agendada', base=base)[0].isoformat()
        form = _form(modo_entrega='agendada', endereco='Muito longe',
                     data_entrega=data, janela_entrega='12:00–13:00')
        with patch('app.services.frete.consultar_frete', return_value=FRETE_FORA):
            pedido, erros = loja_checkout.criar_pedido(
                form, [{'kind': 'produto', 'id': p.id, 'qtd': 1}], base=base)
        assert pedido is None
        assert any('fora' in e.lower() for e in erros)


def test_criar_pedido_carrinho_vazio_falha(app):
    from app.services import loja_checkout
    with app.app_context():
        base = datetime(2026, 6, 17, 10, 0)
        form = _form(modo_entrega='retirada', loja_id='1',
                     data_entrega='2026-06-18', janela_entrega='08:00–09:00')
        pedido, erros = loja_checkout.criar_pedido(form, [], base=base)
        assert pedido is None
        assert any('vazio' in e.lower() or 'catálogo' in e.lower() for e in erros)


def test_criar_pedido_sem_aceite_falha(app):
    from app.extensions import db
    from app.services import loja_checkout
    with app.app_context():
        p = _produto_pub(db)
        loja = _loja(db)
        base = datetime(2026, 6, 17, 10, 0)
        data = loja_checkout.datas_disponiveis('retirada', base=base)[0].isoformat()
        form = _form(modo_entrega='retirada', loja_id=str(loja.id),
                     data_entrega=data, janela_entrega='08:00–09:00')
        del form['aceite_lgpd']
        pedido, erros = loja_checkout.criar_pedido(
            form, [{'kind': 'produto', 'id': p.id, 'qtd': 1}], base=base)
        assert pedido is None
        assert any('termos' in e.lower() for e in erros)


def test_criar_pedido_data_invalida_falha(app):
    from app.extensions import db
    from app.services import loja_checkout
    with app.app_context():
        p = _produto_pub(db)
        loja = _loja(db)
        base = datetime(2026, 6, 17, 10, 0)
        form = _form(modo_entrega='retirada', loja_id=str(loja.id),
                     data_entrega='2020-01-01',  # passado, inválida
                     janela_entrega='08:00–09:00')
        pedido, erros = loja_checkout.criar_pedido(
            form, [{'kind': 'produto', 'id': p.id, 'qtd': 1}], base=base)
        assert pedido is None
        assert any('data' in e.lower() for e in erros)


def test_criar_pedido_reusa_cliente_por_email(app):
    from app.extensions import db
    from app.models import Cliente
    from app.services import loja_checkout
    with app.app_context():
        p = _produto_pub(db)
        loja = _loja(db)
        base = datetime(2026, 6, 17, 10, 0)
        data = loja_checkout.datas_disponiveis('retirada', base=base)[0].isoformat()
        form = _form(modo_entrega='retirada', loja_id=str(loja.id),
                     data_entrega=data, janela_entrega='08:00–09:00')
        loja_checkout.criar_pedido(
            form, [{'kind': 'produto', 'id': p.id, 'qtd': 1}], base=base)
        loja_checkout.criar_pedido(
            form, [{'kind': 'produto', 'id': p.id, 'qtd': 1}], base=base)
        # Dois pedidos, UM cliente (reuso por email)
        assert Cliente.query.filter_by(email='maria@x.com').count() == 1


# ── Rotas ─────────────────────────────────────────────────────────────

def test_checkout_get_staff_200(app, monkeypatch):
    monkeypatch.delenv('LOJA_VISIVEL', raising=False)
    c = _admin_logado(app)
    r = c.get('/loja/checkout')
    assert r.status_code == 200
    assert b'checkout-form' in r.data


def test_checkout_anonimo_404(app, monkeypatch):
    monkeypatch.delenv('LOJA_VISIVEL', raising=False)
    c = app.test_client()
    assert c.get('/loja/checkout').status_code == 404


def test_checkout_post_cria_pedido_e_redireciona(app, monkeypatch):
    import json as _json

    from app.extensions import db
    from app.models import PedidoOnline
    from app.services import loja_checkout
    monkeypatch.delenv('LOJA_VISIVEL', raising=False)
    c = _admin_logado(app)
    p = _produto_pub(db, preco=25.0)
    loja = _loja(db)
    data = loja_checkout.datas_disponiveis('retirada')[0].isoformat()
    r = c.post('/loja/checkout', data={
        'nome': 'João', 'email': 'joao@x.com', 'telefone': '11888',
        'cpf': '52998224725',
        'aceite_lgpd': '1', 'modo_entrega': 'retirada',
        'loja_id': str(loja.id), 'data_entrega': data,
        'janela_entrega': '08:00–09:00',
        'itens_json': _json.dumps([{'kind': 'produto', 'id': p.id, 'qtd': 2}]),
    }, follow_redirects=False)
    assert r.status_code == 302
    assert '/loja/pedido/' in r.headers['Location']
    ped = PedidoOnline.query.filter_by(email_cliente='joao@x.com').first()
    assert ped is not None
    assert ped.valor_total == Decimal('50.00')


def test_checkout_post_preco_forjado_usa_catalogo(app, monkeypatch):
    """Cliente adultera o preço no itens_json — servidor usa o do catálogo."""
    import json as _json

    from app.extensions import db
    from app.models import PedidoOnline
    from app.services import loja_checkout
    monkeypatch.delenv('LOJA_VISIVEL', raising=False)
    c = _admin_logado(app)
    p = _produto_pub(db, preco=25.0)
    loja = _loja(db)
    data = loja_checkout.datas_disponiveis('retirada')[0].isoformat()
    c.post('/loja/checkout', data={
        'nome': 'Hacker', 'email': 'h@x.com', 'cpf': '52998224725',
        'aceite_lgpd': '1',
        'modo_entrega': 'retirada', 'loja_id': str(loja.id),
        'data_entrega': data, 'janela_entrega': '08:00–09:00',
        'itens_json': _json.dumps(
            [{'kind': 'produto', 'id': p.id, 'qtd': 1, 'preco': 0.01}]),
    })
    ped = PedidoOnline.query.filter_by(email_cliente='h@x.com').first()
    assert ped is not None
    assert ped.valor_total == Decimal('25.00')  # não 0.01


def test_api_frete_retorna_json(app, monkeypatch):
    monkeypatch.delenv('LOJA_VISIVEL', raising=False)
    c = _admin_logado(app)
    with patch('app.services.frete.consultar_frete', return_value=FRETE_OK):
        r = c.post('/loja/api/frete', json={'endereco': 'Rua X', 'cep': '04077000'})
    assert r.status_code == 200
    assert r.get_json()['valor'] == 15.0


def test_pedido_confirmado_mostra_codigo_e_limpa_carrinho(app, monkeypatch):
    from app.extensions import db
    from app.models import PedidoOnline
    monkeypatch.delenv('LOJA_VISIVEL', raising=False)
    c = _admin_logado(app)
    p = PedidoOnline(nome_cliente='M', email_cliente='m@x.com',
                     modo_entrega='retirada', subtotal=Decimal('10'),
                     valor_total=Decimal('10'))
    db.session.add(p)
    db.session.commit()
    r = c.get(f'/loja/pedido/{p.codigo}')
    assert r.status_code == 200
    assert p.codigo.encode() in r.data
    assert b'limpar-carrinho' in r.data  # marker pro carrinho.js esvaziar
