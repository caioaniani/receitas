"""Ficha tecnica: ordem dos ingredientes (drag-and-drop persiste pela ordem
do formulario — o salvar apaga e recria na ordem recebida) e modo de preparo
em etapas (1 modulo por etapa, juntadas com linha em branco). Tambem o modal
de exclusao com vinculos resolviveis na propria janela."""


def _login(c):
    c.post('/auth/login', data={'login': 'admin', 'senha': '123'})


def _receita(app, nome='Sourdough', **kw):
    from app.extensions import db
    from app.models import Receita
    with app.app_context():
        r = Receita(nome=nome, categoria='Paes', rendimento_qtd=10,
                    rendimento_unidade='un', peso_base=1000.0, **kw)
        db.session.add(r)
        db.session.commit()
        return r.id


def _form_base(nome='Sourdough'):
    return {'nome': nome, 'rendimento_qtd': '10',
            'rendimento_unidade': 'un', 'peso_base': '1000'}


def test_dividir_etapas_preparo():
    from app.utils import dividir_etapas_preparo
    assert dividir_etapas_preparo('Misturar.\n\nAssar.') == ['Misturar.', 'Assar.']
    # CRLF de textarea e linhas em branco com espacos
    assert dividir_etapas_preparo('A\r\n\r\nB\n   \nC') == ['A', 'B', 'C']
    # texto corrido = 1 etapa unica
    assert dividir_etapas_preparo('Passo 1: tudo junto\nPasso 2: na mesma') == \
        ['Passo 1: tudo junto\nPasso 2: na mesma']
    assert dividir_etapas_preparo(None) == []


def test_ordem_ingredientes_segue_o_formulario(app, admin_user):
    from app.models import Receita
    rid = _receita(app)
    c = app.test_client()
    _login(c)
    data = _form_base()
    data['ingrediente_tipo[]'] = ['mp', 'mp']
    data['ingrediente_nome[]'] = ['Farinha', 'Agua']
    data['porcentagem[]'] = ['100', '70']
    data['eh_base[]'] = ['1', '0']
    data['nota[]'] = ['', '']
    assert c.post(f'/receitas/{rid}/salvar', data=data).status_code == 302
    with app.app_context():
        nomes = [i.ingrediente_nome for i in Receita.query.get(rid).ingredientes]
        assert nomes == ['Farinha', 'Agua']

    # Reordenado no form (drag-and-drop) -> persiste invertido
    data['ingrediente_nome[]'] = ['Agua', 'Farinha']
    data['porcentagem[]'] = ['70', '100']
    data['eh_base[]'] = ['0', '1']
    assert c.post(f'/receitas/{rid}/salvar', data=data).status_code == 302
    with app.app_context():
        nomes = [i.ingrediente_nome for i in Receita.query.get(rid).ingredientes]
        assert nomes == ['Agua', 'Farinha']


def test_modo_preparo_em_etapas_junta_e_renderiza(app, admin_user):
    from app.models import Receita
    rid = _receita(app)
    c = app.test_client()
    _login(c)
    data = _form_base()
    data['tem_etapas'] = '1'
    # etapas vazias sao descartadas; CRLF normalizado
    data['modo_preparo_etapa[]'] = ['Misturar tudo.', '', '  ', 'Assar 30min.\r\nVirar na metade.']
    assert c.post(f'/receitas/{rid}/salvar', data=data).status_code == 302
    with app.app_context():
        mp = Receita.query.get(rid).modo_preparo
        assert mp == 'Misturar tudo.\n\nAssar 30min.\nVirar na metade.'

    r = c.get(f'/receitas/{rid}')
    assert r.status_code == 200
    # 2 etapas viram 2 modulos (+1 do <template> de nova etapa)
    assert r.data.count(b'name="modo_preparo_etapa[]"') == 3
    assert 'Misturar tudo.'.encode() in r.data


def test_form_sem_tem_etapas_segue_legado(app, admin_user):
    """O salvar em lote (/receitas/modos-preparo) e clients antigos mandam
    `modo_preparo` inteiro — continua funcionando."""
    from app.models import Receita
    rid = _receita(app)
    c = app.test_client()
    _login(c)
    data = _form_base()
    data['modo_preparo'] = 'Texto corrido legado.'
    assert c.post(f'/receitas/{rid}/salvar', data=data).status_code == 302
    with app.app_context():
        assert Receita.query.get(rid).modo_preparo == 'Texto corrido legado.'


