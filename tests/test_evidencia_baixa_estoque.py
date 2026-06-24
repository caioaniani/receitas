"""EVIDENCIA: prova que a venda baixa o estoque da loja corretamente.

Cada teste simula 1 pedido Seru real (mock da API, sem rede) e verifica o
efeito no banco: EstoqueLoja decrementado + MovEstoqueLoja registrado.

Cobre os pontos que o dono questionou:
- a venda baixa o estoque (caminho feliz);
- NAO baixa em dobro (idempotencia) — mesmo rodando o sync 2x / catch-up;
- loja nao confirmada / produto nao mapeado NAO baixam (salvaguardas);
- estoque nunca fica negativo;
- cancelamento estorna (devolve ao estoque).
"""
from datetime import date
from unittest.mock import patch

PEDIDO_DIA = date(2026, 5, 20)
CREATED_AT = '2026-05-20T13:00:00Z'  # 13h UTC = 10h BRT, mesmo dia


def _pedido(pid, company, itens, canceled_at=None):
    return {
        'id': pid,
        'createdAt': CREATED_AT,
        'canceledAt': canceled_at,
        'company': {'name': company},
        'items': [{'name': n, 'quantity': q} for n, q in itens],
    }


def _setup(*, qtd_estoque=10, confirmar_loja=True, mapear_produto=True):
    """Cria loja+mapa+receita+estoque. Retorna (loja, receita, estoque_loja)."""
    from app.extensions import db
    from app.models import EstoqueLoja, Loja, Receita, SeruLojaMap, SeruProdutoMap
    from app.utils import agora

    loja = Loja(nome='Ribeiro do Vale', ativa=True)
    db.session.add(loja)
    db.session.flush()

    db.session.add(SeruLojaMap(
        seru_company_name='Ribeiro do Vale',
        loja_id=loja.id,
        confirmado_em=agora() if confirmar_loja else None,
    ))

    receita = Receita(nome='Pao Frances', categoria='Paes', rendimento_qtd=1,
                      rendimento_unidade='un', peso_base=100.0)
    db.session.add(receita)
    db.session.flush()

    if mapear_produto:
        db.session.add(SeruProdutoMap(
            seru_nome='PAO FRANCES', receita_id=receita.id,
            confirmado_em=agora(), fator_quantidade=1.0))

    el = EstoqueLoja(loja_id=loja.id, receita_id=receita.id,
                     quantidade=qtd_estoque, estado=None)
    db.session.add(el)
    db.session.commit()
    return loja, receita, el


def _sync(pedidos):
    """Roda processar_pedidos com a API Seru mockada."""
    from app.services import seru_sync
    with patch('app.services.seru.listar_pedidos_completo', return_value=pedidos):
        return seru_sync.processar_pedidos(PEDIDO_DIA, PEDIDO_DIA, user=None)


def test_venda_baixa_estoque(app):
    """Caminho feliz: vende 3, estoque 10 → 7, MovEstoqueLoja registra a baixa."""
    from app.models import EstoqueLoja, MovEstoqueLoja
    with app.app_context():
        loja, receita, el = _setup(qtd_estoque=10)
        eid = el.id

        stats = _sync([_pedido('P1', 'Ribeiro do Vale', [('PAO FRANCES', 3)])])

        assert stats['itens_baixados'] == 1
        assert EstoqueLoja.query.get(eid).quantidade == 7  # 10 - 3
        movs = MovEstoqueLoja.query.filter_by(estoque_loja_id=eid,
                                              tipo='venda_seru').all()
        assert len(movs) == 1
        assert movs[0].quantidade == 3
        assert 'Seru #P1' in movs[0].referencia


