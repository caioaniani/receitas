"""Teste de desempacotamento de cesta em saida em lote (estoque loja).

Bug descoberto na conferencia da Anesio: quando a loja vendia uma cesta
('Family Box'), o sistema tentava subtrair do EstoqueLoja(produto_id=cesta),
mas a loja so tem os componentes em estoque, nao a cesta. Resultado:
nada era descontado, e o estoque acumulava em relacao ao fisico.

Fix: aplicar_saida_lote agora desempacota cesta em componentes e baixa
cada componente individualmente.
"""
import pytest
from app import create_app
from app.extensions import db
from app.models import (Loja, Receita, Produto, ProdutoItem,
                          EstoqueLoja, LojaProdutoMap, Usuario)
from app.services.estoque_loja_lote import aplicar_saida_lote


@pytest.fixture
def app():
    app = create_app()
    app.config['TESTING'] = True
    app.config['WTF_CSRF_ENABLED'] = False
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
    with app.app_context():
        db.create_all()
        yield app


def test_saida_lote_desempacota_cesta(app):
    """Cesta com 5 pao + 3 croissant → vender 1 cesta baixa 5 pao + 3 croissant."""
    with app.app_context():
        # Setup: cria loja, usuario, receitas (componentes), produto cesta
        # Nomes unicos pra nao conflitar com seed da padaria
        loja = Loja(nome='Loja Test Cesta XYZ', ativa=True)
        usr = Usuario(nome='Admin Cesta', login='adm_cesta_test', papel='admin', is_owner=True)
        usr.set_senha('x')
        pao = Receita(nome='Pao Tradicional', categoria='Paes', preco_venda=5.0)
        croi = Receita(nome='Croissant', categoria='Viennoiserie', preco_venda=8.0)
        db.session.add_all([loja, usr, pao, croi])
        db.session.flush()

        cesta = Produto(nome='Family Box', categoria='Cestas', ativo=True,
                         preco_atacado=50.0, preco_loja=60.0)
        db.session.add(cesta)
        db.session.flush()

        # ProdutoItens (cesta tem 5 pao + 3 croissant)
        db.session.add(ProdutoItem(produto_id=cesta.id, tipo='receita',
                                     item_nome='Pao Tradicional', quantidade=5))
        db.session.add(ProdutoItem(produto_id=cesta.id, tipo='receita',
                                     item_nome='Croissant', quantidade=3))
        db.session.flush()

        # Estoque inicial: 20 paes, 10 croissants, 0 Family Box
        db.session.add(EstoqueLoja(loja_id=loja.id, receita_id=pao.id,
                                     quantidade=20))
        db.session.add(EstoqueLoja(loja_id=loja.id, receita_id=croi.id,
                                     quantidade=10))
        db.session.flush()

        # Mapping: "Family Box" → produto_id=cesta.id (confirmado)
        mp = LojaProdutoMap(
            nome_digitado='Family Box',
            produto_id=cesta.id,
            confirmado_em=db.func.now(),
            confirmado_por=usr.id,
            fator_quantidade=1.0,
        )
        db.session.add(mp)
        db.session.commit()

        # Simula resolucao do parser
        itens_resolvidos = [{
            'linha': 'Family Box: 2',
            'nome': 'Family Box',
            'quantidade': 2,
            'map_entry': mp,
        }]

        # Aplica saida
        resultado = aplicar_saida_lote(itens_resolvidos, loja.id, usr,
                                         referencia='Teste')

        # Verificacoes
        assert len(resultado['aplicados']) == 1, \
            f'esperava 1 aplicado, veio {resultado["aplicados"]}'
        assert len(resultado['ignorados']) == 0, \
            f'nao esperava ignorados: {resultado["ignorados"]}'

        # Pao: 20 - (2 cestas × 5 paes) = 10
        ep_pao = EstoqueLoja.query.filter_by(loja_id=loja.id, receita_id=pao.id).first()
        assert ep_pao.quantidade == 10, \
            f'pao esperado 10, ficou {ep_pao.quantidade}'

        # Croissant: 10 - (2 × 3) = 4
        ep_croi = EstoqueLoja.query.filter_by(loja_id=loja.id, receita_id=croi.id).first()
        assert ep_croi.quantidade == 4, \
            f'croissant esperado 4, ficou {ep_croi.quantidade}'

        # Cesta NAO foi criada em EstoqueLoja (nao deve aparecer)
        ep_cesta = EstoqueLoja.query.filter_by(loja_id=loja.id, produto_id=cesta.id).first()
        assert ep_cesta is None, 'nao devia criar EstoqueLoja pra cesta'


def test_saida_lote_produto_normal_continua_funcionando(app):
    """Produto sem componentes (nao-cesta) continua descontando normalmente."""
    with app.app_context():
        loja = Loja(nome='Loja2', ativa=True)
        usr = Usuario(nome='Admin', login='adm2', papel='admin', is_owner=True)
        usr.set_senha('x')
        # Produto sem itens (nao eh cesta)
        sabao = Produto(nome='Sabao em Pedra', categoria='Limpeza',
                          ativo=True, preco_atacado=3.0, preco_loja=5.0)
        db.session.add_all([loja, usr, sabao])
        db.session.flush()
        db.session.add(EstoqueLoja(loja_id=loja.id, produto_id=sabao.id, quantidade=10))
        db.session.flush()
        mp = LojaProdutoMap(
            nome_digitado='Sabao',
            produto_id=sabao.id,
            confirmado_em=db.func.now(),
            confirmado_por=usr.id,
            fator_quantidade=1.0,
        )
        db.session.add(mp)
        db.session.commit()

        itens = [{'linha': 'Sabao: 3', 'nome': 'Sabao', 'quantidade': 3, 'map_entry': mp}]
        resultado = aplicar_saida_lote(itens, loja.id, usr, referencia='Teste')

        assert len(resultado['aplicados']) == 1
        ep = EstoqueLoja.query.filter_by(loja_id=loja.id, produto_id=sabao.id).first()
        assert ep.quantidade == 7, f'esperado 7, veio {ep.quantidade}'
