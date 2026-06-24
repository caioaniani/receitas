"""Estoque por produto (estado vive só no pedido).

Cobre: consolidação de linhas duplicadas (helper), idempotência, recebimento de
pedido somando estados numa linha, baixa da linha única, e a rota de consolidação.
"""
import pytest


def _login(client, user):
    with client.session_transaction() as sess:
        sess['_user_id'] = str(user.id)
        sess['_fresh'] = True


def _receita(nome='Danish de Calabresa'):
    from app.models import Receita
    return Receita(nome=nome, categoria='Fornadas Especiais', rendimento_qtd=1,
                   rendimento_unidade='un', peso_base=100.0)


def test_obter_linha_loja_consolida_duplicatas(app, loja):
    from app.extensions import db
    from app.models import EstoqueLoja, MovEstoqueLoja
    from app.services.estoque_helpers import obter_linha_loja

    with app.app_context():
        r = _receita()
        db.session.add(r)
        db.session.commit()
        rid, lid = r.id, loja.id
        linhas = []
        for est, q in ((None, 1), ('backup', 3), ('assado', 1)):
            el = EstoqueLoja(loja_id=lid, receita_id=rid, estado=est, quantidade=q)
            db.session.add(el)
            linhas.append(el)
        db.session.commit()
        # movimento numa das extras pra checar preservação do histórico
        extra_id = linhas[1].id
        db.session.add(MovEstoqueLoja(estoque_loja_id=extra_id,
                                      tipo='entrada_pedido', quantidade=3,
                                      referencia='historico antigo'))
        db.session.commit()

        canonica = obter_linha_loja(lid, receita_id=rid)
        db.session.commit()

        restantes = EstoqueLoja.query.filter_by(loja_id=lid, receita_id=rid).all()
        assert len(restantes) == 1
        assert restantes[0].id == canonica.id
        assert canonica.quantidade == 5  # 1 + 3 + 1
        assert canonica.estado is None
        # histórico antigo reatribuído (não apagado pelo delete-orphan)
        movs = MovEstoqueLoja.query.filter_by(estoque_loja_id=canonica.id).all()
        tipos = [m.tipo for m in movs]
        assert 'entrada_pedido' in tipos      # o mov antigo migrou
        assert 'consolidacao_estado' in tipos  # auditoria da consolidação


def test_obter_linha_loja_idempotente(app, loja):
    from app.extensions import db
    from app.models import EstoqueLoja, MovEstoqueLoja
    from app.services.estoque_helpers import obter_linha_loja

    with app.app_context():
        r = _receita('Pão Francês')
        db.session.add(r)
        db.session.commit()
        db.session.add(EstoqueLoja(loja_id=loja.id, receita_id=r.id,
                                   estado=None, quantidade=7))
        db.session.commit()

        obter_linha_loja(loja.id, receita_id=r.id)
        db.session.commit()
        obter_linha_loja(loja.id, receita_id=r.id)
        db.session.commit()

        linhas = EstoqueLoja.query.filter_by(loja_id=loja.id, receita_id=r.id).all()
        assert len(linhas) == 1
        assert linhas[0].quantidade == 7
        # nenhuma consolidação espúria
        assert MovEstoqueLoja.query.filter_by(tipo='consolidacao_estado').count() == 0


def test_recebimento_soma_estados_numa_linha(app, loja, admin_user):
    from app.blueprints.pedidos.routes import _executar_recebimento_pedido
    from app.extensions import db
    from app.models import EstoqueLoja, PedidoItem, PedidoLoja

    with app.app_context():
        r = _receita('Croissant')
        db.session.add(r)
        db.session.commit()
        ped = PedidoLoja(loja_id=loja.id, status='em_transporte')
        db.session.add(ped)
        db.session.flush()
        db.session.add(PedidoItem(pedido_id=ped.id, receita_id=r.id,
                                  quantidade=5, estado=None))
        db.session.add(PedidoItem(pedido_id=ped.id, receita_id=r.id,
                                  quantidade=3, estado='backup'))
        db.session.commit()

        ok, _msg, _div = _executar_recebimento_pedido(ped, admin_user)
        assert ok is True

        linhas = EstoqueLoja.query.filter_by(loja_id=loja.id, receita_id=r.id).all()
        assert len(linhas) == 1          # 1 produto, sem separar por estado
        assert linhas[0].quantidade == 8  # 5 + 3
        assert linhas[0].estado is None


