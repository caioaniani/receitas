"""Exclusão de entregador — fix do bug de prod (2026-06-09): clicar excluir,
confirmar OK e nada acontecer. Causa: FKs (principalmente DriverMagicToken,
NOT NULL) travavam o delete no Postgres → 500 que o front engolia."""
from datetime import timedelta


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


def test_remover_driver_com_magic_token_exclui_e_limpa(app):
    """Driver com link mágico diário deve ser excluído E o token limpo (antes
    o token órfão travava a FK em prod)."""
    from app.extensions import db
    from app.models import Driver, DriverMagicToken
    from app.utils import agora
    c = _admin_logado(app)
    d = Driver(nome='Joao', ativo=True)
    db.session.add(d)
    db.session.commit()
    db.session.add(DriverMagicToken(driver_id=d.id, token='tk-abc',
                                    expira_em=agora() + timedelta(days=1)))
    db.session.commit()
    did = d.id

    r = c.delete('/entregas/api/drivers/%d' % did)
    assert r.status_code == 200
    assert r.get_json()['acao'] == 'excluido'
    assert Driver.query.get(did) is None
    # o token foi removido (sem isso, ficava órfão e travava a FK no Postgres)
    assert DriverMagicToken.query.filter_by(driver_id=did).count() == 0


def test_remover_driver_com_historico_desativa_depois_force_exclui(app):
    """Com histórico de entregas: 1o DELETE desativa; ?force=1 exclui de vez."""
    from app.extensions import db
    from app.models import AtribuicaoEntrega, Driver
    c = _admin_logado(app)
    d = Driver(nome='Maria', ativo=True)
    db.session.add(d)
    db.session.commit()
    db.session.add(AtribuicaoEntrega(pedido_code='P1', driver_id=d.id))
    db.session.commit()
    did = d.id

    r1 = c.delete('/entregas/api/drivers/%d' % did)
    assert r1.get_json()['acao'] == 'desativado'
    assert Driver.query.get(did).ativo is False

    r2 = c.delete('/entregas/api/drivers/%d?force=1' % did)
    assert r2.get_json()['acao'] == 'excluido_com_historico'
    assert Driver.query.get(did) is None
    assert AtribuicaoEntrega.query.filter_by(driver_id=did).count() == 0


def test_remover_driver_zera_ref_de_pedido_loja(app):
    """PedidoLoja.driver_id (handshake) é nullable → deve ser zerado, não travar."""
    from app.extensions import db
    from app.models import Driver, Loja, PedidoLoja
    c = _admin_logado(app)
    loja = Loja(nome='Centro', ativa=True)
    d = Driver(nome='Carlos', ativo=True)
    db.session.add_all([loja, d])
    db.session.commit()
    pl = PedidoLoja(loja_id=loja.id, driver_id=d.id)
    db.session.add(pl)
    db.session.commit()
    did, plid = d.id, pl.id

    r = c.delete('/entregas/api/drivers/%d' % did)
    assert r.get_json()['acao'] == 'excluido'
    assert Driver.query.get(did) is None
    # pedido preservado, só perdeu a atribuição ao motorista
    assert PedidoLoja.query.get(plid).driver_id is None
