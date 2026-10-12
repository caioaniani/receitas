"""Kit sob encomenda sai da TV do padeiro assim que a entrega é registrada.

Caso de 12/10/2026 ("o padeiro ligou dizendo que está cheio de pedido na
tela dele"): os cartões eram de Kits Dia das Crianças ainda `pago`. Estes
testes travam, com a composição REAL do kit (`kit_dia_criancas.criar`), que
cada caminho de registro de entrega/saída tira o cartão da fila "Para
separar" e do pré-preparo — e que o kit ainda não entregue continua lá.
"""
from decimal import Decimal
from unittest.mock import patch

import pytest

from app.extensions import db
from app.models import (
    AtribuicaoEntrega,
    Driver,
    Loja,
    PedidoOnline,
    PedidoOnlineItem,
    Produto,
    Receita,
    SaidaProducaoSite,
)
from app.services import kit_dia_criancas, loja_entrega
from app.utils import hoje


def _kit():
    db.session.add_all([
        Receita(nome='Croissant Tradicional', categoria='Viennoiserie', rendimento_qtd=1,
                rendimento_unidade='unidades', peso_base=1000),
        Receita(nome='Brioche', categoria='Pães', rendimento_qtd=1,
                rendimento_unidade='unidades', peso_base=1000),
        Produto(nome='Salada de Frutas 100g', categoria='Acompanhamentos', ativo=True),
    ])
    db.session.commit()
    kit = db.session.get(Produto, kit_dia_criancas.criar(subir_foto=False)['kit_id'])
    kit.sob_encomenda = True
    db.session.commit()
    return kit


def _pedido(kit, *, codigo='KITTV001', modo='agendada', loja=None):
    p = PedidoOnline(codigo=codigo, nome_cliente='Cliente Kit',
                     email_cliente='kit@example.test', modo_entrega=modo,
                     loja_retirada_id=(loja.id if loja else None),
                     status='pago', data_entrega=hoje(),
                     subtotal=Decimal('120'), frete_valor=Decimal('0'),
                     valor_total=Decimal('120'))
    db.session.add(p)
    db.session.flush()
    db.session.add(PedidoOnlineItem(
        pedido_id=p.id, kind='produto', produto_id=kit.id, nome=kit.nome,
        preco_unitario=Decimal('120'), quantidade=1, subtotal=Decimal('120')))
    db.session.commit()
    return p


def _cards_online():
    from app.blueprints.padeiro.routes import _dados_listas
    return [c for c in _dados_listas(hoje(), True)['a_separar'] if c['tipo'] == 'online']


def _cliente(app, usuario):
    c = app.test_client()
    with c.session_transaction() as s:
        s['_user_id'] = str(usuario.id)
        s['_fresh'] = True
    return c


def test_kit_pago_nao_entregue_continua_na_tv(app):
    """Sem registro de entrega/saída o cartão fica — é o lembrete de produção."""
    p = _pedido(_kit())
    cards = _cards_online()
    assert [c['codigo'] for c in cards] == [p.codigo]
    # O cartão abre o kit nos componentes a produzir.
    nomes = [i['nome'] for i in cards[0]['itens']]
    assert 'Kit Dia das Crianças' in nomes and '· Brioche do Mickey' in nomes


@pytest.mark.parametrize('origem', ['motorista', 'painel_admin', 'lalamove_completed'])
def test_entrega_registrada_tira_kit_da_tv(app, origem):
    """Motorista, baixa do admin e Lalamove COMPLETED passam pelo mesmo
    `avancar_status_entrega` (driver/routes.py, entregas/routes.py,
    lalamove/routes.py) — o pedido vira entregue e sai da fila."""
    p = _pedido(_kit())
    assert _cards_online()
    with patch('app.services.email.disponivel', return_value=False):
        loja_entrega.avancar_status_entrega(p.codigo, 'entregue')
    db.session.refresh(p)
    assert p.status == 'entregue'
    assert SaidaProducaoSite.query.filter_by(pedido_id=p.id).count() == 1
    assert _cards_online() == []


def test_retirada_marcada_entregue_no_painel_sai_da_tv(app, admin_user):
    """Retirada não tem motorista: o gesto é o 📦 ENTREGUE do painel de
    entregas, que sincroniza o PedidoOnline e tira o cartão da TV."""
    loja = Loja(nome='Loja Retirada', ativa=True, endereco='Rua X, 1')
    db.session.add(loja)
    db.session.commit()
    p = _pedido(_kit(), codigo='KITRET01', modo='retirada', loja=loja)
    assert _cards_online()
    with patch('app.services.email.disponivel', return_value=False):
        r = _cliente(app, admin_user).post(
            f'/entregas/api/painel/status/{p.codigo}', json={'status': 'entregue'})
    assert r.status_code == 200, r.get_json()
    db.session.refresh(p)
    assert p.status == 'entregue'
    assert _cards_online() == []


def test_inicio_da_rota_tira_kit_da_tv_mesmo_ainda_pago(app):
    """Saída confirmada no início da rota: o status comercial segue `pago`
    até a entrega, mas o cartão do padeiro já sai (o kit saiu da indústria)."""
    from app.services.rastreio_entrega import iniciar_rota
    p = _pedido(_kit())
    d = Driver(nome='Motorista Kit', token='kittv', pin=None)
    db.session.add(d)
    db.session.flush()
    db.session.add(AtribuicaoEntrega(driver_id=d.id, pedido_code=p.codigo,
                                    data_entrega=hoje(), ordem=1))
    db.session.commit()
    with patch('app.services.rastreio_entrega._enviar_emails_saida', return_value=0):
        iniciar_rota(d)
    db.session.commit()
    db.session.refresh(p)
    assert p.status == 'pago'
    assert _cards_online() == []
