"""Varredura 19/07/2026 — item arquivado/inativo NUNCA entra em fluxo ativo.

Caso gatilho: "Pão de queijo un" (Receita arquivada em 01/07) apareceu no
/cardapio?tipo=atacado. A varredura achou a mesma classe de furo em ~20
pontos (pickers, matchers, resolvers do copilot, relatórios). Contrato:
`Receita.ativas()` / `Produto.ativo=True` / `MateriaPrima.ativas()` em tudo
que CONECTA algo novo; histórico continua lendo cru.
"""
from app.extensions import db
from app.utils import agora


def _receita(nome, arquivada=False, preco=10.0):
    from app.models import Receita
    r = Receita(nome=nome, categoria='Paes', rendimento_qtd=1,
                rendimento_unidade='un', peso_base=100.0, preco_venda=preco,
                arquivada_em=agora() if arquivada else None)
    db.session.add(r)
    db.session.commit()
    return r


def _produto(nome, ativo=True):
    from app.models import Produto
    p = Produto(nome=nome, categoria='Cestas', ativo=ativo,
                preco_atacado=50.0)
    db.session.add(p)
    db.session.commit()
    return p


def _login(app, user):
    c = app.test_client()
    c.post('/auth/login', data={'login': user.login, 'senha': '123'})
    return c


def test_typeahead_pedido_loja_nao_lista_arquivada(app, admin_user):
    with app.app_context():
        _receita('Pao Vivo TA')
        _receita('Pao Morto TA', arquivada=True)
    c = _login(app, admin_user)
    d = c.get('/pedidos/buscar-itens.json?q=pao').get_json()
    nomes = [i['nome'] for i in d['itens']]
    assert 'Pao Vivo TA' in nomes
    assert 'Pao Morto TA' not in nomes


def test_catalogo_venda_b2b_filtra_mas_grandfather_no_editar(app):
    from app.blueprints.b2b.routes import _catalogo_venda
    from app.models import ClienteB2B, VendaB2B, VendaB2BItem
    with app.app_context():
        viva = _receita('Pao Vivo B2B')
        morta = _receita('Pao Morto B2B', arquivada=True)
        cli = ClienteB2B(nome='Cliente X', ativo=True)
        db.session.add(cli)
        db.session.flush()
        venda = VendaB2B(cliente_id=cli.id, valor_total=10)
        db.session.add(venda)
        db.session.flush()
        db.session.add(VendaB2BItem(venda_id=venda.id, receita_id=morta.id,
                                    quantidade=1, preco_unitario=10))
        db.session.commit()
        # venda NOVA: arquivada fora
        nomes = [r.nome for r in _catalogo_venda()['receitas']]
        assert 'Pao Vivo B2B' in nomes and 'Pao Morto B2B' not in nomes
        # EDITAR a venda que já tem a arquivada: grandfather mantém
        nomes2 = [r.nome
                  for r in _catalogo_venda(excluir_venda_id=venda.id)['receitas']]
        assert 'Pao Morto B2B' in nomes2


def test_copilot_nao_resolve_produto_inativo(app):
    from app.services import copilot
    with app.app_context():
        _produto('Cesta Viva CP')
        _produto('Cesta Morta CP', ativo=False)
        vivos = copilot._resolver_produto('Cesta Viva CP')
        assert vivos and vivos[0]['nome'] == 'Cesta Viva CP'
        assert copilot._resolver_produto('Cesta Morta CP') == []
        # _resolver_item_qualquer idem
        assert copilot._resolver_item_qualquer('Cesta Morta CP') is None


def test_matcher_estoque_lote_nao_casa_arquivada(app):
    from app.services import estoque_congelados, estoque_loja_lote
    with app.app_context():
        _receita('Croissant Vivo ML')
        _receita('Croissant Morto ML', arquivada=True)
        cat_loja = estoque_loja_lote._carregar_catalogo(None)
        nomes_loja = [n for _, n, _ in cat_loja['receitas']] \
            if isinstance(cat_loja, dict) else [n for _, n, _ in cat_loja[0]]
        assert 'Croissant Vivo ML' in nomes_loja
        assert 'Croissant Morto ML' not in nomes_loja
        cat_cong = estoque_congelados._carregar_catalogo()
        nomes_cong = [n for _, n, _ in cat_cong['receitas']] \
            if isinstance(cat_cong, dict) else [n for _, n, _ in cat_cong[0]]
        assert 'Croissant Vivo ML' in nomes_cong
        assert 'Croissant Morto ML' not in nomes_cong


def test_aprovacao_orcamento_recusa_item_arquivado(app):
    """Item vinculado enquanto ativo + receita arquivada DEPOIS: aprovar
    (que cria VendaB2B na hora) tem que recusar com erro claro."""
    from app.models import ClienteB2B, Orcamento, OrcamentoItem
    from app.services import orcamentos
    from app.utils import hoje
    with app.app_context():
        r = _receita('Pao Orc AR')
        cli = ClienteB2B(nome='Cliente Orc', ativo=True)
        db.session.add(cli)
        db.session.flush()
        orc = Orcamento(cliente_id=cli.id, data_entrega=hoje())
        db.session.add(orc)
        db.session.flush()
        db.session.add(OrcamentoItem(orcamento_id=orc.id, receita_id=r.id,
                                     nome=r.nome, quantidade=2,
                                     preco_unitario=10))
        db.session.commit()
        assert orcamentos.validar_para_aprovacao(orc) == []
        r.arquivada_em = agora()
        db.session.commit()
        erros = orcamentos.validar_para_aprovacao(orc)
        assert any('arquivado' in e for e in erros)


def test_dashboard_e_relatorio_custos_sem_arquivadas(app, admin_user):
    with app.app_context():
        _receita('Pao Vivo REL', preco=100.0)
        _receita('Pao Morto REL', arquivada=True, preco=999.0)
    c = _login(app, admin_user)
    body = c.get('/relatorios/custos').get_data(as_text=True)
    assert 'Pao Vivo REL' in body and 'Pao Morto REL' not in body
    body2 = c.get('/rentabilidade').get_data(as_text=True)
    assert 'Pao Vivo REL' in body2 and 'Pao Morto REL' not in body2
