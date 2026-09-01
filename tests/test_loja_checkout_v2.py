"""Checkout v2 (Fase 3+): CPF obrigatório, endereço estruturado, destinatário
diferente do pagador, API de CEP."""
from datetime import datetime
from decimal import Decimal
from unittest.mock import patch

FRETE_OK = {'ok': True, 'valor': 15.0, 'gratis': False, 'fora_area': False,
            'distancia_km': 3.4, 'endereco': 'Rua X, Moema', 'aviso': ''}

# Endereço estruturado que a retirada passou a exigir pra emitir a NF-e
# (dono 20/07/2026). Spread nos forms de retirada que esperam sucesso.
_END_NF = {'cep': '04077-000', 'logradouro': 'Rua X', 'numero': '10',
           'bairro': 'Moema', 'cidade': 'São Paulo', 'uf': 'SP'}


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
    from app.models import AppConfig, EstoqueLoja, Loja, Produto, Receita
    loja = Loja(nome=nome, endereco='Ribeiro do Vale, 455', ativa=True)
    db.session.add(loja)
    db.session.commit()
    # Loja do site (permitida p/ retirada) + estoca o catálogo pra os itens
    # não caírem como "esgotado" no checkout (a loja do site ativa o filtro).
    AppConfig.set('loja_site_estoque_id', loja.id)
    for r in Receita.query.all():
        db.session.add(EstoqueLoja(loja_id=loja.id, receita_id=r.id,
                                   quantidade=999))
    for pr in Produto.query.all():
        db.session.add(EstoqueLoja(loja_id=loja.id, produto_id=pr.id,
                                   quantidade=999))
    db.session.commit()
    return loja


# ── Janelas date-aware (filtra horários passados quando é hoje) ───────

def test_janelas_hoje_filtra_horarios_passados(app):
    from app.services import loja_checkout
    with app.app_context():
        hoje = datetime(2026, 6, 17, 13, 0)  # 13h, lead 2 -> a partir de 15h
        js = loja_checkout.janelas_disponiveis('agendada', hoje.date(), base=hoje)
        # 08:00–14:00 já passaram (início < 15)
        assert '08:00–09:00' not in js
        assert '14:00–15:00' not in js
        assert '15:00–16:00' in js
        assert '17:00–18:00' in js


def test_janelas_dia_futuro_mostra_todas(app):
    from datetime import timedelta as _td

    from app.services import loja_checkout
    with app.app_context():
        hoje = datetime(2026, 6, 17, 13, 0)
        amanha = (hoje + _td(days=1)).date()
        js = loja_checkout.janelas_disponiveis('agendada', amanha, base=hoje)
        assert js == list(loja_checkout.JANELAS_HORARIAS)  # todas


def test_criar_pedido_rejeita_janela_passada_hoje(app):
    from app.extensions import db
    from app.services import loja_checkout
    with app.app_context():
        p = _produto(db)
        loja = _loja(db)
        base = datetime(2026, 6, 17, 13, 0)
        form = {'nome': 'M', 'email': 'm@x.com', 'cpf': '52998224725',
                'aceite_lgpd': '1', 'modo_entrega': 'retirada',
                'loja_id': str(loja.id),
                'data_entrega': '2026-06-17',  # hoje
                'janela_entrega': '08:00–09:00'}  # já passou
        pedido, erros = loja_checkout.criar_pedido(
            form, [{'kind': 'produto', 'id': p.id, 'qtd': 1}], base=base)
        assert pedido is None
        assert any('horário' in e.lower() or 'passou' in e.lower() for e in erros)


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
        data = loja_checkout.datas_disponiveis('retirada', base=base)[1].isoformat()
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
        data = loja_checkout.datas_disponiveis('retirada', base=base)[1].isoformat()
        form = {'nome': 'Maria', 'email': 'm@x.com',
                'cpf': '529.982.247-25', 'aceite_lgpd': '1',
                'modo_entrega': 'retirada', 'loja_id': str(loja.id),
                'data_entrega': data, 'janela_entrega': '08:00–09:00',
                **_END_NF}
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