def test_baixar_loja_da_linha_unica(app, loja):
    from app.extensions import db
    from app.models import EstoqueLoja, MovEstoqueLoja
    from app.services.estoque_helpers import baixar_loja_por_prioridade

    with app.app_context():
        r = _receita('Pão de Queijo')
        db.session.add(r)
        db.session.commit()
        db.session.add(EstoqueLoja(loja_id=loja.id, receita_id=r.id,
                                   estado=None, quantidade=10))
        db.session.commit()

        filtro = {'loja_id': loja.id, 'receita_id': r.id}
        res = baixar_loja_por_prioridade(
            filtro, 4, tipo_mov='venda_seru', referencia='Seru #1',
            sem_estoque_tipo='venda_seru_sem_estoque', usuario_id=None)
        db.session.commit()
        assert res == {'baixado': 4, 'faltou': 0}
        el = EstoqueLoja.query.filter_by(loja_id=loja.id, receita_id=r.id).one()
        assert el.quantidade == 6

        res2 = baixar_loja_por_prioridade(
            filtro, 10, tipo_mov='venda_seru', referencia='Seru #2',
            sem_estoque_tipo='venda_seru_sem_estoque', usuario_id=None)
        db.session.commit()
        assert res2 == {'baixado': 6, 'faltou': 4}
        el = EstoqueLoja.query.filter_by(loja_id=loja.id, receita_id=r.id).one()
        assert el.quantidade == 0
        assert MovEstoqueLoja.query.filter_by(tipo='venda_seru_sem_estoque').count() == 1


def test_rota_consolidar(app, loja, admin_user):
    from app.extensions import db
    from app.models import EstoqueLoja

    with app.app_context():
        r = _receita()
        db.session.add(r)
        db.session.commit()
        rid, lid = r.id, loja.id
        for est, q in ((None, 1), ('backup', 3), ('assado', 1)):
            db.session.add(EstoqueLoja(loja_id=lid, receita_id=rid,
                                       estado=est, quantidade=q))
        db.session.commit()

    client = app.test_client()
    _login(client, admin_user)
    resp = client.post('/pedidos/estoque-loja/consolidar', follow_redirects=False)
    assert resp.status_code in (302, 303)

    with app.app_context():
        linhas = EstoqueLoja.query.filter_by(loja_id=lid, receita_id=rid).all()
        assert len(linhas) == 1
        assert linhas[0].quantidade == 5


def test_migracao_consolida_e_cria_trava(app, loja):
    """Fluxo da migracao auto-suficiente: consolida duplicatas legadas e cria a
    trava de unicidade, que passa a bloquear nova duplicata do mesmo produto."""
    import sqlalchemy

    from app.extensions import db
    from app.migrations_legacy import _migrate_estoque_trava
    from app.models import EstoqueLoja

    with app.app_context():
        r = _receita('Croissant Amêndoa')
        db.session.add(r)
        db.session.commit()
        rid, lid = r.id, loja.id
        for est, q in ((None, 2), ('backup', 4)):  # duplicata legada por estado
            db.session.add(EstoqueLoja(loja_id=lid, receita_id=rid,
                                       estado=est, quantidade=q))
        db.session.commit()

        _migrate_estoque_trava(app)  # consolida + cria a trava

        linhas = EstoqueLoja.query.filter_by(loja_id=lid, receita_id=rid).all()
        assert len(linhas) == 1
        assert linhas[0].quantidade == 6  # 2 + 4

        # Trava ativa: 2a linha do mesmo produto deve falhar no commit.
        db.session.add(EstoqueLoja(loja_id=lid, receita_id=rid,
                                   estado=None, quantidade=1))
        with pytest.raises(sqlalchemy.exc.IntegrityError):
            db.session.commit()
        db.session.rollback()
