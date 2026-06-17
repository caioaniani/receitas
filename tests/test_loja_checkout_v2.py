"""Checkout v2 (Fase 3+): CPF obrigatório, endereço estruturado, destinatário
diferente do pagador, API de CEP."""
from datetime import datetime
from decimal import Decimal
from unittest.mock import patch

FRETE_OK = {'ok': True, 'valor': 15.0, 'gratis': False, 'fora_area': False,
            'distancia_km': 3.4, 'endereco': 'Rua X, Moema', 'aviso': ''}


def _admin(app):
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
    return c


def _produto(db, nome='Box Mimo', preco=20.0):
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


# ── Validador de CPF ─────────────────────────────────────────────────

def test_cpf_valido_aceita_cpf_real():
    from app.services.loja_checkout import _cpf_valido
    # CPFs com DV correto (algoritmo da Receita)
    assert _cpf_valido('52998224725') is True
    assert _cpf_valido('529.982.247-25') is True  # com máscara


def test_cpf_valido_rejeita_invalido():
    from app.services.loja_checkout import _cpf_valido
    assert _cpf_valido('11111111111') is False  # sequência igual
    assert _cpf_valido('12345678901') is False  # DV errado
    assert _cpf_valido('123') is False
    assert _cpf_valido('') is False


# ── CPF obrigatório no criar_pedido ──────────────────────────────────

def test_criar_pedido_sem_cpf_falha(app):
    from app.extensions import db
    from app.services import loja_checkout
    with app.app_context():
        p = _produto(db)
        loja = _loja(db)
        base = datetime(2026, 6, 17, 10, 0)
        data = loja_checkout.datas_disponiveis('retirada', base=base)[0].isoformat()
        form = {'nome': 'Maria', 'email': 'm@x.com', 'aceite_lgpd': '1',
                'modo_entrega': 'retirada', 'loja_id': str(loja.id),
                'data_entrega': data, 'janela_entrega': '08:00–09:00'}
        pedido, erros = loja_checkout.criar_pedido(
            form, [{'kind': 'produto', 'id': p.id, 'qtd': 1}], base=base)
        assert pedido is None
        assert any('cpf' in e.lower() for e in erros)


def test_criar_pedido_grava_cpf_no_cliente(app):
    from app.extensions import db
    from app.models import Cliente
    from app.services import loja_checkout
    with app.app_context():
        p = _produto(db)
        loja = _loja(db)
        base = datetime(2026, 6, 17, 10, 0)
        data = loja_checkout.datas_disponiveis('retirada', base=base)[0].isoformat()
        form = {'nome': 'Maria', 'email': 'm@x.com',
                'cpf': '529.982.247-25', 'aceite_lgpd': '1',
                'modo_entrega': 'retirada', 'loja_id': str(loja.id),
                'data_entrega': data, 'janela_entrega': '08:00–09:00'}
        pedido, erros = loja_checkout.criar_pedido(
            form, [{'kind': 'produto', 'id': p.id, 'qtd': 1}], base=base)
        assert erros == []
        cli = Cliente.query.filter_by(email='m@x.com').first()
        assert cli.cpf == '52998224725'  # só dígitos


# ── Endereço estruturado obrigatório ─────────────────────────────────

def test_agendada_exige_numero_do_endereco(app):
    from app.extensions import db
    from app.services import loja_checkout
    with app.app_context():
        p = _produto(db)
        base = datetime(2026, 6, 17, 10, 0)
        data = loja_checkout.datas_disponiveis('agendada', base=base)[0].isoformat()
        # CEP sozinho, sem número
        form = {'nome': 'Maria', 'email': 'm@x.com',
                'cpf': '52998224725', 'aceite_lgpd': '1',
                'modo_entrega': 'agendada',
                'cep': '04077-000', 'logradouro': 'Rua X', 'cidade': 'SP',
                # SEM numero
                'data_entrega': data, 'janela_entrega': '12:00–13:00'}
        with patch('app.services.frete.consultar_frete', return_value=FRETE_OK):
            pedido, erros = loja_checkout.criar_pedido(
                form, [{'kind': 'produto', 'id': p.id, 'qtd': 1}], base=base)
        assert pedido is None
        assert any('número' in e.lower() or 'numero' in e.lower() for e in erros)


def test_agendada_consolida_endereco_estruturado(app):
    from app.extensions import db
    from app.services import loja_checkout
    with app.app_context():
        p = _produto(db)
        base = datetime(2026, 6, 17, 10, 0)
        data = loja_checkout.datas_disponiveis('agendada', base=base)[0].isoformat()
        form = {'nome': 'Maria', 'email': 'm@x.com',
                'cpf': '52998224725', 'aceite_lgpd': '1',
                'modo_entrega': 'agendada',
                'cep': '04077-000',
                'logradouro': 'Avenida Brasil', 'numero': '123',
                'complemento': 'Apto 5', 'bairro': 'Moema',
                'cidade': 'São Paulo', 'uf': 'SP',
                'data_entrega': data, 'janela_entrega': '12:00–13:00'}
        with patch('app.services.frete.consultar_frete', return_value=FRETE_OK):
            pedido, erros = loja_checkout.criar_pedido(
                form, [{'kind': 'produto', 'id': p.id, 'qtd': 1}], base=base)
        assert erros == []
        # endereco_entrega é a string consolidada
        e = pedido.endereco_entrega
        assert 'Avenida Brasil' in e and '123' in e
        assert 'Apto 5' in e and 'Moema' in e
        assert 'São Paulo' in e and 'SP' in e


