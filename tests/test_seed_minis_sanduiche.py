"""Seed dos 6 minis sanduíches do cardápio em PDF (dono 19/08/2026):
Receitas + slots no menu configurável Caixa de Mini (produto 148, preços
do PDF) + SKU do Tiny herdado do Mini Croissant Tradicional (canal site,
confirmado — NF-e automática, ordem explícita do dono)."""
from decimal import Decimal

from app.extensions import db
from app.migrations_legacy import MINIS_SANDUICHE_SEED, _seed_minis_sanduiche
from app.models import AppConfig, Produto, ProdutoItem, Receita, TinyProdutoMap
from app.utils import agora


def _cenario(com_menu=True):
    ref = Receita(nome='Mini Croissant Tradicional', categoria='Minis',
                  rendimento_qtd=1, rendimento_unidade='un', peso_base=80.0,
                  estado_padrao='assado', sob_encomenda=True)
    db.session.add(ref)
    db.session.flush()
    db.session.add(TinyProdutoMap(canal='site', kind='receita',
                                  item_id=ref.id, tiny_sku='MINI-CROI-01',
                                  confirmado_em=agora()))
    menu = None
    if com_menu:
        menu = Produto(id=MINIS_SANDUICHE_SEED['menu_produto_id'],
                       nome='Caixa de Mini', menu_configuravel=True,
                       menu_total_unidades=30, preco_site=300)
        db.session.add(menu)
        db.session.flush()
        db.session.add(ProdutoItem(produto_id=menu.id, tipo='receita',
                                   receita_id=ref.id, item_nome=ref.nome,
                                   quantidade=5,
                                   preco_menu=Decimal('10.00')))
    db.session.commit()
    return ref, menu


def test_seed_cria_minis_slots_fotos_e_sku(app):
    with app.app_context():
        ref, menu = _cenario()
        _seed_minis_sanduiche(app)
        precos = {n: Decimal(p) for n, _d, p, _a in
                  MINIS_SANDUICHE_SEED['itens']}
        for nome, preco in precos.items():
            r = Receita.query.filter_by(nome=nome).first()
            assert r is not None, nome
            assert r.observacao                    # descrição do PDF
            assert r.imagem_blob                   # foto do PDF importada
            assert r.sob_encomenda is True         # herdado da referência
            pi = ProdutoItem.query.filter_by(produto_id=menu.id,
                                             receita_id=r.id).first()
            assert pi is not None and pi.preco_menu == preco
            assert int(pi.quantidade) == 0         # pré-seleção não muda
            m = TinyProdutoMap.query.filter_by(canal='site', kind='receita',
                                               item_id=r.id).first()
            assert m is not None and m.tiny_sku == 'MINI-CROI-01'
            assert m.confirmado_em is not None     # confirmado (fiscal exige)
        marker = AppConfig.get(MINIS_SANDUICHE_SEED['chave'])
        assert 'criadas=6' in marker and 'slots=6' in marker
        assert 'skus=6' in marker and 'fotos=6' in marker


def test_seed_respeita_homonima_e_roda_uma_vez(app):
    with app.app_context():
        _cenario()
        existente = Receita(nome='Brioche Caprese', categoria='Minis',
                            rendimento_qtd=1, rendimento_unidade='un',
                            peso_base=90.0)
        db.session.add(existente)
        db.session.commit()
        _seed_minis_sanduiche(app)
        marker = AppConfig.get(MINIS_SANDUICHE_SEED['chave'])
        assert 'criadas=5' in marker and 'mantidas=1' in marker
        # a homônima do dono não foi tocada, mas ganhou o slot no menu
        db.session.refresh(existente)
        assert existente.peso_base == 90.0
        pi = ProdutoItem.query.filter_by(
            produto_id=MINIS_SANDUICHE_SEED['menu_produto_id'],
            receita_id=existente.id).first()
        assert pi is not None and pi.preco_menu == Decimal('19.00')
        # 2ª rodada é no-op (dono pode editar preço sem o seed voltar)
        pi.preco_menu = Decimal('21.00')
        db.session.commit()
        _seed_minis_sanduiche(app)
        db.session.refresh(pi)
        assert pi.preco_menu == Decimal('21.00')


def test_seed_sem_menu_nao_marca_e_retenta(app):
    with app.app_context():
        _cenario(com_menu=False)
        _seed_minis_sanduiche(app)
        assert AppConfig.get(MINIS_SANDUICHE_SEED['chave']) is None
        assert Receita.query.filter_by(nome='Posta de Lagarto').first() is None
        # menu chega depois (deploy seguinte) → o seed aplica
        menu = Produto(id=MINIS_SANDUICHE_SEED['menu_produto_id'],
                       nome='Caixa de Mini', menu_configuravel=True,
                       menu_total_unidades=30, preco_site=300)
        db.session.add(menu)
        db.session.commit()
        _seed_minis_sanduiche(app)
        assert AppConfig.get(MINIS_SANDUICHE_SEED['chave']) is not None
        assert Receita.query.filter_by(nome='Posta de Lagarto').first()
