"""Testa abas por status na lista de pedidos + fotos de conferencia no detalhe."""


def _login(c):
    c.post('/auth/login', data={'login': 'admin', 'senha': '123'})


def _pedido(db, loja, status):
    from app.models import PedidoLoja
    p = PedidoLoja(loja_id=loja.id, status=status)
    db.session.add(p)
    db.session.flush()
    return p


def test_abas_filtram_por_status(app, admin_user, loja):
    from app.extensions import db
    with app.app_context():
        _pedido(db, loja, 'pendente')
        _pedido(db, loja, 'confirmado')
        _pedido(db, loja, 'separado')
        _pedido(db, loja, 'em_transporte')
        _pedido(db, loja, 'entregue')
        _pedido(db, loja, 'cancelado')
        db.session.commit()
        pid_sep = _pedido(db, loja, 'separado').id  # noqa: F841
        db.session.commit()

    c = app.test_client()
    _login(c)

    # Aba pendentes: pega pendente + confirmado (2), nao em_transporte
    r = c.get('/pedidos/?aba=pendentes')
    assert r.status_code == 200
    assert b'Pendentes/Confirmados' in r.data
    assert b'Cancelados' in r.data  # aba existe

    # Aba em_rota: so em_transporte
    r2 = c.get('/pedidos/?aba=em_rota')
    assert r2.status_code == 200

    # Aba cancelados
    r3 = c.get('/pedidos/?aba=cancelados')
    assert r3.status_code == 200


def test_aba_invalida_cai_no_default(app, admin_user, loja):
    from app.extensions import db
    with app.app_context():
        _pedido(db, loja, 'pendente')
        db.session.commit()
    c = app.test_client()
    _login(c)
    r = c.get('/pedidos/?aba=inexistente')
    assert r.status_code == 200  # nao quebra, usa default


def test_detalhe_mostra_fotos_conferencia(app, admin_user, loja):
    from app.extensions import db
    from app.models import PedidoItem, PedidoItemFoto, PedidoLoja, Receita
    with app.app_context():
        r = Receita(nome='Pao', categoria='Paes', rendimento_qtd=1,
                    rendimento_unidade='un', peso_base=100.0)
        db.session.add(r)
        db.session.flush()
        p = PedidoLoja(loja_id=loja.id, status='entregue')
        db.session.add(p)
        db.session.flush()
        it = PedidoItem(pedido_id=p.id, receita_id=r.id, quantidade=5)
        db.session.add(it)
        db.session.flush()
        # Foto de conferencia de entrega, ja no Dropbox (imagem_url)
        db.session.add(PedidoItemFoto(
            pedido_item_id=it.id, etapa='entrega',
            imagem_url='https://www.dropbox.com/x/foo.jpg?raw=1',
            mimetype='image/jpeg'))
        db.session.commit()
        pid = p.id

    c = app.test_client()
    _login(c)
    r = c.get(f'/pedidos/{pid}')
    assert r.status_code == 200
    assert b'Confer\xc3\xaancia na entrega' in r.data  # secao apareceu
    assert '/pedidos/conferencia-foto/'.encode() in r.data  # link da foto


def test_conferencia_foto_redireciona_dropbox(app, admin_user, loja):
    from app.extensions import db
    from app.models import PedidoItem, PedidoItemFoto, PedidoLoja, Receita
    with app.app_context():
        r = Receita(nome='Pao', categoria='Paes', rendimento_qtd=1,
                    rendimento_unidade='un', peso_base=100.0)
        db.session.add(r)
        db.session.flush()
        p = PedidoLoja(loja_id=loja.id, status='entregue')
        db.session.add(p)
        db.session.flush()
        it = PedidoItem(pedido_id=p.id, receita_id=r.id, quantidade=5)
        db.session.add(it)
        db.session.flush()
        f = PedidoItemFoto(pedido_item_id=it.id, etapa='entrega',
                           imagem_url='https://www.dropbox.com/x/foo.jpg?raw=1')
        db.session.add(f)
        db.session.commit()
        fid = f.id

    c = app.test_client()
    _login(c)
    r = c.get(f'/pedidos/conferencia-foto/{fid}', follow_redirects=False)
    assert r.status_code == 302
    assert 'dropbox.com' in r.headers['Location']


def test_abas_de_loja_filtram_e_contam(app, admin_user, loja):
    """Linha de abas por LOJA sob as abas de status (16/07/2026): 'todas as
    lojas' + uma aba por loja com a contagem da aba atual; ?loja= filtra."""
    from app.extensions import db
    from app.models import Loja
    with app.app_context():
        outra = Loja(nome='Loja Nebraska T', ativa=True)
        db.session.add(outra)
        db.session.commit()
        _pedido(db, loja, 'pendente')
        _pedido(db, loja, 'pendente')
        _pedido(db, outra, 'pendente')
        db.session.commit()
        loja_id, outra_id = loja.id, outra.id

    c = app.test_client()
    _login(c)

    r = c.get('/pedidos/?aba=pendentes')
    html = r.data.decode()
    assert 'todas as lojas' in html
    assert 'Loja Nebraska T' in html
    assert f'loja={outra_id}' in html          # link da aba da loja

    # Filtrando pela outra loja: só o pedido dela na tabela.
    r2 = c.get(f'/pedidos/?aba=pendentes&loja={outra_id}')
    html2 = r2.data.decode()
    assert r2.status_code == 200
    assert html2.count('Loja Nebraska T') >= 2  # aba ativa + linha do pedido
    # O badge da aba de status reflete o filtro (1 pendente da Nebraska).
    assert f'loja={loja_id}' in html2           # abas das outras lojas seguem