def test_vinculos_lista_resolve_e_libera_exclusao(app, admin_user):
    from app.extensions import db
    from app.models import (
        Loja,
        PedidoItem,
        PedidoLoja,
        Produto,
        ProdutoItem,
        Receita,
        SeruProdutoMap,
    )
    rid = _receita(app, nome='Croissant Teste')
    with app.app_context():
        cesta = Produto(nome='Cesta X', categoria='cestas')
        loja = Loja(nome='Centro', ativa=True)
        db.session.add_all([cesta, loja])
        db.session.flush()
        db.session.add(ProdutoItem(produto_id=cesta.id, tipo='receita',
                                   receita_id=rid, item_nome='Croissant Teste',
                                   quantidade=2))
        p = PedidoLoja(loja_id=loja.id, status='pendente')
        db.session.add(p)
        db.session.flush()
        db.session.add(PedidoItem(pedido_id=p.id, receita_id=rid, quantidade=5))
        db.session.add(SeruProdutoMap(seru_nome='CROISSANT', receita_id=rid))
        db.session.commit()
        pedido_item_id = PedidoItem.query.first().id

    c = app.test_client()
    _login(c)
    r = c.get(f'/receitas/{rid}/vinculos')
    data = r.get_json()
    assert data['pode_excluir'] is False
    chaves = {g['chave']: g for g in data['grupos']}
    assert chaves['cestas']['resolvivel'] is True
    assert chaves['cestas']['itens'][0]['label'] == 'Cesta X'
    assert chaves['mapeamentos']['resolvivel'] is True
    assert chaves['hist_pedido_item']['resolvivel'] is False

    # grupo historico nao tem resolucao automatica
    r400 = c.post(f'/receitas/{rid}/vinculos/resolver',
                  data={'chave': 'hist_pedido_item'})
    assert r400.status_code == 400

    # resolve cestas e mapeamentos
    d2 = c.post(f'/receitas/{rid}/vinculos/resolver',
                data={'chave': 'cestas'}).get_json()
    assert 'cestas' not in {g['chave'] for g in d2['grupos']}
    d3 = c.post(f'/receitas/{rid}/vinculos/resolver',
                data={'chave': 'mapeamentos'}).get_json()
    assert 'mapeamentos' not in {g['chave'] for g in d3['grupos']}
    with app.app_context():
        m = SeruProdutoMap.query.filter_by(seru_nome='CROISSANT').first()
        assert m is not None and m.receita_id is None   # voltou pra pendente
        assert ProdutoItem.query.count() == 0

    # ainda bloqueado pelo historico de pedido
    assert d3['pode_excluir'] is False

    # some o historico -> liberado -> exclui de verdade
    with app.app_context():
        db.session.delete(db.session.get(PedidoItem, pedido_item_id))
        db.session.commit()
    d4 = c.get(f'/receitas/{rid}/vinculos').get_json()
    assert d4['pode_excluir'] is True
    c.post(f'/receitas/{rid}/excluir', follow_redirects=True)
    with app.app_context():
        assert db.session.get(Receita, rid) is None


def test_vinculo_ingrediente_em_outra_ficha(app, admin_user):
    """Uso por NOME (tipo='receita') nao e FK mas quebraria o custo das
    outras fichas — bloqueia e resolve removendo das fichas."""
    from app.extensions import db
    from app.models import Receita, ReceitaIngrediente
    rid = _receita(app, nome='Molho pesto')
    rid_mae = _receita(app, nome='Salada organica')
    with app.app_context():
        db.session.add(ReceitaIngrediente(
            receita_id=rid_mae, tipo='receita',
            ingrediente_nome='Molho pesto', porcentagem=0.02))
        db.session.commit()

    c = app.test_client()
    _login(c)
    data = c.get(f'/receitas/{rid}/vinculos').get_json()
    grupo = {g['chave']: g for g in data['grupos']}['ingrediente_em_fichas']
    assert grupo['itens'][0]['label'] == 'Salada organica'

    d2 = c.post(f'/receitas/{rid}/vinculos/resolver',
                data={'chave': 'ingrediente_em_fichas'}).get_json()
    assert d2['pode_excluir'] is True
    with app.app_context():
        assert ReceitaIngrediente.query.filter_by(receita_id=rid_mae).count() == 0
        assert db.session.get(Receita, rid_mae) is not None  # a ficha-mae fica
