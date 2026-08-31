"""Perfil fixo de consulta multicanal, sem qualquer escrita."""
from datetime import timedelta
from decimal import Decimal

from app.extensions import db
from app.models import (
    Loja,
    PedidoItem,
    PedidoLocal,
    PedidoLocalItem,
    PedidoLoja,
    PedidoOnline,
    PedidoOnlineItem,
    Receita,
    Usuario,
    VendaB2B,
    VendaB2BItem,
)
from app.utils import agora, hoje


def _login(client, usuario):
    with client.session_transaction() as sess:
        sess['_user_id'] = str(usuario.id)
        sess['_fresh'] = True


def _observador():
    u = Usuario(nome='Agente Consulta', login='agente-consulta',
                papel='observador')
    u.set_senha('segura-123')
    db.session.add(u)
    db.session.commit()
    return u


def _pedidos_multicanal():
    loja = Loja(nome='Loja Centro', ativa=True)
    receita = Receita(nome='Sourdough Tradicional', categoria='Pães',
                      rendimento_qtd=1, rendimento_unidade='un',
                      peso_base=100)
    db.session.add_all([loja, receita])
    db.session.flush()

    pedido_loja = PedidoLoja(
        loja_id=loja.id, data_pedido=hoje(), data_entrega=hoje(),
        status='confirmado')
    db.session.add(pedido_loja)
    db.session.flush()
    db.session.add(PedidoItem(
        pedido_id=pedido_loja.id, receita_id=receita.id, quantidade=25))

    pedido_site = PedidoOnline(
        codigo='SITE123', nome_cliente='Cliente do Site',
        email_cliente='cliente@example.com', modo_entrega='retirada',
        loja_retirada_id=loja.id, data_entrega=hoje() + timedelta(days=1),
        subtotal=Decimal('48.00'), frete_valor=Decimal('0'),
        valor_total=Decimal('48.00'), status='pago', criado_em=agora())
    db.session.add(pedido_site)
    db.session.flush()
    db.session.add(PedidoOnlineItem(
        pedido_id=pedido_site.id, kind='receita', receita_id=receita.id,
        nome='Sourdough Tradicional', preco_unitario=Decimal('24.00'),
        quantidade=2, subtotal=Decimal('48.00')))

    b2b = VendaB2B(
        cliente_nome='Café Parceiro', data_venda=hoje(),
        data_entrega=hoje() + timedelta(days=2), status='ativa',
        status_entrega='pendente', valor_total=Decimal('90.00'),
        criado_em=agora())
    db.session.add(b2b)
    db.session.flush()
    db.session.add(VendaB2BItem(
        venda_id=b2b.id, receita_id=receita.id, quantidade=5,
        preco_unitario=Decimal('18.00')))

    manual = PedidoLocal(
        code='MAN123', destinatario='Cliente Manual', telefone='11999999999',
        endereco='Retirada', data_entrega=hoje() + timedelta(days=3),
        periodo='manhã', criado_em=agora())
    db.session.add(manual)
    db.session.flush()
    db.session.add(PedidoLocalItem(
        pedido_local_id=manual.id, nome='Brioche', quantidade=4,
        preco_unitario=12))
    db.session.commit()
    return pedido_loja.id


def test_observador_ve_todos_os_canais_em_uma_tela(app):
    observador = _observador()
    _pedidos_multicanal()
    client = app.test_client()
    _login(client, observador)

    inicio = client.get('/')
    assert inicio.status_code == 302
    assert inicio.location.endswith('/pedidos/consulta')

    resp = client.get('/pedidos/consulta')
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    for texto in ('Somente leitura', 'Loja Centro', 'Cliente do Site',
                  'Café Parceiro', 'Cliente Manual', 'SITE123', 'MAN123'):
        assert texto in html
    assert 'Pedidos de todos os canais' in html
    assert 'Novo pedido' not in html
    assert 'Cancelar pedido' not in html
    assert 'Treinamento' not in html
    assert '/area/' not in html


def test_observador_nao_abre_ou_altera_nenhuma_outra_rota(app):
    observador = _observador()
    pedido_id = _pedidos_multicanal()
    client = app.test_client()
    _login(client, observador)

    fora = client.get('/pedidos/')
    assert fora.status_code == 302
    assert fora.location.endswith('/pedidos/consulta')

    tentativa = client.post(f'/pedidos/{pedido_id}/cancelar')
    assert tentativa.status_code == 403
    assert db.session.get(PedidoLoja, pedido_id).status == 'confirmado'


def test_observador_nao_herda_ferramentas_do_copilot(app):
    from app.services import copilot

    observador = _observador()
    assert copilot.papel_efetivo(observador) == 'observador'
    assert all(
        not copilot.pode_usar(nome, observador)
        for nome in copilot.PAPEIS_POR_TOOL
    )


def test_admin_pode_criar_perfil_observador(app, admin_user):
    client = app.test_client()
    _login(client, admin_user)

    pagina = client.get('/auth/usuarios')
    assert pagina.status_code == 200
    assert 'Observador — somente leitura' in pagina.get_data(as_text=True)

    resp = client.post('/auth/usuarios/novo', data={
        'nome': 'Auditoria', 'login': 'auditoria', 'papel': 'observador',
    })
    assert resp.status_code == 302
    criado = Usuario.query.filter_by(login='auditoria').one()
    assert criado.papel == 'observador'
    assert criado.senha_provisoria is True
