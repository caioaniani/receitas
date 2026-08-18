"""Busca de itens por typeahead no novo pedido.

Cobre: endpoint /pedidos/buscar-itens.json retorna itens em formato
r_/p_/mp_, filtra por substring acento-insensível e multi-termo,
mínimo 2 caracteres, e está protegido por @pedidos_required.
"""


def _login(client, user):
    """Aceita um id (int) ou uma instância Usuario atachada."""
    uid = user if isinstance(user, int) else user.id
    with client.session_transaction() as sess:
        sess['_user_id'] = str(uid)
        sess['_fresh'] = True


def test_buscar_itens_receitas(app, admin_user):
    from app.extensions import db
    from app.models import Receita

    with app.app_context():
        r = Receita(nome='Pão Francês', categoria='Básicos',
                    rendimento_qtd=1, rendimento_unidade='un', peso_base=100.0)
        db.session.add(r)
        db.session.commit()
        rid = r.id

    client = app.test_client()
    _login(client, admin_user)
    resp = client.get('/pedidos/buscar-itens.json?q=pao')
    assert resp.status_code == 200
    data = resp.get_json()
    assert 'itens' in data
    nomes = [item['nome'] for item in data['itens']]
    assert 'Pão Francês' in nomes
    ids = [item['id'] for item in data['itens']]
    assert f'r_{rid}' in ids


def test_buscar_itens_produtos(app, admin_user):
    from app.extensions import db
    from app.models import Produto

    with app.app_context():
        p = Produto(nome='Cesta Especial', ativo=True)
        db.session.add(p)
        db.session.commit()
        pid = p.id

    client = app.test_client()
    _login(client, admin_user)
    resp = client.get('/pedidos/buscar-itens.json?q=cesta')
    assert resp.status_code == 200
    data = resp.get_json()
    nomes = [item['nome'] for item in data['itens']]
    assert 'Cesta Especial' in nomes
    ids = [item['id'] for item in data['itens']]
    assert f'p_{pid}' in ids


def test_buscar_itens_materias_primas(app, admin_user):
    """MP so aparece no typeahead se LIBERADA pra pedido de loja (checkbox
    "sugerir pedido loja" — trava de 07/07/2026, ver test_mp_pedivel.py)."""
    from app.extensions import db
    from app.models import MateriaPrima

    with app.app_context():
        m = MateriaPrima(nome='Farinha Integral', unidade='kg', custo_por_kg=5.0,
                         sugerir_pedido_loja=True)
        db.session.add(m)
        db.session.commit()
        mid = m.id

    client = app.test_client()
    _login(client, admin_user)
    resp = client.get('/pedidos/buscar-itens.json?q=farinha')
    assert resp.status_code == 200
    data = resp.get_json()
    nomes = [item['nome'] for item in data['itens']]
    assert 'Farinha Integral' in nomes
    ids = [item['id'] for item in data['itens']]
    assert f'mp_{mid}' in ids


def test_buscar_itens_acento_insensivel(app, admin_user):
    from app.extensions import db
    from app.models import Receita

    with app.app_context():
        r = Receita(nome='Pão de Queijo', categoria='Pães',
                    rendimento_qtd=1, rendimento_unidade='un', peso_base=50.0)
        db.session.add(r)
        db.session.commit()

    client = app.test_client()
    _login(client, admin_user)
    # Busca sem acento mas receita tem
    resp = client.get('/pedidos/buscar-itens.json?q=pao')
    assert resp.status_code == 200
    data = resp.get_json()
    nomes = [item['nome'] for item in data['itens']]
    assert 'Pão de Queijo' in nomes


def test_buscar_itens_multi_termo(app, admin_user):
    from app.extensions import db
    from app.models import Receita

    with app.app_context():
        r1 = Receita(nome='Pão de Queijo', categoria='Pães',
                     rendimento_qtd=1, rendimento_unidade='un', peso_base=50.0)
        r2 = Receita(nome='Pão Francês', categoria='Pães',
                     rendimento_qtd=1, rendimento_unidade='un', peso_base=100.0)
        db.session.add(r1)
        db.session.add(r2)
        db.session.commit()

    client = app.test_client()
    _login(client, admin_user)
    # Busca por múltiplos termos
    resp = client.get('/pedidos/buscar-itens.json?q=pao queijo')
    assert resp.status_code == 200
    data = resp.get_json()
    nomes = [item['nome'] for item in data['itens']]
    # Só "Pão de Queijo" casa com ambos
    assert 'Pão de Queijo' in nomes
    assert 'Pão Francês' not in nomes


