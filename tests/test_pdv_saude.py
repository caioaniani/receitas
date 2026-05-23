"""Testes do painel de saude + reconciliacao do sync PDV."""
from datetime import date, datetime
from unittest.mock import patch


def test_resumo_banco_vazio(app):
    from app.services import pdv_saude
    with app.app_context():
        r = pdv_saude.resumo()
    assert r['lojas_pendentes'] == 0
    assert r['produtos_pendentes_seru'] == 0
    assert r['produtos_pendentes_vnda'] == 0
    assert r['pedidos_sem_loja'] == 0
    assert r['total_pendencias'] == 0
    # Sem run nesta sessao → atrasado.
    assert r['seru_atrasado'] is True
    assert r['vnda_atrasado'] is True


def test_resumo_conta_loja_pendente(app):
    from app.extensions import db
    from app.models import SeruLojaMap
    from app.services import pdv_saude
    with app.app_context():
        # Loja sem confirmado_em e nao ignorada → pendente (nao baixa).
        db.session.add(SeruLojaMap(seru_company_name='Loja X', loja_id=None,
                                   ignorar=False, confirmado_em=None))
        # Loja confirmada → nao conta.
        from app.utils import agora
        db.session.add(SeruLojaMap(seru_company_name='Loja Y', loja_id=1,
                                   ignorar=False, confirmado_em=agora()))
        # Loja ignorada → nao conta.
        db.session.add(SeruLojaMap(seru_company_name='Loja Z', ignorar=True))
        db.session.commit()
        r = pdv_saude.resumo()
    assert r['lojas_pendentes'] == 1


def test_resumo_conta_produto_pendente(app):
    from app.extensions import db
    from app.models import SeruProdutoMap
    from app.services import pdv_saude
    with app.app_context():
        # Pendente: sem receita/produto, nao ignorado.
        db.session.add(SeruProdutoMap(seru_nome='Cafe Pendente'))
        # Mapeado: nao conta.
        db.session.add(SeruProdutoMap(seru_nome='Pao Mapeado', receita_id=1))
        # Ignorado: nao conta.
        db.session.add(SeruProdutoMap(seru_nome='Agua', ignorar=True))
        db.session.commit()
        r = pdv_saude.resumo()
    assert r['produtos_pendentes_seru'] == 1


def test_contar_pendencias_soma(app):
    from app.extensions import db
    from app.models import SeruLojaMap, SeruPedidoProcessado, SeruProdutoMap
    from app.services import pdv_saude
    with app.app_context():
        db.session.add(SeruLojaMap(seru_company_name='L', confirmado_em=None, ignorar=False))
        db.session.add(SeruProdutoMap(seru_nome='P'))
        db.session.add(SeruPedidoProcessado(seru_pedido_id='ped1', loja_id=None))
        db.session.commit()
        assert pdv_saude.contar_pendencias() == 3


def test_reconciliar_separa_pendentes_vendidos(app):
    from app.extensions import db
    from app.models import EstoqueLoja, MovEstoqueLoja
    from app.services import pdv_saude

    agg_fake = {
        'total_pedidos': 10,
        'total_itens_vendidos': 50.0,
        'faturamento_total': 500.0,
        'produtos': [
            {'nome': 'Pao', 'sku': '1', 'qtd': 30, 'faturamento': 300.0,
             'estado_map': 'mapeado'},
            {'nome': 'Bolo Novo', 'sku': '2', 'qtd': 20, 'faturamento': 200.0,
             'estado_map': 'pendente'},  # vendido mas nao baixa
        ],
    }
    with app.app_context():
        el = EstoqueLoja(loja_id=1, receita_id=1, quantidade=0)
        db.session.add(el)
        db.session.flush()
        # Data dentro do periodo reconciliado (2026-05-05).
        db.session.add(MovEstoqueLoja(estoque_loja_id=el.id, tipo='venda_seru',
                                      quantidade=30, data=datetime(2026, 5, 5, 10, 0)))
        db.session.commit()
        with patch('app.services.vendas_itens.agregar_itens', return_value=agg_fake):
            r = pdv_saude.reconciliar(date(2026, 5, 1), date(2026, 5, 7))

    assert 'erro' not in r
    assert len(r['pendentes_vendidos']) == 1
    assert r['pendentes_vendidos'][0]['nome'] == 'Bolo Novo'
    assert r['qtd_pendente'] == 20
    assert r['baixado_efetivo'] == 30


