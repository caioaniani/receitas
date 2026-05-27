"""B2B na tela do padeiro (Fase 1): vendas B2B com data de entrega aparecem
junto dos pedidos de loja, com estado por item, e podem ser separadas. B2B sem
data de entrega (venda imediata) nao entra na fila. Sem mexer em estoque (o B2B
ja baixou do freezer na venda)."""
from datetime import timedelta

import pytest


@pytest.fixture
def cliente(app):
    return app.test_client()


def _login(cliente):
    return cliente.post('/auth/login', data={'login': 'admin', 'senha': '123'})


def _venda_b2b(app, admin_user, catalogo, *, data_entrega, estado='backup',
               status_entrega='pendente'):
    from app.extensions import db
    from app.models import VendaB2B, VendaB2BItem
    v = VendaB2B(cliente_nome='Bruno', data_entrega=data_entrega, status='ativa',
                 status_entrega=status_entrega, valor_total=0,
                 criado_por_id=admin_user.id)
    db.session.add(v)
    db.session.commit()
    db.session.add(VendaB2BItem(venda_id=v.id, receita_id=catalogo['receita'].id,
                                quantidade=20, preco_unitario=0, estado=estado))
    db.session.commit()
    return v


def test_b2b_com_data_aparece_no_padeiro(app, admin_user, catalogo, cliente):
    from app.utils import hoje
    v = _venda_b2b(app, admin_user, catalogo, data_entrega=hoje(), estado='backup')
    _login(cliente)
    r = cliente.get('/padeiro/')
    assert r.status_code == 200
    assert b'Bruno' in r.data
    assert b'[BACKUP]' in r.data  # estado do item B2B aparece no card
    assert ('/padeiro/b2b/%d/separar' % v.id).encode() in r.data  # form separar B2B


def test_b2b_sem_data_nao_aparece(app, admin_user, catalogo, cliente):
    _venda_b2b(app, admin_user, catalogo, data_entrega=None)
    _login(cliente)
    r = cliente.get('/padeiro/')
    assert r.status_code == 200
    assert b'Bruno' not in r.data  # venda imediata fica fora da fila do padeiro


def test_separar_b2b_muda_status(app, admin_user, catalogo, cliente):
    from app.models import VendaB2B
    from app.utils import hoje
    v = _venda_b2b(app, admin_user, catalogo, data_entrega=hoje())
    _login(cliente)
    r = cliente.post('/padeiro/b2b/%d/separar' % v.id,
                     data={'data': hoje().isoformat()})
    assert r.status_code == 302
    assert VendaB2B.query.get(v.id).status_entrega == 'separado'


def test_preparar_inclui_b2b_dia_seguinte(app, admin_user, catalogo, cliente):
    from app.utils import hoje
    amanha = hoje() + timedelta(days=1)
    _venda_b2b(app, admin_user, catalogo, data_entrega=amanha, estado='assado')
    _login(cliente)
    j = cliente.get('/padeiro/preparar.json?data=%s' % hoje().isoformat()).get_json()
    assert any(linha['estado_label'] == 'ASSADO' and linha['qtd'] == 20
               and 'Bruno' in linha['loja'] for linha in j['itens'])


def test_form_b2b_salva_data_entrega_e_aparece_amanha(app, admin_user, catalogo, cliente):
    """Reproduz o fluxo do dono: cria pelo formulario /b2b com entrega amanha e
    confere que (a) a data foi salva e (b) o card aparece no padeiro de amanha."""
    from app.models import VendaB2B
    from app.utils import hoje
    _login(cliente)
    amanha = hoje() + timedelta(days=1)
    rid = catalogo['receita'].id
    r = cliente.post('/b2b/vendas/nova', data={
        'cliente_nome': 'Bruno',
        'data_venda': hoje().isoformat(),
        'data_entrega': amanha.isoformat(),
        'item_ref[]': 'receita:%d' % rid,
        'item_qtd[]': '20',
        'item_preco[]': '10',
        'item_desc[]': '',
        'item_estado[]': 'backup',
    })
    assert r.status_code == 302
    v = VendaB2B.query.filter_by(cliente_nome='Bruno').first()
    assert v is not None
    assert v.data_entrega == amanha        # data de entrega persistida
    assert v.status_entrega == 'pendente'  # default de entrega
    r2 = cliente.get('/padeiro/?data=' + amanha.isoformat())
    assert b'Bruno' in r2.data             # card B2B aparece no dia de amanha


