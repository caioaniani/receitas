"""Painel: "ENTREGUE" recusado não pode sumir em silêncio (dono, 14/10/2026).

"Ver o porquê quando marcam na TV como entregue ele volta os status": a
sonda /api/claude/pedidos-site-vencidos mostrou 18 pedidos pagos vencidos
com o card do painel em "pronto" e nenhuma saída de produção registrada —
o clique ENTREGUE nunca gravou. O servidor recusa (409) quando a saída do
kit sob encomenda não pode ser registrada; o front só logava no console e
a pendência otimista desfazia o card em 30 s. Corrida Lalamove COMPLETED
do mesmo kit (CE639E7C) virava 500 a cada reentrega.
"""
from datetime import date
from decimal import Decimal
from unittest.mock import patch

from test_lalamove_encerramento import _corrida, _webhook, cfg  # noqa: F401
from test_padeiro_kit_entregue import _cliente, _kit, _pedido

from app.extensions import db
from app.models import (
    AppConfig,
    PainelPedidoStatus,
    PedidoOnline,
    ProdutoItem,
    SaidaProducaoSite,
)


def _kit_bloqueado():
    kit = _kit()
    # Componente sem vínculo: a saída da produção recusa (ValueError → 409).
    db.session.add(ProdutoItem(produto_id=kit.id, tipo='mp', item_nome='Bisnaga sumida',
                               quantidade=1))
    db.session.commit()
    return kit


def test_painel_recusa_kit_bloqueado_com_motivo_e_nada_muda(app, admin_user):
    p = _pedido(_kit_bloqueado(), codigo='KITBLQ01')
    with patch('app.services.email.disponivel', return_value=False):
        r = _cliente(app, admin_user).post(
            f'/entregas/api/painel/status/{p.codigo}', json={'status': 'entregue'})
    d = r.get_json()
    assert r.status_code == 409 and d['ok'] is False and d['bloqueado'] is True
    assert 'vínculo' in d['erro']
    db.session.refresh(p)
    assert p.status == 'pago'
    assert PainelPedidoStatus.query.filter_by(pedido_code=p.codigo).first() is None
    assert SaidaProducaoSite.query.count() == 0


def test_front_mostra_o_motivo_e_desfaz_na_hora(app, admin_user):
    html = _cliente(app, admin_user).get('/entregas/painel-testes?embed=1').get_data(as_text=True)
    # Corpo JSON (falha de CSRF volta JSON e é re-tentada com token novo) e
    # recusa = alerta com o motivo + recarga imediata — nunca só console.
    assert "JSON.stringify({ status: status })" in html
    assert 'function falhouStatus' in html and 'NÃO foi marcado como' in html
    assert "d.erro !== 'csrf_expirada'" in html


def test_lalamove_completed_com_saida_recusada_nao_vira_500(cfg):  # noqa: F811
    p = _pedido(_kit_bloqueado(), codigo='KITLAL01')
    p.status = 'a_caminho'
    db.session.commit()
    _corrida(p.codigo, 'ORD-KIT-1')
    with patch('app.services.email.disponivel', return_value=False):
        r = _webhook(cfg.test_client(), 'ORD-KIT-1', 'COMPLETED')
    assert r.status_code == 200
    db.session.refresh(p)
    assert p.status == 'a_caminho'          # fica para a equipe conferir


def _online(codigo, status, data_entrega):
    p = PedidoOnline(codigo=codigo, nome_cliente='C', email_cliente='c@x.test',
                     modo_entrega='agendada', status=status, data_entrega=data_entrega,
                     subtotal=Decimal('10'), frete_valor=Decimal('0'),
                     valor_total=Decimal('10'))
    db.session.add(p)
    return p


def test_seed_marca_so_a_lista_vencida_sem_email_nem_estoque(app):
    from app.migrations_legacy import _seed_entregues_vencidos_2026_10_14
    _online('CE639E7C', 'a_caminho', date(2026, 10, 12))
    _online('93F53A48', 'pago', date(2026, 10, 11))
    _online('B47A8691', 'cancelado', date(2026, 9, 18))     # pulado
    _online('C64E9373', 'pago', date(2026, 10, 20))         # data posterior: pulado
    _online('FORA0001', 'pago', date(2026, 10, 1))          # fora da lista
    db.session.add(PainelPedidoStatus(pedido_code='93F53A48', status='pronto'))
    db.session.commit()
    with patch('app.services.email.enviar_pedido_entregue') as mail:
        _seed_entregues_vencidos_2026_10_14(app)
    mail.assert_not_called()
    st = {p.codigo: p.status for p in PedidoOnline.query.all()}
    assert st == {'CE639E7C': 'entregue', '93F53A48': 'entregue',
                  'B47A8691': 'cancelado', 'C64E9373': 'pago', 'FORA0001': 'pago'}
    painel = {s.pedido_code: s.status for s in PainelPedidoStatus.query.all()}
    assert painel == {'CE639E7C': 'entregue', '93F53A48': 'entregue'}
    assert SaidaProducaoSite.query.count() == 0
    marker = AppConfig.get('seed_entregues_vencidos_2026_10_14')
    assert 'n=2' in marker and 'B47A8691:cancelado' in marker
    # Uma vez só.
    PedidoOnline.query.filter_by(codigo='93F53A48').one().status = 'pago'
    db.session.commit()
    _seed_entregues_vencidos_2026_10_14(app)
    assert PedidoOnline.query.filter_by(codigo='93F53A48').one().status == 'pago'


def test_seed_saida_baixa_kits_entregues_pelo_motor_uma_vez(app):
    """Kits do 12/10 conciliados como entregues SEM baixa: o acerto registra a
    saída pelo motor do painel (composição atual), só pros pedidos entregues
    da lista, uma vez."""
    from app.migrations_legacy import _seed_saida_entregues_vencidos_2026_10_14
    from app.models import EstoqueProducao, MovEstoqueProducao, Receita
    from tests.test_padeiro_kit_entregue import _kit, _pedido
    kit = _kit()
    croissant = Receita.query.filter_by(nome='Croissant Tradicional').one()
    db.session.add(EstoqueProducao(receita_id=croissant.id, quantidade=20))
    p = _pedido(kit, codigo='CE639E7C')
    p.status = 'entregue'
    _pedido(kit, codigo='93F53A48')                    # ainda pago: pulado
    db.session.commit()
    _seed_saida_entregues_vencidos_2026_10_14(app)
    assert EstoqueProducao.query.filter_by(receita_id=croissant.id).one().quantidade == 19
    assert SaidaProducaoSite.query.filter_by(pedido_id=p.id).count() == 1
    assert MovEstoqueProducao.query.filter_by(tipo='saida_site_direto').count() == 1
    marker = AppConfig.get('seed_saida_entregues_vencidos_2026_10_14')
    assert 'CE639E7C:itens=1' in marker and '93F53A48:pulado_pago' in marker
    # Uma vez só (marker) e idempotente por item mesmo sem ele.
    _seed_saida_entregues_vencidos_2026_10_14(app)
    AppConfig.query.filter_by(key='seed_saida_entregues_vencidos_2026_10_14').delete()
    db.session.commit()
    _seed_saida_entregues_vencidos_2026_10_14(app)
    assert EstoqueProducao.query.filter_by(receita_id=croissant.id).one().quantidade == 19
    assert 'CE639E7C:nada' in AppConfig.get('seed_saida_entregues_vencidos_2026_10_14')
