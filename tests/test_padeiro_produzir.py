"""Painel Produzir da TV do padeiro: typeahead de receita + entrada no congelado
(EstoqueProducao) da industria, sempre como cru (estado=None). O mesmo helper
`entrada_producao` alimenta a rota /congelados/entrada existente."""
import pytest


@pytest.fixture
def cliente(app):
    return app.test_client()


def _login(cliente, login='admin', senha='123'):
    return cliente.post('/auth/login', data={'login': login, 'senha': senha})


# ── Helper entrada_producao ──────────────────────────────────────────────

def test_helper_cria_linha_e_movimento(app, admin_user, catalogo):
    from app.extensions import db
    from app.models import EstoqueProducao, MovEstoqueProducao
    from app.services.estoque_congelados import entrada_producao
    rid = catalogo['receita'].id
    ep = entrada_producao(receita_id=rid, quantidade=5, usuario_id=admin_user.id)
    db.session.commit()
    assert ep.receita_id == rid and ep.estado is None and ep.quantidade == 5
    movs = MovEstoqueProducao.query.filter_by(estoque_producao_id=ep.id).all()
    assert len(movs) == 1
    assert movs[0].tipo == 'producao' and movs[0].quantidade == 5
    assert EstoqueProducao.query.filter_by(receita_id=rid).count() == 1


def test_helper_incrementa_linha_cru(app, admin_user, catalogo):
    from app.extensions import db
    from app.models import EstoqueProducao, MovEstoqueProducao
    from app.services.estoque_congelados import entrada_producao
    rid = catalogo['receita'].id
    db.session.add(EstoqueProducao(receita_id=rid, estado=None, quantidade=5))
    db.session.commit()
    entrada_producao(receita_id=rid, quantidade=3, usuario_id=admin_user.id)
    db.session.commit()
    ep = EstoqueProducao.query.filter_by(receita_id=rid, estado=None).one()
    assert ep.quantidade == 8
    assert MovEstoqueProducao.query.filter_by(
        estoque_producao_id=ep.id, quantidade=3).count() == 1


def test_helper_desambigua_estado_backup(app, admin_user, catalogo):
    """Guarda do bug: entrada cru NAO pode somar na linha backup."""
    from app.extensions import db
    from app.models import EstoqueProducao
    from app.services.estoque_congelados import entrada_producao
    rid = catalogo['receita'].id
    db.session.add(EstoqueProducao(receita_id=rid, estado='backup', quantidade=100))
    db.session.commit()
    entrada_producao(receita_id=rid, quantidade=4, usuario_id=admin_user.id)
    db.session.commit()
    backup = EstoqueProducao.query.filter_by(receita_id=rid, estado='backup').one()
    cru = EstoqueProducao.query.filter_by(receita_id=rid, estado=None).one()
    assert backup.quantidade == 100  # intacta
    assert cru.quantidade == 4


def test_helper_rejeita_qtd_invalida(app, admin_user, catalogo):
    from app.services.estoque_congelados import entrada_producao
    rid = catalogo['receita'].id
    with pytest.raises(ValueError):
        entrada_producao(receita_id=rid, quantidade=0, usuario_id=admin_user.id)
    with pytest.raises(ValueError):
        entrada_producao(receita_id=rid, quantidade=-1, usuario_id=admin_user.id)


def test_helper_exige_exatamente_um_alvo(app, admin_user, catalogo):
    from app.services.estoque_congelados import entrada_producao
    with pytest.raises(ValueError):
        entrada_producao(quantidade=1, usuario_id=admin_user.id)
    with pytest.raises(ValueError):
        entrada_producao(receita_id=catalogo['receita'].id,
                         produto_id=catalogo['produto'].id,
                         quantidade=1, usuario_id=admin_user.id)


# ── Busca de receitas (typeahead) ────────────────────────────────────────