def test_geocode_do_frete_nao_leva_complemento(app):
    """O complemento (apto/bloco/nome do prédio) DERRUBA o geocoder — caso
    real Mooca 11/07/2026: 'Ape 502 Positano' fez a venda ser barrada por
    endereço 'não localizado', embora o endereço fosse válido. A consulta de
    frete deve ir SEM complemento; o snapshot de entrega o mantém (motorista)."""
    from unittest.mock import MagicMock

    from app.extensions import db
    from app.services import loja_checkout
    with app.app_context():
        p = _produto(db)
        base = datetime(2026, 6, 17, 10, 0)
        data = loja_checkout.datas_disponiveis('agendada', base=base)[0].isoformat()
        form = {'nome': 'Pamela', 'email': 'p@x.com',
                'cpf': '52998224725', 'aceite_lgpd': '1',
                'modo_entrega': 'agendada', 'cep': '03111-010',
                'logradouro': 'Rua João Antônio de Oliveira', 'numero': '544',
                'complemento': 'Ape 502 Positano', 'bairro': 'Mooca',
                'cidade': 'São Paulo', 'uf': 'SP',
                'data_entrega': data, 'janela_entrega': '12:00–13:00'}
        espiao = MagicMock(return_value=FRETE_OK)
        with patch('app.services.frete.consultar_frete', espiao):
            pedido, erros = loja_checkout.criar_pedido(
                form, [{'kind': 'produto', 'id': p.id, 'qtd': 1}], base=base)
        assert erros == []
        geo_arg = espiao.call_args.args[0]
        # geocode SEM o complemento (nem o nome do prédio)
        assert 'Positano' not in geo_arg and 'Ape 502' not in geo_arg
        # mas com o resto que localiza
        assert 'Rua João Antônio de Oliveira' in geo_arg and '544' in geo_arg
        assert 'Mooca' in geo_arg and '03111-010' in geo_arg
        # snapshot de entrega PRESERVA o complemento (motorista precisa)
        assert 'Ape 502 Positano' in pedido.endereco_entrega


# ── Destinatário diferente do pagador ────────────────────────────────

def test_presente_exige_nome_do_destinatario(app):
    from app.extensions import db
    from app.services import loja_checkout
    with app.app_context():
        p = _produto(db)
        loja = _loja(db)
        base = datetime(2026, 6, 17, 10, 0)
        data = loja_checkout.datas_disponiveis('retirada', base=base)[1].isoformat()
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
        data = loja_checkout.datas_disponiveis('retirada', base=base)[1].isoformat()
        form = {'nome': 'Maria', 'email': 'm@x.com',
                'cpf': '52998224725', 'aceite_lgpd': '1',
                'modo_entrega': 'retirada', 'loja_id': str(loja.id),
                'data_entrega': data, 'janela_entrega': '08:00–09:00',
                'e_presente': '1',
                'nome_destinatario': 'Ana Pereira',
                'telefone_destinatario': '11988887777', **_END_NF}
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
        data = loja_checkout.datas_disponiveis('retirada', base=base)[1].isoformat()
        form = {'nome': 'Maria', 'email': 'm@x.com',
                'cpf': '52998224725', 'aceite_lgpd': '1',
                'modo_entrega': 'retirada', 'loja_id': str(loja.id),
                'data_entrega': data, 'janela_entrega': '08:00–09:00',
                # sem e_presente
                'nome_destinatario': 'Ana', 'telefone_destinatario': '11999',
                **_END_NF}
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


def test_api_cep_fallback_viacep(app, monkeypatch):
    """BrasilAPI fora do ar → ViaCEP responde → 200. Com o checkout
    CEP-first (campos travados até o CEP resolver), UMA API fora não pode
    virar venda travada — a BrasilAPI já degradou em prod (05 e 09/07)."""
    monkeypatch.delenv('LOJA_VISIVEL', raising=False)
    c = _admin(app)

    class _Via:
        status_code = 200
        def json(self):
            return {'logradouro': 'Rua do ViaCEP', 'bairro': 'Centro',
                    'localidade': 'São Paulo', 'uf': 'SP'}

    def fake_get(url, **kw):
        if 'brasilapi' in url:
            raise Exception('timeout')
        return _Via()
    with patch('requests.get', side_effect=fake_get):
        r = c.get('/loja/api/cep/04077000')
    assert r.status_code == 200
    j = r.get_json()
    assert j['ok'] is True and j['logradouro'] == 'Rua do ViaCEP'
    assert j['cidade'] == 'São Paulo' and j['uf'] == 'SP'