def test_buscar_itens_minimo_2_chars(app, admin_user):
    from app.extensions import db
    from app.models import Receita

    with app.app_context():
        r = Receita(nome='Pão Francês', categoria='Pães',
                    rendimento_qtd=1, rendimento_unidade='un', peso_base=100.0)
        db.session.add(r)
        db.session.commit()

    client = app.test_client()
    _login(client, admin_user)
    # Busca com menos de 2 caracteres
    resp = client.get('/pedidos/buscar-itens.json?q=p')
    assert resp.status_code == 200
    data = resp.get_json()
    # Deve retornar vazio
    assert len(data['itens']) == 0


def test_buscar_itens_padeiro_bloqueado(app):
    from app.extensions import db
    from app.models import Receita, Usuario

    with app.app_context():
        padeiro = Usuario(login='padeiro', nome='Padeiro', papel='padeiro')
        padeiro.set_senha('senha123')
        db.session.add(padeiro)
        r = Receita(nome='Pão Francês', categoria='Pães',
                    rendimento_qtd=1, rendimento_unidade='un', peso_base=100.0)
        db.session.add(r)
        db.session.commit()
        uid = padeiro.id

    client = app.test_client()
    _login(client, uid)
    # Padeiro não tem acesso
    resp = client.get('/pedidos/buscar-itens.json?q=pao')
    assert resp.status_code == 403


def test_post_novo_pedido_cria_com_item_r(app, admin_user, loja):
    """O typeahead envia item_id[]=r_<id>; o POST cria o pedido (contrato mantido)."""
    from app.extensions import db
    from app.models import PedidoLoja, Receita
    from app.utils import hoje

    with app.app_context():
        r = Receita(nome='Pão Teste', categoria='Pães', rendimento_qtd=1,
                    rendimento_unidade='un', peso_base=100.0)
        db.session.add(r)
        db.session.commit()
        rid, lid = r.id, loja.id
        data = hoje().isoformat()

    client = app.test_client()
    _login(client, admin_user)
    resp = client.post('/pedidos/novo', data={
        'loja_id': str(lid),
        'data_entrega': data,
        'item_id[]': f'r_{rid}',
        'item_qtd[]': '5',
        'item_estado[]': '',
        'item_obs[]': '',
    }, follow_redirects=False)
    assert resp.status_code in (302, 303)

    with app.app_context():
        peds = PedidoLoja.query.filter_by(loja_id=lid).all()
        assert len(peds) == 1
        assert len(peds[0].itens) == 1
        assert peds[0].itens[0].receita_id == rid
        assert peds[0].itens[0].quantidade == 5


def test_post_novo_pedido_ignora_item_sem_id(app, admin_user, loja):
    """Linha com texto mas sem item_id (typeahead sem escolher / id limpo ao
    reescrever) é ignorada — não vira item fantasma. Rede de segurança do
    fix de 'id velho' no JS."""
    from app.extensions import db
    from app.models import PedidoLoja, Receita
    from app.utils import hoje

    with app.app_context():
        r = Receita(nome='Pão Teste', categoria='Pães', rendimento_qtd=1,
                    rendimento_unidade='un', peso_base=100.0)
        db.session.add(r)
        db.session.commit()
        rid, lid = r.id, loja.id
        data = hoje().isoformat()

    client = app.test_client()
    _login(client, admin_user)
    resp = client.post('/pedidos/novo', data={
        'loja_id': str(lid),
        'data_entrega': data,
        # 2 linhas: uma válida, uma com id vazio (texto digitado sem escolher)
        'item_id[]': [f'r_{rid}', ''],
        'item_qtd[]': ['5', '3'],
        'item_estado[]': ['', ''],
        'item_obs[]': ['', ''],
    }, follow_redirects=False)
    assert resp.status_code in (302, 303)

    with app.app_context():
        peds = PedidoLoja.query.filter_by(loja_id=lid).all()
        assert len(peds) == 1
        # só a linha com id válido virou item; a vazia foi ignorada
        assert len(peds[0].itens) == 1
        assert peds[0].itens[0].receita_id == rid