# ── Destinatário diferente do pagador ────────────────────────────────

def test_presente_exige_nome_do_destinatario(app):
    from app.extensions import db
    from app.services import loja_checkout
    with app.app_context():
        p = _produto(db)
        loja = _loja(db)
        base = datetime(2026, 6, 17, 10, 0)
        data = loja_checkout.datas_disponiveis('retirada', base=base)[0].isoformat()
        form = {'nome': 'Maria', 'email': 'm@x.com',
                'cpf': '52998224725', 'aceite_lgpd': '1',
                'modo_entrega': 'retirada', 'loja_id': str(loja.id),
                'data_entrega': data, 'janela_entrega': '08:00–09:00',
                'e_presente': '1',
                # SEM nome_destinatario
                }
        pedido, erros = loja_checkout.criar_pedido(
            form, [{'kind': 'produto', 'id': p.id, 'qtd': 1}], base=base)
        assert pedido is None
        assert any('destinatário' in e.lower() or 'recebe' in e.lower()
                   for e in erros)


def test_presente_salva_destinatario(app):
    from app.extensions import db
    from app.services import loja_checkout
    with app.app_context():
        p = _produto(db)
        loja = _loja(db)
        base = datetime(2026, 6, 17, 10, 0)
        data = loja_checkout.datas_disponiveis('retirada', base=base)[0].isoformat()
        form = {'nome': 'Maria', 'email': 'm@x.com',
                'cpf': '52998224725', 'aceite_lgpd': '1',
                'modo_entrega': 'retirada', 'loja_id': str(loja.id),
                'data_entrega': data, 'janela_entrega': '08:00–09:00',
                'e_presente': '1',
                'nome_destinatario': 'Ana Pereira',
                'telefone_destinatario': '11988887777'}
        pedido, erros = loja_checkout.criar_pedido(
            form, [{'kind': 'produto', 'id': p.id, 'qtd': 1}], base=base)
        assert erros == []
        assert pedido.nome_destinatario == 'Ana Pereira'
        assert pedido.telefone_destinatario == '11988887777'


def test_sem_presente_nao_salva_destinatario(app):
    """Checkbox desmarcado: ignora os campos do destinatário mesmo se
    vierem populados (cliente desmarcou depois de ter digitado)."""
    from app.extensions import db
    from app.services import loja_checkout
    with app.app_context():
        p = _produto(db)
        loja = _loja(db)
        base = datetime(2026, 6, 17, 10, 0)
        data = loja_checkout.datas_disponiveis('retirada', base=base)[0].isoformat()
        form = {'nome': 'Maria', 'email': 'm@x.com',
                'cpf': '52998224725', 'aceite_lgpd': '1',
                'modo_entrega': 'retirada', 'loja_id': str(loja.id),
                'data_entrega': data, 'janela_entrega': '08:00–09:00',
                # sem e_presente
                'nome_destinatario': 'Ana', 'telefone_destinatario': '11999'}
        pedido, _erros = loja_checkout.criar_pedido(
            form, [{'kind': 'produto', 'id': p.id, 'qtd': 1}], base=base)
        assert pedido.nome_destinatario is None
        assert pedido.telefone_destinatario is None


# ── API de CEP ───────────────────────────────────────────────────────

def test_api_cep_invalido(app, monkeypatch):
    monkeypatch.delenv('LOJA_VISIVEL', raising=False)
    c = _admin(app)
    r = c.get('/loja/api/cep/123')
    assert r.status_code == 400


def test_api_cep_ok(app, monkeypatch):
    monkeypatch.delenv('LOJA_VISIVEL', raising=False)
    c = _admin(app)

    class _R:
        status_code = 200
        def json(self):
            return {'street': 'Avenida Brasil', 'neighborhood': 'Centro',
                    'city': 'São Paulo', 'state': 'SP'}
    with patch('requests.get', return_value=_R()):
        r = c.get('/loja/api/cep/04077000')
    assert r.status_code == 200
    j = r.get_json()
    assert j['ok'] is True
    assert j['logradouro'] == 'Avenida Brasil'
    assert j['cidade'] == 'São Paulo'


# ── Pagamento Pagar.me: o payload customer agora envia CPF ───────────

def test_payload_customer_envia_cpf_do_cliente(app):
    """Regressão do bug 'The customer Document is required' — o customer
    enviado pro Pagar.me precisa incluir document quando o Cliente tem CPF."""
    from app.extensions import db
    from app.models import Cliente, PedidoOnline
    from app.services import pagarme
    with app.app_context():
        cli = Cliente(nome='Maria', email='m@x.com', cpf='52998224725')
        db.session.add(cli)
        db.session.flush()
        ped = PedidoOnline(cliente_id=cli.id,
                           nome_cliente='Maria', email_cliente='m@x.com',
                           telefone_cliente='11988887777',
                           modo_entrega='retirada',
                           valor_total=Decimal('20'))
        db.session.add(ped)
        db.session.commit()
        payload = pagarme._payload_customer(ped)
        assert payload['document'] == '52998224725'
        assert payload['document_type'] == 'cpf'