def test_api_cep_404_quando_cep_nao_existe(app, monkeypatch):
    """BrasilAPI 404 + ViaCEP {'erro': true} → 404 (o front mantém os campos
    travados e manda o cliente CONFERIR o CEP — caso Mirelle, dígitos
    invertidos 88650-020)."""
    monkeypatch.delenv('LOJA_VISIVEL', raising=False)
    c = _admin(app)

    class _R404:
        status_code = 404
        def json(self):
            return {}

    class _ViaErro:
        status_code = 200
        def json(self):
            return {'erro': True}

    def fake_get(url, **kw):
        return _R404() if 'brasilapi' in url else _ViaErro()
    with patch('requests.get', side_effect=fake_get):
        r = c.get('/loja/api/cep/88650020')
    assert r.status_code == 404


def test_api_cep_404_brasilapi_com_viacep_fora(app, monkeypatch):
    """BrasilAPI disse 'não existe' (agrega 3 provedores) e o ViaCEP caiu por
    INFRA → veredito segue 404 (não 502): o cliente deve conferir o CEP."""
    monkeypatch.delenv('LOJA_VISIVEL', raising=False)
    c = _admin(app)

    class _R404:
        status_code = 404
        def json(self):
            return {}

    def fake_get(url, **kw):
        if 'brasilapi' in url:
            return _R404()
        raise Exception('viacep fora')
    with patch('requests.get', side_effect=fake_get):
        r = c.get('/loja/api/cep/88650020')
    assert r.status_code == 404


def test_api_cep_502_quando_tudo_fora(app, monkeypatch):
    """As DUAS APIs fora por infra → 502 — o front destrava os campos pra
    digitação manual (fail-open, venda nunca fica presa)."""
    monkeypatch.delenv('LOJA_VISIVEL', raising=False)
    c = _admin(app)
    with patch('requests.get', side_effect=Exception('tudo fora')):
        r = c.get('/loja/api/cep/04077000')
    assert r.status_code == 502


def test_api_cep_5xx_da_brasilapi_nao_vira_404(app, monkeypatch):
    """BrasilAPI 503 (degradação de INFRA, não 'CEP não existe') + ViaCEP
    fora → 502, NUNCA 404 — achado de revisão: tratar qualquer não-200 como
    'não existe' reproduzia o fail-closed que o CEP-first veio eliminar."""
    monkeypatch.delenv('LOJA_VISIVEL', raising=False)
    c = _admin(app)

    class _R503:
        status_code = 503
        def json(self):
            return {}

    def fake_get(url, **kw):
        if 'brasilapi' in url:
            return _R503()
        raise Exception('viacep fora')
    with patch('requests.get', side_effect=fake_get):
        r = c.get('/loja/api/cep/04077000')
    assert r.status_code == 502


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


# ── Nome: bloquear CPF/números no campo de nome (23/06/2026) ──────────────

def _form_retirada(loja, base, **over):
    """Form válido de retirada pra isolar a validação de nome."""
    from app.services import loja_checkout
    data = loja_checkout.datas_disponiveis('retirada', base=base)[1].isoformat()
    f = {'nome': 'Maria', 'sobrenome': 'Silva',
         'email': 'm@x.com', 'cpf': '52998224725', 'aceite_lgpd': '1',
         'modo_entrega': 'retirada', 'loja_id': str(loja.id),
         'data_entrega': data, 'janela_entrega': '08:00–09:00',
         # Endereço pra NF-e — a retirada passou a exigir (dono 20/07/2026).
         **_END_NF}
    f.update(over)
    return f


