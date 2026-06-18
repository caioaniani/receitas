"""Endereços salvos do cliente (Fase 6 — PR 6).

Auto-salva no fim do checkout (cliente logado + modo entrega) e
pré-preenche no GET do próximo checkout.
"""
from datetime import datetime
from decimal import Decimal
from unittest.mock import patch


def _produto(db, preco=20.0):
    from app.models import Produto
    p = Produto(nome='Box', categoria='Cestas', preco_site=preco,
                imagem_dropbox_url='https://x/p.jpg', ativo=True)
    db.session.add(p)
    db.session.commit()
    return p


def _cliente_logado(app, c, email='end@x.com'):
    from app.extensions import db
    from app.models import Cliente
    with app.app_context():
        cli = Cliente(nome='Cliente', email=email, telefone='119')
        cli.set_senha('senha-forte-1')
        cli.aceite_lgpd_em = datetime(2026, 6, 17, 10, 0)
        db.session.add(cli)
        db.session.commit()
        cli_id = cli.id
    with c.session_transaction() as s:
        s['cliente_id'] = cli_id
    return cli_id


def _form_entrega(**kw):
    base = {'nome': 'Cliente', 'email': 'end@x.com', 'telefone': '119',
            'cpf': '52998224725', 'aceite_lgpd': '1',
            'modo_entrega': 'agendada',
            'logradouro': 'Rua Tal', 'numero': '77',
            'complemento': 'apto 3', 'bairro': 'Centro',
            'cidade': 'São Paulo', 'uf': 'SP', 'cep': '04077000',
            'data_entrega': '', 'janela_entrega': '12:00–13:00'}
    base.update(kw)
    return base


def test_checkout_auto_salva_endereco_principal(app):
    """Cliente logado fecha um pedido de entrega → o endereço fica salvo
    como principal pra ele reusar."""
    from app.extensions import db
    from app.models import EnderecoCliente
    from app.services import loja_checkout
    c = app.test_client()
    cli_id = _cliente_logado(app, c)
    with app.app_context():
        prod = _produto(db, preco=30.0)
        base_dt = datetime(2026, 6, 17, 10, 0)
        data = loja_checkout.datas_disponiveis(
            'agendada', base=base_dt)[0].isoformat()
        form = _form_entrega(data_entrega=data)
        frete_ok = {'ok': True, 'valor': 15.0, 'gratis': False,
                    'fora_area': False, 'distancia_km': 3.0,
                    'endereco': 'Rua Tal, 77', 'aviso': ''}
        with patch('app.services.frete.consultar_frete', return_value=frete_ok):
            pedido, erros = loja_checkout.criar_pedido(
                form, [{'kind': 'produto', 'id': prod.id, 'qtd': 1}],
                base=base_dt)
        # Como criar_pedido roda fora do request flask-client, o Cliente
        # foi resolvido por email — então salva pra ele.
        assert erros == [] and pedido is not None
        ends = EnderecoCliente.query.filter_by(cliente_id=cli_id).all()
        assert len(ends) == 1
        e = ends[0]
        assert e.principal is True
        assert e.logradouro == 'Rua Tal'
        assert e.numero == '77'
        assert e.bairro == 'Centro'


def test_checkout_logado_pre_preenche_endereco_salvo(app, monkeypatch):
    """Cliente já tem endereço salvo → GET /loja/checkout traz preenchido."""
    monkeypatch.setenv('LOJA_VISIVEL', '1')
    from app.extensions import db
    from app.models import EnderecoCliente
    c = app.test_client()
    cli_id = _cliente_logado(app, c, email='reuso@x.com')
    with app.app_context():
        end = EnderecoCliente(
            cliente_id=cli_id, cep='01001000', logradouro='Rua Salva',
            numero='99', bairro='Sé', cidade='São Paulo', uf='SP',
            principal=True)
        db.session.add(end)
        db.session.commit()
    r = c.get('/loja/checkout')
    assert r.status_code == 200
    assert b'Rua Salva' in r.data
    assert b'01001000' in r.data
    assert b'Centro' not in r.data or b'value="99"' in r.data  # número certo


def test_fallback_endereco_do_ultimo_pedido_quando_sem_salvo(app, monkeypatch):
    """Cliente cadastrou e seus pedidos antigos eram como guest — sem
    `EnderecoCliente`. O checkout precisa usar o endereço do ÚLTIMO pedido
    como fallback (senão o cliente tem que redigitar tudo)."""
    monkeypatch.setenv('LOJA_VISIVEL', '1')
    from app.extensions import db
    from app.models import PedidoOnline
    c = app.test_client()
    cli_id = _cliente_logado(app, c, email='fb@x.com')
    with app.app_context():
        # Pedido antigo de entrega com endereço estruturado mas SEM
        # EnderecoCliente (cenário do usuário em prod 17/06/2026).
        from app.utils import agora
        p = PedidoOnline(codigo='OLD123', cliente_id=cli_id,
                         nome_cliente='X', email_cliente='fb@x.com',
                         modo_entrega='agendada', status='pago',
                         endereco_logradouro='Rua Antiga', endereco_numero='42',
                         endereco_bairro='Vila', endereco_cidade='São Paulo',
                         endereco_uf='SP', endereco_cep='04077000',
                         frete_valor=Decimal('5'), subtotal=Decimal('10'),
                         valor_total=Decimal('15'), criado_em=agora())
        db.session.add(p)
        db.session.commit()
    r = c.get('/loja/checkout')
    assert r.status_code == 200
    assert b'Rua Antiga' in r.data
    assert b'04077000' in r.data


def test_endereco_repetido_nao_duplica(app):
    """Segundo pedido com MESMO endereço → atualiza o existente, não
    duplica linha."""
    from app.extensions import db
    from app.models import EnderecoCliente
    from app.services import loja_checkout
    c = app.test_client()
    cli_id = _cliente_logado(app, c, email='nodup@x.com')
    with app.app_context():
        prod = _produto(db, preco=30.0)
        base_dt = datetime(2026, 6, 17, 10, 0)
        data = loja_checkout.datas_disponiveis(
            'agendada', base=base_dt)[0].isoformat()
        form = _form_entrega(email='nodup@x.com', data_entrega=data)
        frete_ok = {'ok': True, 'valor': 15.0, 'gratis': False,
                    'fora_area': False, 'distancia_km': 3.0,
                    'endereco': '', 'aviso': ''}
        with patch('app.services.frete.consultar_frete', return_value=frete_ok):
            # Pedido 1
            loja_checkout.criar_pedido(
                form, [{'kind': 'produto', 'id': prod.id, 'qtd': 1}],
                base=base_dt)
            # Pedido 2 (mesmo endereço)
            loja_checkout.criar_pedido(
                form, [{'kind': 'produto', 'id': prod.id, 'qtd': 2}],
                base=base_dt)
        ends = EnderecoCliente.query.filter_by(cliente_id=cli_id).all()
        assert len(ends) == 1   # deduplicou
