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
        # Pedido/venda NOVOS (_resolver_produto): a morta NUNCA aparece
        # (o fuzzy pode sugerir a viva parecida — ok).
        res = copilot._resolver_produto('Cesta Morta CP')
        assert all(m['nome'] != 'Cesta Morta CP' for m in res)
        # EXCEÇÃO DELIBERADA (pós-revisão 19/07/2026): desperdício/devolução/
        # retirada operam sobre estoque FÍSICO — produto soft-deletado com
        # saldo precisa continuar escoável, então _resolver_item_qualquer
        # ENXERGA o inativo.
        alvo = copilot._resolver_item_qualquer('Cesta Morta CP')
        assert alvo is not None and alvo[2] == 'Cesta Morta CP'


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
        orc = Orcamento(codigo='ORC-AR-1', cliente_id=cli.id,
                        data_entrega=hoje())
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


def test_salvar_cesta_preserva_fk_de_componente_arquivado(app, admin_user):
    """Pós-revisão 19/07/2026: editar OUTRA linha da cesta não pode orfanar
    em silêncio o componente cuja receita foi arquivada DEPOIS de vinculada
    (a baixa de venda dele pararia). A linha existente reusa a FK antiga;
    órfão de verdade só em linha NOVA."""
    from app.models import Produto, ProdutoItem
    with app.app_context():
        r = _receita('Componente GF')
        cesta = Produto(nome='Cesta GF', categoria='Cestas', ativo=True)
        db.session.add(cesta)
        db.session.flush()
        db.session.add(ProdutoItem(produto_id=cesta.id, tipo='receita',
                                   item_nome=r.nome, receita_id=r.id,
                                   quantidade=2))
        db.session.commit()
        r.arquivada_em = agora()
        db.session.commit()
        cesta_id, r_id = cesta.id, r.id
    c = _login(app, admin_user)
    resp = c.post(f'/produtos/{cesta_id}/salvar', data={
        'nome': 'Cesta GF', 'categoria': 'Cestas',
        'item_tipo[]': ['receita'], 'item_nome[]': ['Componente GF'],
        'quantidade[]': ['3'],
    })
    assert resp.status_code in (302, 200)
    with app.app_context():
        item = ProdutoItem.query.filter_by(produto_id=cesta_id).one()
        assert item.receita_id == r_id       # FK preservada (grandfather)
        assert item.quantidade == 3


def _login_owner(app, owner_user):
    c = app.test_client()
    c.post('/auth/login', data={'login': owner_user.login, 'senha': '123'})
    return c


def test_arquivadas_saldo_dry_run_e_executar(app, owner_user):
    """/admin/arquivadas-saldo (owner): dry-run lista sem tocar; ?executar=1
    zera com movimento 'ajuste' rastreável. Linha com reserva de site é
    pulada; item VIVO nunca entra."""
    from app.models import EstoqueLoja, Loja, MovEstoqueLoja
    with app.app_context():
        loja = Loja(nome='Loja Saldo', ativa=True)
        db.session.add(loja)
        db.session.flush()
        morta = _receita('Pao Saldo Morto', arquivada=True)
        viva = _receita('Pao Saldo Vivo')
        reservada = _receita('Pao Saldo Reservado', arquivada=True)
        db.session.add_all([
            EstoqueLoja(loja_id=loja.id, receita_id=morta.id, quantidade=444),
            EstoqueLoja(loja_id=loja.id, receita_id=viva.id, quantidade=10),
            EstoqueLoja(loja_id=loja.id, receita_id=reservada.id,
                        quantidade=7, quantidade_reservada=2),
        ])
        db.session.commit()
        morta_id, viva_id, res_id = morta.id, viva.id, reservada.id
        loja_id = loja.id
    c = _login_owner(app, owner_user)
    # DRY-RUN: lista a morta, pula a reservada, ignora a viva; nada muda.
    d = c.get('/admin/arquivadas-saldo').get_json()
    assert d['dry_run'] is True
    itens = [li['item'] for li in d['linhas']]
    assert 'Pao Saldo Morto' in itens and 'Pao Saldo Vivo' not in itens
    assert any('Reservado' in p['item'] for p in d['pulados_com_reserva'])
    with app.app_context():
        el = EstoqueLoja.query.filter_by(loja_id=loja_id,
                                         receita_id=morta_id).one()
        assert el.quantidade == 444          # dry-run não tocou
    # EXECUTAR: zera + movimento com a quantidade exata.
    d2 = c.get('/admin/arquivadas-saldo?executar=1').get_json()
    assert d2['dry_run'] is False and d2['zerados'] >= 1
    with app.app_context():
        el = EstoqueLoja.query.filter_by(loja_id=loja_id,
                                         receita_id=morta_id).one()
        assert el.quantidade == 0
        mov = MovEstoqueLoja.query.filter_by(
            estoque_loja_id=el.id, tipo='ajuste').one()
        assert mov.quantidade == 444
        assert 'arquivad' in (mov.referencia or '')
        # viva intocada; reservada intocada (pulada)
        assert EstoqueLoja.query.filter_by(
            loja_id=loja_id, receita_id=viva_id).one().quantidade == 10
        assert EstoqueLoja.query.filter_by(
            loja_id=loja_id, receita_id=res_id).one().quantidade == 7


def test_arquivadas_saldo_exige_owner(app, admin_user):
    c = _login(app, admin_user)
    resp = c.get('/admin/arquivadas-saldo')
    assert resp.status_code == 403