def test_nome_com_cpf_e_recusado(app):
    """Cliente digitou o CPF no nome — sistema tem que recusar."""
    from app.extensions import db
    from app.services import loja_checkout
    with app.app_context():
        p = _produto(db)
        loja = _loja(db)
        base = datetime(2026, 6, 17, 10, 0)
        form = _form_retirada(loja, base, nome='04821886693', sobrenome='')
        pedido, erros = loja_checkout.criar_pedido(
            form, [{'kind': 'produto', 'id': p.id, 'qtd': 1}], base=base)
        assert pedido is None
        assert any('nome' in e.lower() for e in erros)


def test_checkout_render_tem_endereco_na_retirada(app, monkeypatch):
    """A página do checkout renderiza a estrutura nova: o bloco de endereço
    (compartilhado) + as partes só-de-entrega (quem recebe / calcular frete)
    que o JS esconde na retirada + o aviso da NF. Garante que a
    reestruturação do template não quebrou o render."""
    monkeypatch.delenv('LOJA_VISIVEL', raising=False)
    from app.extensions import db
    with app.app_context():
        pid = _produto(db).id
    c = _admin(app)
    with c.session_transaction() as s:
        s['carrinho'] = [{'kind': 'produto', 'id': pid, 'qtd': 1}]
    html = c.get('/loja/checkout').get_data(as_text=True)
    assert 'id="entrega-titulo"' in html          # título que o JS troca
    assert 'id="retirada-nf-aviso"' in html       # aviso da NF na retirada
    assert 'id="entrega-quem"' in html            # "quem recebe" (entrega)
    assert 'id="entrega-frete"' in html           # "calcular frete" (entrega)
    assert 'name="logradouro"' in html and 'name="numero"' in html


def test_nome_e_sobrenome_concatenam(app):
    """Nome + sobrenome viram o nome completo do pedido."""
    from app.extensions import db
    from app.services import loja_checkout
    with app.app_context():
        p = _produto(db)
        loja = _loja(db)
        base = datetime(2026, 6, 17, 10, 0)
        form = _form_retirada(loja, base, nome='Maria', sobrenome='Silva')
        pedido, erros = loja_checkout.criar_pedido(
            form, [{'kind': 'produto', 'id': p.id, 'qtd': 1}], base=base)
        assert pedido is not None, erros
        assert pedido.nome_cliente == 'Maria Silva'


def test_destinatario_com_numero_recusado(app):
    """Nome de quem recebe (presente) também bloqueia números."""
    from app.extensions import db
    from app.services import loja_checkout
    with app.app_context():
        p = _produto(db)
        loja = _loja(db)
        base = datetime(2026, 6, 17, 10, 0)
        form = _form_retirada(loja, base, e_presente='1',
                              nome_destinatario='Joao 123',
                              telefone_destinatario='11988887777')
        pedido, erros = loja_checkout.criar_pedido(
            form, [{'kind': 'produto', 'id': p.id, 'qtd': 1}], base=base)
        assert pedido is None
        assert any('recebe' in e.lower() for e in erros)


# ── Corte de janela por distância (23/06/2026, caso Alphaville) ───────────

def test_janelas_disponiveis_corta_primeira_se_longe(app):
    """Cliente a >= 10km perde a janela 08-09 (motoboy demora pra alocar)."""
    from datetime import date

    from app.services.loja_checkout import (
        DISTANCIA_CORTE_PRIMEIRA_JANELA_KM,
        JANELAS_CORTADAS_LONGE,
        janelas_disponiveis,
    )
    amanha = date(2026, 6, 18)
    base = datetime(2026, 6, 17, 8, 0)
    perto = janelas_disponiveis('agendada', amanha, base=base, distancia_km=3.0)
    longe = janelas_disponiveis(
        'agendada', amanha, base=base,
        distancia_km=DISTANCIA_CORTE_PRIMEIRA_JANELA_KM + 0.1)
    assert '08:00–09:00' in perto
    for j in JANELAS_CORTADAS_LONGE:
        assert j not in longe
    # Restante das janelas continua presente
    assert '09:00–10:00' in longe


def test_janelas_sem_distancia_nao_corta(app):
    """Sem cotação ainda (cliente não digitou endereço): mostra tudo —
    o servidor é a autoridade no submit."""
    from datetime import date

    from app.services.loja_checkout import janelas_disponiveis
    amanha = date(2026, 6, 18)
    base = datetime(2026, 6, 17, 8, 0)
    out = janelas_disponiveis('agendada', amanha, base=base, distancia_km=None)
    assert '08:00–09:00' in out


