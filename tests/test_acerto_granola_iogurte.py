"""Seed one-shot do acerto granola/iogurte (dono 18/08/2026).

Corrige pedidos históricos lançados em POTES/LITROS (mapas em
migrations_legacy), o fator das cestas "Granola 50g"/"Cesta dia das mães
2026" e o lote/mínimo do iogurte (padrão 3000). Só aplica quando o valor
atual ainda é o lançado — edição do dono manda.
"""
from datetime import timedelta

from app.extensions import db
from app.migrations_legacy import (
    ACERTO_CESTAS_GRANOLA,
    ACERTO_GRANOLA_PEDIDOS,
    ACERTO_IOGURTE_PEDIDOS,
    _seed_acerto_granola_iogurte,
)
from app.models import (
    AppConfig,
    Loja,
    PedidoItem,
    PedidoLoja,
    Produto,
    ProdutoItem,
    Receita,
)
from app.utils import hoje

MARKER = 'acerto_granola_iogurte_2026_08'


def _cenario():
    loja = Loja(nome='Loja Teste', ativa=True)
    granola = Receita(nome='Produção - Granola Artesanal 1000g',
                      categoria='Granola', rendimento_qtd=15300,
                      rendimento_unidade='', peso_base=4000.0,
                      peso_unitario=1.0, lote_pedido=5000,
                      minimo_pedido=5000)
    iogurte = Receita(nome='Produção - Iogurte Caseiro 1000ml',
                      categoria='Iogurte', rendimento_qtd=1170,
                      rendimento_unidade='ml', peso_base=1170.0,
                      peso_unitario=1.0, minimo_pedido=5000)
    db.session.add_all([loja, granola, iogurte])
    db.session.commit()
    return loja, granola, iogurte


def _pedido(pid, loja, receita, qtd, recebida=None):
    p = PedidoLoja(id=pid, loja_id=loja.id, status='entregue',
                   data_entrega=hoje() - timedelta(days=10),
                   data_pedido=hoje() - timedelta(days=11))
    db.session.add(p)
    db.session.flush()
    it = PedidoItem(pedido_id=p.id, receita_id=receita.id, quantidade=qtd,
                    quantidade_recebida=recebida)
    db.session.add(it)
    db.session.commit()
    return it


def test_corrige_quantidade_e_recebida(app):
    with app.app_context():
        loja, granola, iogurte = _cenario()
        # granola pedido 210: 5 -> 5000 (mapa real)
        it_g = _pedido(210, loja, granola, 5, recebida=5)
        # iogurte pedido 322: 3 -> 3000
        it_i = _pedido(322, loja, iogurte, 3, recebida=3)
        _seed_acerto_granola_iogurte(app)
        db.session.refresh(it_g)
        db.session.refresh(it_i)
        assert it_g.quantidade == 5000 and it_g.quantidade_recebida == 5000
        assert it_i.quantidade == 3000 and it_i.quantidade_recebida == 3000
        marker = AppConfig.get(MARKER)
        assert 'corrigidos=2' in marker


def test_valor_editado_pelo_dono_nao_e_tocado(app):
    with app.app_context():
        loja, granola, iogurte = _cenario()
        # o mapa espera 5; o dono já corrigiu pra 5000 na mão
        it = _pedido(210, loja, granola, 5000, recebida=5000)
        # e um caso onde ele pôs OUTRO valor qualquer
        it2 = _pedido(106, loja, granola, 123, recebida=123)
        _seed_acerto_granola_iogurte(app)
        db.session.refresh(it)
        db.session.refresh(it2)
        assert it.quantidade == 5000
        assert it2.quantidade == 123          # mapa esperava 7 — mantém
        assert 'mantidos=2' in AppConfig.get(MARKER)


def test_pedido_fora_do_mapa_fica_intacto(app):
    with app.app_context():
        loja, granola, _ = _cenario()
        it = _pedido(9999, loja, granola, 5)
        _seed_acerto_granola_iogurte(app)
        db.session.refresh(it)
        assert it.quantidade == 5


def test_cestas_ganham_fator_certo(app):
    with app.app_context():
        _, granola, _ = _cenario()
        p1 = Produto(nome='Granola 50g', ativo=True)
        p2 = Produto(nome='Cesta dia das mães 2026', ativo=True)
        p3 = Produto(nome='Granola Artesanal 100g', ativo=True)
        db.session.add_all([p1, p2, p3])
        db.session.flush()
        db.session.add_all([
            ProdutoItem(produto_id=p1.id, tipo='receita',
                        receita_id=granola.id, item_nome=granola.nome,
                        quantidade=0.05),
            ProdutoItem(produto_id=p2.id, tipo='receita',
                        receita_id=granola.id, item_nome=granola.nome,
                        quantidade=0.1),
            # essa está certa (100g) — não pode mudar
            ProdutoItem(produto_id=p3.id, tipo='receita',
                        receita_id=granola.id, item_nome=granola.nome,
                        quantidade=100.0),
        ])
        db.session.commit()
        _seed_acerto_granola_iogurte(app)
        vals = {pi.produto.nome: float(pi.quantidade)
                for pi in ProdutoItem.query.all()}
        assert vals['Granola 50g'] == 50.0
        assert vals['Cesta dia das mães 2026'] == 100.0
        assert vals['Granola Artesanal 100g'] == 100.0
        assert 'cestas=2' in AppConfig.get(MARKER)


def test_iogurte_ganha_lote_3000(app):
    with app.app_context():
        _, _, iogurte = _cenario()
        _seed_acerto_granola_iogurte(app)
        db.session.refresh(iogurte)
        assert iogurte.lote_pedido == 3000
        assert iogurte.minimo_pedido == 3000
        assert 'lote_iogurte=1' in AppConfig.get(MARKER)


def test_lote_ja_definido_pelo_dono_nao_muda(app):
    with app.app_context():
        _, _, iogurte = _cenario()
        iogurte.lote_pedido = 2000
        db.session.commit()
        _seed_acerto_granola_iogurte(app)
        db.session.refresh(iogurte)
        assert iogurte.lote_pedido == 2000
        assert iogurte.minimo_pedido == 5000
        assert 'lote_iogurte=0' in AppConfig.get(MARKER)


def test_marker_impede_segunda_rodada(app):
    with app.app_context():
        loja, granola, _ = _cenario()
        it = _pedido(210, loja, granola, 5)
        _seed_acerto_granola_iogurte(app)
        db.session.refresh(it)
        assert it.quantidade == 5000
        # o dono volta o valor na mão; o seed NUNCA re-aplica
        it.quantidade = 5
        db.session.commit()
        _seed_acerto_granola_iogurte(app)
        db.session.refresh(it)
        assert it.quantidade == 5


def test_mapas_sem_typos_obvios(app):
    """Todo par (old, new) do mapa é coerente: new é old*1000 OU um valor
    de observação explícita (documentado no comentário do mapa)."""
    excecoes = {203: 3000, 229: 3000, 238: 3000, 311: 3000,
                489: 9360, 496: 18000, 530: 4000}
    for mapa in (ACERTO_GRANOLA_PEDIDOS, ACERTO_IOGURTE_PEDIDOS):
        for pid, (old, new) in mapa.items():
            if mapa is ACERTO_IOGURTE_PEDIDOS and pid in excecoes:
                assert new == excecoes[pid]
            else:
                assert new == old * 1000, (pid, old, new)
    assert [c[1] < c[2] for c in ACERTO_CESTAS_GRANOLA] == [True, True]