def test_nao_baixa_em_dobro(app):
    """EVIDENCIA-CHAVE: rodar o sync 2x com o MESMO pedido baixa so 1x."""
    from app.models import EstoqueLoja, MovEstoqueLoja
    with app.app_context():
        loja, receita, el = _setup(qtd_estoque=10)
        eid = el.id
        pedido = [_pedido('P1', 'Ribeiro do Vale', [('PAO FRANCES', 3)])]

        _sync(pedido)
        assert EstoqueLoja.query.get(eid).quantidade == 7
        # Segunda passada (sync de novo / catch-up reprocessando o dia)
        stats2 = _sync(pedido)

        assert stats2['pedidos_ja_processados'] == 1
        assert stats2['itens_baixados'] == 0
        assert EstoqueLoja.query.get(eid).quantidade == 7  # NAO virou 4
        # So 1 movimento, nao 2
        assert MovEstoqueLoja.query.filter_by(estoque_loja_id=eid,
                                              tipo='venda_seru').count() == 1


def test_catchup_3x_nao_duplica(app):
    """Catch-up roda o sync varias vezes/dia — idempotencia segura tudo."""
    from app.models import EstoqueLoja
    with app.app_context():
        loja, receita, el = _setup(qtd_estoque=20)
        eid = el.id
        pedido = [_pedido('P9', 'Ribeiro do Vale', [('PAO FRANCES', 5)])]

        for _ in range(3):
            _sync(pedido)

        assert EstoqueLoja.query.get(eid).quantidade == 15  # 20 - 5, uma vez so


def test_loja_nao_confirmada_nao_baixa(app):
    """Salvaguarda: loja com mapa nao confirmado NAO baixa (retenta depois)."""
    from app.models import EstoqueLoja
    with app.app_context():
        loja, receita, el = _setup(qtd_estoque=10, confirmar_loja=False)
        eid = el.id

        stats = _sync([_pedido('P2', 'Ribeiro do Vale', [('PAO FRANCES', 3)])])

        assert stats['pedidos_aguardando_loja'] == 1
        assert stats['itens_baixados'] == 0
        assert EstoqueLoja.query.get(eid).quantidade == 10  # intacto


def test_produto_nao_mapeado_nao_baixa(app):
    """Salvaguarda: produto pendente (sem mapa) NAO baixa, conta pra revisao."""
    from app.models import EstoqueLoja
    with app.app_context():
        loja, receita, el = _setup(qtd_estoque=10, mapear_produto=False)
        eid = el.id

        stats = _sync([_pedido('P3', 'Ribeiro do Vale', [('PAO FRANCES', 3)])])

        assert stats['itens_pendentes_novos'] == 1
        assert stats['itens_baixados'] == 0
        assert EstoqueLoja.query.get(eid).quantidade == 10  # intacto


def test_estoque_nunca_fica_negativo(app):
    """Vende 10 com so 3 em estoque → baixa 3, registra falta, NAO fica -7."""
    from app.models import EstoqueLoja, MovEstoqueLoja
    with app.app_context():
        loja, receita, el = _setup(qtd_estoque=3)
        eid = el.id

        stats = _sync([_pedido('P4', 'Ribeiro do Vale', [('PAO FRANCES', 10)])])

        assert EstoqueLoja.query.get(eid).quantidade == 0  # nao -7
        assert stats['itens_sem_estoque'] == 1
        falta = MovEstoqueLoja.query.filter_by(
            estoque_loja_id=eid, tipo='venda_seru_sem_estoque').all()
        assert len(falta) == 1
        assert falta[0].quantidade == 7  # registrou a falta pra auditoria


def test_cancelamento_estorna(app):
    """Pedido baixado e depois cancelado na Seru → estorno devolve ao estoque."""
    from app.models import EstoqueLoja, MovEstoqueLoja
    with app.app_context():
        loja, receita, el = _setup(qtd_estoque=10)
        eid = el.id

        _sync([_pedido('P5', 'Ribeiro do Vale', [('PAO FRANCES', 4)])])
        assert EstoqueLoja.query.get(eid).quantidade == 6  # 10 - 4

        # Mesmo pedido volta cancelado
        stats = _sync([_pedido('P5', 'Ribeiro do Vale', [('PAO FRANCES', 4)],
                               canceled_at='2026-05-20T15:00:00Z')])

        assert stats['pedidos_cancelados_estornados'] == 1
        assert EstoqueLoja.query.get(eid).quantidade == 10  # devolvido
        assert MovEstoqueLoja.query.filter_by(
            estoque_loja_id=eid, tipo='venda_seru_estorno').count() == 1