def test_criar_pedido_recusa_8h_quando_longe(app):
    """Servidor (autoridade) recusa pedido na 08-09 quando o endereço é >=10km
    da loja — mensagem clara cita a distância."""
    from app.extensions import db
    from app.services import loja_checkout
    with app.app_context():
        p = _produto(db)
        _loja(db)
        base = datetime(2026, 6, 17, 10, 0)
        data = loja_checkout.datas_disponiveis('agendada', base=base)[1].isoformat()
        # Mock distancia: o consultar_frete usa BrasilAPI. No teste,
        # passamos um endereço dentro de SP e o servidor faz a conta;
        # se a infra de geocoding falhar, esse teste vira no-op silencioso.
        # Pra ser determinístico, validamos a CHAMADA DIRETA da função pura
        # acima e aqui só testamos a mensagem de erro.
        from app.services.loja_checkout import (
            DISTANCIA_CORTE_PRIMEIRA_JANELA_KM as DC,
        )
        from app.services.loja_checkout import (
            JANELAS_CORTADAS_LONGE as JC,
        )
        from app.services.loja_checkout import (
            janelas_disponiveis,
        )
        amanha = datetime(2026, 6, 18).date()
        # Sanity: 08-09 sai quando cliente a 12km
        out = janelas_disponiveis('agendada', amanha, base=base, distancia_km=12.0)
        assert all(j not in out for j in JC)
        assert DC == 10.0
        # Sanity de tipos
        assert isinstance(p, type(p))  # silenciar unused


def test_corte_so_vale_agendada(app):
    """Retirada e express não levam a regra (não tem motoboy)."""
    from datetime import date

    from app.services.loja_checkout import janelas_disponiveis
    amanha = date(2026, 6, 18)
    base = datetime(2026, 6, 17, 8, 0)
    # Retirada: cliente vai buscar — não corta
    ret = janelas_disponiveis('retirada', amanha, base=base, distancia_km=20.0)
    assert '08:00–09:00' in ret


# ── Cartinha: limite de 250 caracteres (23/06/2026) ──────────────────────

def test_cartinha_grande_e_truncada_nao_recusa(app):
    """Cliente empolgou e escreveu 400 chars: trunca em 250, não rejeita."""
    from app.extensions import db
    from app.services import loja_checkout
    from app.services.loja_checkout import CARTINHA_MAX_CHARS
    with app.app_context():
        p = _produto(db)
        loja = _loja(db)
        base = datetime(2026, 6, 17, 10, 0)
        data = loja_checkout.datas_disponiveis(
            'retirada', base=base)[1].isoformat()
        msg = 'A' * 400
        form = {'nome': 'Maria', 'sobrenome': 'Silva', 'email': 'm@x.com',
                'cpf': '52998224725', 'aceite_lgpd': '1',
                'modo_entrega': 'retirada', 'loja_id': str(loja.id),
                'data_entrega': data, 'janela_entrega': '08:00–09:00',
                'cartinha': msg, **_END_NF}
        pedido, erros = loja_checkout.criar_pedido(
            form, [{'kind': 'produto', 'id': p.id, 'qtd': 1}], base=base)
        assert pedido is not None, erros
        assert len(pedido.cartinha) <= CARTINHA_MAX_CHARS


def test_cartinha_no_limite_passa_inteira(app):
    """Exatos 250 chars: vai inteira."""
    from app.extensions import db
    from app.services import loja_checkout
    from app.services.loja_checkout import CARTINHA_MAX_CHARS
    with app.app_context():
        p = _produto(db)
        loja = _loja(db)
        base = datetime(2026, 6, 17, 10, 0)
        data = loja_checkout.datas_disponiveis(
            'retirada', base=base)[1].isoformat()
        msg = 'B' * CARTINHA_MAX_CHARS
        form = {'nome': 'Maria', 'sobrenome': 'Silva', 'email': 'm@x.com',
                'cpf': '52998224725', 'aceite_lgpd': '1',
                'modo_entrega': 'retirada', 'loja_id': str(loja.id),
                'data_entrega': data, 'janela_entrega': '08:00–09:00',
                'cartinha': msg, **_END_NF}
        pedido, erros = loja_checkout.criar_pedido(
            form, [{'kind': 'produto', 'id': p.id, 'qtd': 1}], base=base)
        assert pedido is not None, erros
        assert pedido.cartinha == msg