def test_reconciliar_api_falha_retorna_erro(app):
    from app.services import pdv_saude
    with app.app_context():
        with patch('app.services.vendas_itens.agregar_itens',
                   side_effect=RuntimeError('seru fora')):
            r = pdv_saude.reconciliar(date(2026, 5, 1), date(2026, 5, 7))
    assert 'erro' in r
    assert 'seru fora' in r['erro']


def test_rota_saude_renderiza(app, admin_user):
    """Smoke: /pdv/saude renderiza 200 (template + url_for OK)."""
    c = app.test_client()
    c.post('/auth/login', data={'login': 'admin', 'senha': '123'})
    r = c.get('/pdv/saude')
    assert r.status_code == 200
    assert b'Saude do Sync' in r.data


def test_rota_reconciliacao_renderiza(app, admin_user, catalogo):
    """Smoke: /pdv/reconciliacao renderiza 200 com pendente + select de alvos."""
    c = app.test_client()
    c.post('/auth/login', data={'login': 'admin', 'senha': '123'})
    agg = {
        'total_pedidos': 5, 'total_itens_vendidos': 5, 'faturamento_total': 50.0,
        'produtos': [
            {'nome': 'Cesta Nova', 'sku': '99', 'qtd': 5, 'faturamento': 50.0,
             'estado_map': 'sem_map'},
            {'nome': 'Pao Mapeado', 'sku': '1', 'qtd': 80, 'faturamento': 400.0,
             'estado_map': 'mapeado', 'fator': 1.0,
             'mapeado_para': {'tipo': 'produto', 'id': 1, 'nome': 'Pao Frances'}},
        ],
    }
    vnda_agg = {'produtos': [], 'total_pedidos': 0, 'total_itens': 0, 'loja': 'Anesio'}
    with patch('app.services.vendas_itens.agregar_itens', return_value=agg), \
         patch('app.services.vnda_sync.agregar_vendas', return_value=vnda_agg):
        r = c.get('/pdv/reconciliacao')
    assert r.status_code == 200
    assert b'Reconciliacao' in r.data
    assert b'Cesta Nova' in r.data        # pendente (nao baixou) aparece
    assert b'reconMapear' in r.data       # JS da acao inline
    assert b'recon-alvo' in r.data        # select de alvos renderizou
    assert b'tab-vnda' in r.data          # aba VNDA renderizou


def test_reconciliar_vnda_separa_pendentes(app):
    from app.extensions import db
    from app.models import EstoqueLoja, MovEstoqueLoja
    from app.services import pdv_saude

    vnda_agg = {
        'produtos': [
            {'nome': 'Box Site', 'sku': 'v1', 'qtd': 10, 'estado_map': 'sem_map',
             'fator': 1.0, 'mapeado_para': None},
            {'nome': 'Pao Site', 'sku': 'v2', 'qtd': 40, 'estado_map': 'mapeado',
             'fator': 1.0, 'mapeado_para': {'tipo': 'produto', 'id': 1, 'nome': 'Pao'}},
        ],
        'total_pedidos': 8, 'total_itens': 50, 'loja': 'Anesio',
    }
    with app.app_context():
        el = EstoqueLoja(loja_id=1, produto_id=1, quantidade=0)
        db.session.add(el)
        db.session.flush()
        db.session.add(MovEstoqueLoja(estoque_loja_id=el.id, tipo='venda_vnda',
                                      quantidade=40, data=datetime(2026, 5, 5, 9, 0)))
        db.session.commit()
        with patch('app.services.vnda_sync.agregar_vendas', return_value=vnda_agg):
            r = pdv_saude.reconciliar_vnda(date(2026, 5, 1), date(2026, 5, 7))

    assert 'erro' not in r
    assert len(r['pendentes_vendidos']) == 1
    assert r['pendentes_vendidos'][0]['nome'] == 'Box Site'
    assert r['baixado_efetivo'] == 40


def test_dashboard_renderiza_com_card_pdv(app, admin_user):
    """Smoke: /dashboard renderiza com pdv_pendencias sem quebrar."""
    c = app.test_client()
    c.post('/auth/login', data={'login': 'admin', 'senha': '123'})
    r = c.get('/dashboard')
    assert r.status_code == 200