def test_post_novo_pedido_sem_itens_nao_cria(app, admin_user, loja):
    """Submeter sem nenhum item válido não cria pedido vazio. O <select required>
    antigo barrava isso no cliente; agora o guard server-side cobre."""
    from app.models import PedidoLoja
    from app.utils import hoje

    with app.app_context():
        lid = loja.id
        data = hoje().isoformat()

    client = app.test_client()
    _login(client, admin_user)
    resp = client.post('/pedidos/novo', data={
        'loja_id': str(lid),
        'data_entrega': data,
        'item_id[]': '',
        'item_qtd[]': '1',
        'item_estado[]': '',
        'item_obs[]': '',
    }, follow_redirects=False)
    # re-renderiza o form (200), não redireciona pro detalhe
    assert resp.status_code == 200

    with app.app_context():
        assert PedidoLoja.query.filter_by(loja_id=lid).count() == 0


# ---------------------------------------------------------------------------
# Flag em_gramas no typeahead — aviso "granola em potes" (18/08/2026).
# Caso real: item medido em g/ml ("Produção - Granola Artesanal 1000g",
# peso_unitario=1.0) recebia quantidade em POTES (5) e o relatório de
# pedidos inflava ~1000x. A flag alimenta o aviso não-bloqueante do form.
# ---------------------------------------------------------------------------

def test_buscar_itens_marca_receita_em_gramas(app, admin_user):
    from app.extensions import db
    from app.models import Receita

    with app.app_context():
        # como a granola real: rendimento_unidade VAZIA + peso_unitario=1.0
        granola = Receita(nome='Produção - Granola Artesanal 1000g',
                          categoria='Produção', rendimento_qtd=15300,
                          rendimento_unidade='', peso_base=4000.0,
                          peso_unitario=1.0)
        # como o iogurte real: rendimento_unidade='ml'
        iogurte = Receita(nome='Produção - Iogurte Caseiro 1000ml',
                          categoria='Produção', rendimento_qtd=1170,
                          rendimento_unidade='ml', peso_base=1170.0)
        normal = Receita(nome='Pão Produção Normal', categoria='Pães',
                         rendimento_qtd=1, rendimento_unidade='un',
                         peso_base=100.0, peso_unitario=100.0)
        db.session.add_all([granola, iogurte, normal])
        db.session.commit()

    client = app.test_client()
    _login(client, admin_user)
    data = client.get('/pedidos/buscar-itens.json?q=producao').get_json()
    flags = {i['nome']: i['em_gramas'] for i in data['itens']}
    assert flags['Produção - Granola Artesanal 1000g'] is True
    assert flags['Produção - Iogurte Caseiro 1000ml'] is True
    assert flags['Pão Produção Normal'] is False


def test_buscar_itens_marca_mp_em_gramas(app, admin_user):
    from app.extensions import db
    from app.models import MateriaPrima

    with app.app_context():
        db.session.add_all([
            MateriaPrima(nome='Granola a granel', unidade='g',
                         custo_por_kg=30.0, sugerir_pedido_loja=True),
            MateriaPrima(nome='Granola em pacote', unidade='un',
                         custo_por_kg=30.0, sugerir_pedido_loja=True),
        ])
        db.session.commit()

    client = app.test_client()
    _login(client, admin_user)
    data = client.get('/pedidos/buscar-itens.json?q=granola').get_json()
    flags = {i['nome']: i['em_gramas'] for i in data['itens']}
    assert flags['Granola a granel'] is True
    assert flags['Granola em pacote'] is False


def test_medida_em_gramas_propriedade(app):
    """A heurística da Receita: unidade g/ml/kg/l OU peso_unitario == 1.0."""
    from app.models import Receita

    with app.app_context():
        def _r(un, peso_unit):
            return Receita(nome='x', categoria='y', rendimento_qtd=1,
                           rendimento_unidade=un, peso_base=1.0,
                           peso_unitario=peso_unit)
        assert _r('g', None).medida_em_gramas is True
        assert _r('KG', 500.0).medida_em_gramas is True
        assert _r('', 1.0).medida_em_gramas is True
        assert _r('un', 90.0).medida_em_gramas is False
        assert _r('un', None).medida_em_gramas is False