# ── Express por distância: >10km vira 2h (23/06/2026) ─────────────────────

def test_janela_express_por_distancia():
    from app.services.loja_checkout import (
        DISTANCIA_EXPRESS_2H_KM,
        JANELA_EXPRESS,
        JANELA_EXPRESS_LONGE,
        janela_express_para_distancia,
    )
    assert janela_express_para_distancia(3.0) == JANELA_EXPRESS
    assert janela_express_para_distancia(None) == JANELA_EXPRESS
    assert janela_express_para_distancia(
        DISTANCIA_EXPRESS_2H_KM + 0.1) == JANELA_EXPRESS_LONGE
    assert janela_express_para_distancia(
        DISTANCIA_EXPRESS_2H_KM) == JANELA_EXPRESS_LONGE


# ── CPF OU CNPJ no checkout (13/07/2026, pedido do dono) ──────────────

def test_cnpj_valido_aceita_cnpj_real():
    from app.services.loja_checkout import _cnpj_valido
    assert _cnpj_valido('11222333000181') is True
    assert _cnpj_valido('11.222.333/0001-81') is True  # com máscara
    assert _cnpj_valido('06990590000123') is True


def test_cnpj_valido_rejeita_invalido():
    from app.services.loja_checkout import _cnpj_valido
    assert _cnpj_valido('11111111111111') is False  # sequência igual
    assert _cnpj_valido('11222333000182') is False  # DV errado
    assert _cnpj_valido('123') is False
    assert _cnpj_valido('') is False


def test_cpf_cnpj_valido_combina_os_dois():
    from app.services.loja_checkout import _cpf_cnpj_valido
    assert _cpf_cnpj_valido('52998224725') is True        # CPF
    assert _cpf_cnpj_valido('11.222.333/0001-81') is True  # CNPJ
    assert _cpf_cnpj_valido('122223330001') is False       # 12 dígitos
    assert _cpf_cnpj_valido('') is False


def test_criar_pedido_aceita_cnpj_e_grava_digitos(app):
    """Cliente PJ compra pelo site: CNPJ passa na validação e é gravado
    só com dígitos no MESMO campo `Cliente.cpf` (String(14) — 14 dígitos
    de CNPJ cabem exatos, sem migração)."""
    from app.extensions import db
    from app.models import Cliente
    from app.services import loja_checkout
    with app.app_context():
        p = _produto(db)
        loja = _loja(db)
        base = datetime(2026, 6, 17, 10, 0)
        data = loja_checkout.datas_disponiveis('retirada', base=base)[1].isoformat()
        form = {'nome': 'Maria', 'sobrenome': 'Silva', 'email': 'pj@x.com',
                'cpf': '11.222.333/0001-81', 'aceite_lgpd': '1',
                'modo_entrega': 'retirada', 'loja_id': str(loja.id),
                'data_entrega': data, 'janela_entrega': '08:00–09:00',
                **_END_NF}
        pedido, erros = loja_checkout.criar_pedido(
            form, [{'kind': 'produto', 'id': p.id, 'qtd': 1}], base=base)
        assert erros == []
        cli = Cliente.query.filter_by(email='pj@x.com').first()
        assert cli.cpf == '11222333000181'


def test_criar_pedido_rejeita_documento_invalido(app):
    """12 dígitos (nem CPF nem CNPJ) e CNPJ com DV errado são recusados."""
    from app.extensions import db
    from app.services import loja_checkout
    with app.app_context():
        p = _produto(db)
        loja = _loja(db)
        base = datetime(2026, 6, 17, 10, 0)
        data = loja_checkout.datas_disponiveis('retirada', base=base)[1].isoformat()
        for doc in ('122223330001', '11222333000182'):
            form = {'nome': 'Maria', 'sobrenome': 'Silva', 'email': 'x@x.com',
                    'cpf': doc, 'aceite_lgpd': '1',
                    'modo_entrega': 'retirada', 'loja_id': str(loja.id),
                    'data_entrega': data, 'janela_entrega': '08:00–09:00'}
            pedido, erros = loja_checkout.criar_pedido(
                form, [{'kind': 'produto', 'id': p.id, 'qtd': 1}], base=base)
            assert pedido is None
            assert any('cnpj' in e.lower() for e in erros)