def test_buscar_substring_e_case_insensitive(app, admin_user, catalogo, cliente):
    _login(cliente)

    def nomes(q):
        return [r['nome'] for r in
                cliente.get('/padeiro/buscar-receitas.json?q=' + q).get_json()['itens']]
    assert 'Croissant Tradicional' in nomes('crois')
    assert 'Croissant Tradicional' in nomes('trad')      # meio da palavra
    assert 'Croissant Tradicional' in nomes('CROISSANT')  # case-insensitive


def test_buscar_query_curta_e_sem_match(app, admin_user, catalogo, cliente):
    _login(cliente)
    assert cliente.get('/padeiro/buscar-receitas.json?q=c').get_json()['itens'] == []
    assert cliente.get('/padeiro/buscar-receitas.json?q=zzz').get_json()['itens'] == []


def test_buscar_inclui_produtos(app, admin_user, catalogo, cliente):
    """A busca do Produzir tambem traz produtos/cestas (ref produto:<id>)."""
    _login(cliente)
    refs = [r['ref'] for r in
            cliente.get('/padeiro/buscar-receitas.json?q=frances').get_json()['itens']]
    assert ('produto:%d' % catalogo['produto'].id) in refs


# ── POST /padeiro/produzir ───────────────────────────────────────────────

def test_produzir_um_item(app, admin_user, catalogo, cliente):
    from app.models import EstoqueProducao, MovEstoqueProducao
    _login(cliente)
    rid = catalogo['receita'].id
    r = cliente.post('/padeiro/produzir',
                     json={'itens': [{'ref': 'receita:%d' % rid, 'quantidade': 7}]})
    assert r.status_code == 200
    j = r.get_json()
    assert j['ok'] is True
    assert j['resumo'] == [{'nome': 'Croissant Tradicional', 'qtd': 7}]
    ep = EstoqueProducao.query.filter_by(receita_id=rid, estado=None).one()
    assert ep.quantidade == 7
    assert MovEstoqueProducao.query.filter_by(
        estoque_producao_id=ep.id, tipo='producao').count() == 1


def test_produzir_produto_cesta(app, admin_user, catalogo, cliente):
    """Produzir tambem aceita produto/cesta -> entrada em EstoqueProducao."""
    from app.models import EstoqueProducao
    _login(cliente)
    pid = catalogo['produto'].id
    r = cliente.post('/padeiro/produzir',
                     json={'itens': [{'ref': 'produto:%d' % pid, 'quantidade': 4}]})
    assert r.status_code == 200 and r.get_json()['ok'] is True
    ep = EstoqueProducao.query.filter_by(produto_id=pid, estado=None).one()
    assert ep.quantidade == 4


def test_produzir_multi_item_atomico(app, admin_user, catalogo, cliente):
    from app.extensions import db
    from app.models import EstoqueProducao, MovEstoqueProducao, Receita
    r2 = Receita(nome='Pain au Chocolat', categoria='Croissants',
                 rendimento_qtd=1, rendimento_unidade='un', peso_base=100.0)
    db.session.add(r2)
    db.session.commit()
    _login(cliente)
    r = cliente.post('/padeiro/produzir', json={'itens': [
        {'ref': 'receita:%d' % catalogo['receita'].id, 'quantidade': 2},
        {'ref': 'receita:%d' % r2.id, 'quantidade': 3},
    ]})
    assert r.status_code == 200 and r.get_json()['ok'] is True
    assert EstoqueProducao.query.filter_by(estado=None).count() == 2
    assert MovEstoqueProducao.query.filter_by(tipo='producao').count() == 2


def test_produzir_invalido_nao_grava(app, admin_user, catalogo, cliente):
    from app.models import EstoqueProducao
    _login(cliente)
    rid = catalogo['receita'].id
    antes = EstoqueProducao.query.count()
    assert cliente.post('/padeiro/produzir', json={'itens': []}).status_code == 400
    assert cliente.post('/padeiro/produzir',
                        json={'itens': [{'ref': 'receita:%d' % rid, 'quantidade': 0}]}).status_code == 400
    assert cliente.post('/padeiro/produzir',
                        json={'itens': [{'ref': 'receita:999999', 'quantidade': 1}]}).status_code == 400
    assert EstoqueProducao.query.count() == antes  # nada gravado