def test_criar_venda_guarda_data_entrega_e_estado(app, admin_user, catalogo):
    from app.models import VendaB2BItem
    from app.services import vendas_b2b as svc
    from app.utils import hoje
    amanha = hoje() + timedelta(days=1)
    venda = svc.criar_venda(
        cliente_nome='Bruno', data_entrega=amanha,
        itens=[{'tipo': 'receita', 'id': catalogo['receita'].id,
                'quantidade': 5, 'preco_unitario': 8.0, 'estado': 'backup'}],
        user=admin_user,
    )
    assert venda.data_entrega == amanha
    assert venda.status_entrega == 'pendente'  # default de entrega
    it = VendaB2BItem.query.filter_by(venda_id=venda.id).first()
    assert it.estado == 'backup'


def test_item_check_markup_para_separar(app, admin_user, catalogo, cliente):
    """Item em 'Para separar' vira alvo tocável: data-order no card, item-chk +
    data-item no item, e o contador data-chk-count."""
    from app.models import VendaB2BItem
    from app.utils import hoje
    v = _venda_b2b(app, admin_user, catalogo, data_entrega=hoje())
    it = VendaB2BItem.query.filter_by(venda_id=v.id).first()
    _login(cliente)
    r = cliente.get('/padeiro/listas.html?data=' + hoje().isoformat())
    assert r.status_code == 200
    assert ('data-order="b2b-%d"' % v.id).encode() in r.data
    assert b'class="item-chk"' in r.data
    assert ('data-item="%d"' % it.id).encode() in r.data
    assert b'data-chk-count' in r.data


def test_item_check_ausente_em_aguardando(app, admin_user, catalogo, cliente):
    """Seção 'aguardando motorista' (já separado) não traz check por item — o
    apoio visual é só durante a separação."""
    from app.utils import hoje
    _venda_b2b(app, admin_user, catalogo, data_entrega=hoje(),
               status_entrega='separado')
    _login(cliente)
    r = cliente.get('/padeiro/listas.html?data=' + hoje().isoformat())
    assert r.status_code == 200
    assert b'Bruno' in r.data         # card renderizado na seção aguardando
    assert b'item-chk' not in r.data  # mas sem item tocável (pós-separação)


def test_prep_ticker_banner_no_markup(app, admin_user, catalogo, cliente):
    """O banner piscante de pré-preparo existe na página (escondido por padrão; o
    show/hide é client-side conforme prepItens)."""
    _login(cliente)
    r = cliente.get('/padeiro/')
    assert r.status_code == 200
    assert b'id="prep-ticker"' in r.data
    assert 'Pedido para preparar para amanhã já disponível'.encode() in r.data


def test_entregar_b2b_sai_da_fila(app, admin_user, catalogo, cliente):
    """B2B separado: 'Marcar entregue' -> status_entrega='entregue' e sai do padeiro."""
    from app.models import VendaB2B
    from app.utils import hoje
    v = _venda_b2b(app, admin_user, catalogo, data_entrega=hoje(),
                   status_entrega='separado')
    _login(cliente)
    r0 = cliente.get('/padeiro/?data=' + hoje().isoformat())
    assert ('/padeiro/b2b/%d/entregue' % v.id).encode() in r0.data  # botao no card
    r = cliente.post('/padeiro/b2b/%d/entregue' % v.id,
                     data={'data': hoje().isoformat()})
    assert r.status_code == 302
    assert VendaB2B.query.get(v.id).status_entrega == 'entregue'
    assert b'Bruno' not in cliente.get('/padeiro/?data=' + hoje().isoformat()).data