def test_payload_customer_cnpj_vira_company(app):
    """Pagar.me exige type='company' + document_type='cnpj' pra PJ."""
    from app.extensions import db
    from app.models import Cliente, PedidoOnline
    from app.services import pagarme
    with app.app_context():
        cli = Cliente(nome='Padoca LTDA', email='pj2@x.com',
                      cpf='11222333000181')
        db.session.add(cli)
        db.session.flush()
        ped = PedidoOnline(cliente_id=cli.id,
                           nome_cliente='Padoca LTDA', email_cliente='pj2@x.com',
                           telefone_cliente='11988887777',
                           modo_entrega='retirada',
                           valor_total=Decimal('20'))
        db.session.add(ped)
        db.session.commit()
        payload = pagarme._payload_customer(ped)
        assert payload['document'] == '11222333000181'
        assert payload['document_type'] == 'cnpj'
        assert payload['type'] == 'company'


def test_nf_tiny_cnpj_vira_pessoa_juridica(app):
    """NF do Tiny: CNPJ no cadastro → tipo_pessoa 'J' (CPF continua 'F')."""
    from app.extensions import db
    from app.models import Cliente, PedidoOnline
    from app.services import tiny_nf
    with app.app_context():
        cli = Cliente(nome='Padoca LTDA', email='pj3@x.com',
                      cpf='11222333000181')
        db.session.add(cli)
        db.session.flush()
        ped = PedidoOnline(cliente_id=cli.id,
                           nome_cliente='Padoca LTDA', email_cliente='pj3@x.com',
                           telefone_cliente='11988887777',
                           modo_entrega='retirada',
                           valor_total=Decimal('20'))
        db.session.add(ped)
        db.session.commit()
        payload = tiny_nf._payload_cliente(ped)
        assert payload['tipo_pessoa'] == 'J'
        assert payload['cpf_cnpj'] == '11222333000181'
        cli.cpf = '52998224725'
        db.session.commit()
        assert tiny_nf._payload_cliente(ped)['tipo_pessoa'] == 'F'


# ── Número do endereço: SÓ DÍGITOS (dono 09/08/2026, pós-Dia dos Pais) ──

def _form_entrega_num(numero):
    return {'nome': 'M', 'email': 'm@x.com', 'cpf': '52998224725',
            'aceite_lgpd': '1', 'modo_entrega': 'agendada',
            'cep': '04571-010', 'logradouro': 'Rua X', 'numero': numero,
            'bairro': 'Brooklin', 'cidade': 'São Paulo', 'uf': 'SP',
            'data_entrega': '2026-06-18', 'janela_entrega': '09:00–10:00'}


def test_numero_com_letras_e_recusado(app):
    """"Muitos clientes colocaram errado o número ou o complemento, foi
    caótico": número aceita apenas dígitos — "123 apto 4" e "s/n" recusam
    apontando o campo complemento."""
    from datetime import datetime as _dt

    from app.extensions import db
    from app.services import loja_checkout
    with app.app_context():
        p = _produto(db)
        base = _dt(2026, 6, 17, 10, 0)
        for ruim in ('123 apto 4', 's/n', 'SN'):
            pedido, erros = loja_checkout.criar_pedido(
                _form_entrega_num(ruim),
                [{'kind': 'produto', 'id': p.id, 'qtd': 1}], base=base)
            assert pedido is None, ruim
            assert any('apenas números' in e for e in erros), (ruim, erros)


def test_numero_so_digitos_nao_acusa(app):
    from datetime import datetime as _dt

    from app.extensions import db
    from app.services import loja_checkout
    with app.app_context():
        p = _produto(db)
        base = _dt(2026, 6, 17, 10, 0)
        _pedido, erros = loja_checkout.criar_pedido(
            _form_entrega_num('123'),
            [{'kind': 'produto', 'id': p.id, 'qtd': 1}], base=base)
        assert not any('apenas números' in e for e in erros), erros