def test_congelados_entrada_tem_typeahead(app, admin_user, catalogo, cliente):
    """A entrada de producao em /pedidos/congelados usa busca por digitacao
    (typeahead), igual ao formulario B2B, no lugar do <select> gigante."""
    _login(cliente)
    r = cliente.get('/pedidos/congelados')
    assert r.status_code == 200
    assert b'entrada-busca' in r.data    # campo de busca
    assert b'ENTRADA_TODOS' in r.data    # JS do typeahead (receitas+produtos)


def test_congelados_entrada_route_mira_cru(app, admin_user, catalogo, cliente):
    """A rota antiga /pedidos/congelados/entrada agora usa o mesmo helper: soma
    na linha cru mesmo havendo linha backup (regressao do bug do .first())."""
    from app.extensions import db
    from app.models import EstoqueProducao
    rid = catalogo['receita'].id
    db.session.add(EstoqueProducao(receita_id=rid, estado='backup', quantidade=100))
    db.session.commit()
    _login(cliente)
    r = cliente.post('/pedidos/congelados/entrada',
                     data={'tipo': 'receita', 'item_id': rid, 'quantidade': 6})
    assert r.status_code == 302
    assert EstoqueProducao.query.filter_by(
        receita_id=rid, estado='backup').one().quantidade == 100
    assert EstoqueProducao.query.filter_by(
        receita_id=rid, estado=None).one().quantidade == 6


def test_produzir_nao_autorizado(app, catalogo, cliente):
    from app.extensions import db
    from app.models import Usuario
    u = Usuario(nome='func', login='func', papel='funcionario')
    u.set_senha('123')
    db.session.add(u)
    db.session.commit()
    _login(cliente, login='func')
    r = cliente.post('/padeiro/produzir',
                     json={'itens': [{'ref': 'receita:%d' % catalogo['receita'].id, 'quantidade': 1}]})
    assert r.status_code == 403


def test_buscar_acento_insensivel(app, admin_user, cliente):
    from app.extensions import db
    from app.models import Receita
    db.session.add(Receita(nome='Pão Francês', categoria='Paes',
                           rendimento_qtd=1, rendimento_unidade='un', peso_base=100.0))
    db.session.commit()
    _login(cliente)

    def nomes(qval):
        return [r['nome'] for r in cliente.get(
            '/padeiro/buscar-receitas.json', query_string={'q': qval}
        ).get_json()['itens']]
    assert 'Pão Francês' in nomes('pao')          # sem acento acha com acento
    assert 'Pão Francês' in nomes('pao frances')  # multi-termo, tudo sem acento


def test_producao_historico_lista_lancamentos(app, admin_user, catalogo, cliente):
    """O que foi produzido pelo painel aparece em /padeiro/producao-historico.json."""
    _login(cliente)
    rid = catalogo['receita'].id
    cliente.post('/padeiro/produzir',
                 json={'itens': [{'ref': 'receita:%d' % rid, 'quantidade': 12}]})
    j = cliente.get('/padeiro/producao-historico.json').get_json()
    assert j['historico']
    linha = j['historico'][0]
    assert linha['item'] == 'Croissant Tradicional'
    assert linha['qtd'] == 12
    assert linha['quando']  # data/hora preenchida


def test_produzir_painel_titulos_qtd_e_historico(app, admin_user, catalogo, cliente):
    """Painel Produzir: titulos de coluna, qtd sem '1' fixo, e botao/overlay de historico."""
    _login(cliente)
    r = cliente.get('/padeiro/')
    assert r.status_code == 200
    assert b'ph-nome">Produto' in r.data           # titulo coluna Produto
    assert b'ph-qtd">Quantidade' in r.data          # titulo coluna Quantidade
    assert b'placeholder="qtd"' in r.data           # qtd começa vazia (sem value="1")
    assert b'prodHist()' in r.data                  # botao historico
    assert b'id="prod-hist"' in r.data              # overlay do historico
