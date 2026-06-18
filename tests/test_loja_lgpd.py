"""LGPD self-service (Fase 6 — PR 7).

- Exportar dados (Art. 18, V — portabilidade): JSON com perfil + endereços
  + pedidos.
- Excluir conta (Art. 18, VI): anonimiza pedidos (mantém histórico
  fiscal/NF) e apaga endereços + senha + email.
"""
from decimal import Decimal


def _cadastrar(c, email='lgpd@x.com', nome='LGPD'):
    return c.post('/loja/cadastrar', data={
        'nome': nome, 'email': email, 'telefone': '119',
        'senha': 'senha-forte-1', 'aceite_lgpd': '1',
    }, follow_redirects=False)


def _pedido_pra(db, cli, codigo='LGPD01'):
    from app.models import PedidoOnline
    p = PedidoOnline(codigo=codigo, cliente_id=cli.id,
                     nome_cliente=cli.nome, email_cliente=cli.email,
                     telefone_cliente=cli.telefone or '',
                     modo_entrega='retirada', status='pago',
                     subtotal=Decimal('10'), frete_valor=Decimal('0'),
                     valor_total=Decimal('10'))
    db.session.add(p)
    db.session.commit()
    return p


def test_exportar_dados_devolve_json_com_perfil_e_pedidos(app):
    """Exporta inclui perfil + pedidos do cliente."""
    import json

    from app.extensions import db
    from app.models import Cliente
    c = app.test_client()
    _cadastrar(c, email='exp@x.com')
    with app.app_context():
        cli = Cliente.query.filter_by(email='exp@x.com').first()
        _pedido_pra(db, cli, codigo='EXP001')
    r = c.get('/loja/conta/dados.json')
    assert r.status_code == 200
    assert r.mimetype == 'application/json'
    assert 'attachment' in r.headers['Content-Disposition']
    dados = json.loads(r.data)
    assert dados['perfil']['email'] == 'exp@x.com'
    assert any(p['codigo'] == 'EXP001' for p in dados['pedidos'])


def test_excluir_conta_anonimiza_pedidos_e_loga_fora(app):
    """Excluir: pedidos viram '[Conta excluída #N]', endereços apagados,
    senha removida, sessão limpa."""
    from app.extensions import db
    from app.models import Cliente, EnderecoCliente, PedidoOnline
    c = app.test_client()
    _cadastrar(c, email='del@x.com', nome='Del')
    with app.app_context():
        cli = Cliente.query.filter_by(email='del@x.com').first()
        _pedido_pra(db, cli, codigo='DEL001')
        # Endereço pra ver que apaga
        end = EnderecoCliente(cliente_id=cli.id, logradouro='R', numero='1',
                              cidade='SP', uf='SP')
        db.session.add(end)
        db.session.commit()
        cli_id = cli.id
    r = c.post('/loja/conta/excluir', data={'confirmar': 'EXCLUIR'},
                follow_redirects=False)
    assert r.status_code == 302
    # Sessão limpa
    with c.session_transaction() as s:
        assert 'cliente_id' not in s
    with app.app_context():
        from app.models import Cliente
        cli = Cliente.query.get(cli_id)
        # Cliente anonimizado, conta inativa, sem senha
        assert cli.ativo is False
        assert cli.senha_hash is None
        assert cli.email.endswith('@anonimo.local')
        # Endereço sumiu
        assert EnderecoCliente.query.filter_by(cliente_id=cli_id).count() == 0
        # Pedido NÃO sumiu (histórico fiscal) mas foi anonimizado
        ped = PedidoOnline.query.filter_by(codigo='DEL001').first()
        assert ped is not None
        assert ped.email_cliente == ''
        assert '[Conta exclu' in ped.nome_cliente
        assert ped.cliente_id is None   # desligado da conta


def test_excluir_sem_confirmar_bloqueia(app):
    """Sem digitar EXCLUIR: redireciona com flash, não exclui."""
    from app.models import Cliente
    c = app.test_client()
    _cadastrar(c, email='nc@x.com')
    r = c.post('/loja/conta/excluir', data={'confirmar': 'sim'},
                follow_redirects=False)
    assert r.status_code == 302
    # Sessão CONTINUA — não logou fora
    with c.session_transaction() as s:
        assert 'cliente_id' in s
    with app.app_context():
        cli = Cliente.query.filter_by(email='nc@x.com').first()
        assert cli.ativo is True   # ainda ativo


def test_exportar_dados_exige_login(app, monkeypatch):
    monkeypatch.setenv('LOJA_VISIVEL', '1')
    c = app.test_client()
    r = c.get('/loja/conta/dados.json', follow_redirects=False)
    assert r.status_code == 302
    assert '/loja/entrar' in r.headers['Location']


def test_minha_conta_mostra_botoes_lgpd(app):
    c = app.test_client()
    _cadastrar(c, email='mc@x.com')
    r = c.get('/loja/conta')
    assert r.status_code == 200
    assert b'Baixar meus dados' in r.data
    assert b'Excluir minha conta' in r.data