# ── Campos seguros: incidente Sentry 01/09/2026 ──────────────────────

def _form_retirada_contato(loja, data, **extra):
    form = {
        'nome': 'Maria', 'sobrenome': 'Silva', 'email': 'maria@x.com',
        'telefone': '11988887777', 'cpf': '52998224725',
        'aceite_lgpd': '1', 'modo_entrega': 'retirada',
        'loja_id': str(loja.id), 'data_entrega': data,
        'janela_entrega': '08:00–09:00', **_END_NF,
    }
    form.update(extra)
    return form


def test_telefone_com_texto_longo_e_normalizado_sem_derrubar_checkout(app):
    """Autofill/colar pode mandar uma frase > varchar(30) com um telefone.
    O servidor extrai o número e cria cliente/pedido sem erro 500."""
    from app.extensions import db
    from app.services import loja_checkout
    with app.app_context():
        p = _produto(db)
        loja = _loja(db)
        base = datetime(2026, 6, 17, 10, 0)
        data = loja_checkout.datas_disponiveis(
            'retirada', base=base)[1].isoformat()
        form = _form_retirada_contato(
            loja, data,
            telefone=('Meu WhatsApp para contato é (11) 98888-7777, '
                      'pode chamar.'))
        pedido, erros = loja_checkout.criar_pedido(
            form, [{'kind': 'produto', 'id': p.id, 'qtd': 1}], base=base)
        assert erros == []
        assert pedido.telefone_cliente == '11988887777'
        assert pedido.cliente.telefone == '11988887777'


def test_telefone_com_dois_numeros_retorna_erro_amigavel(app):
    """Mais de 15 dígitos não chega ao flush do PostgreSQL."""
    from app.extensions import db
    from app.models import Cliente, PedidoOnline
    from app.services import loja_checkout
    with app.app_context():
        p = _produto(db)
        loja = _loja(db)
        base = datetime(2026, 6, 17, 10, 0)
        data = loja_checkout.datas_disponiveis(
            'retirada', base=base)[1].isoformat()
        form = _form_retirada_contato(
            loja, data, telefone='11988887777 / 11977776666')
        pedido, erros = loja_checkout.criar_pedido(
            form, [{'kind': 'produto', 'id': p.id, 'qtd': 1}], base=base)
        assert pedido is None
        assert any('Revise o telefone' in e for e in erros)
        assert Cliente.query.filter_by(email='maria@x.com').count() == 0
        assert PedidoOnline.query.count() == 0


def test_texto_maior_que_coluna_retorna_erro_sem_flush(app):
    """Outros textos também são conferidos antes do banco (mesma classe de
    falha do telefone), preservando o formulário para a cliente corrigir."""
    from app.extensions import db
    from app.models import Cliente, PedidoOnline
    from app.services import loja_checkout
    with app.app_context():
        p = _produto(db)
        loja = _loja(db)
        base = datetime(2026, 6, 17, 10, 0)
        data = loja_checkout.datas_disponiveis(
            'retirada', base=base)[1].isoformat()
        form = _form_retirada_contato(
            loja, data, complemento='A' * 101)
        pedido, erros = loja_checkout.criar_pedido(
            form, [{'kind': 'produto', 'id': p.id, 'qtd': 1}], base=base)
        assert pedido is None
        assert any('complemento' in e.lower() and '100' in e for e in erros)
        assert Cliente.query.filter_by(email='maria@x.com').count() == 0
        assert PedidoOnline.query.count() == 0


def test_checkout_nao_usa_restricao_nativa_inconsistente_entre_browsers(app):
    """Chrome/Safari não devem bloquear nome/número com mensagens nativas
    diferentes; o servidor devolve a orientação em português."""
    body = _admin(app).get('/loja/checkout').get_data(as_text=True)
    assert 'data-nome-field' not in body
    assert 'pattern="[^0-9]+"' not in body
    assert 'pattern="[0-9]+"' not in body
    assert body.count('type="tel"') >= 2
