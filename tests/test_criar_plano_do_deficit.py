"""POST /producao/painel/criar-plano-do-deficit — cria PlanejamentoProducao
do dia preenchido com as receitas em deficit (Produzir > 0)."""
from datetime import timedelta
from math import ceil

from app.extensions import db
from app.models import Loja, PedidoItem, PedidoLoja, PlanejamentoProducao, Receita
from app.utils import hoje


def _login(client, admin_user):
    client.post('/auth/login',
                data={'login': admin_user.login, 'senha': '123'},
                follow_redirects=True)


def _receita(nome, rendimento=10):
    r = Receita(nome=nome, categoria='Paes', rendimento_qtd=rendimento,
                rendimento_unidade='un', peso_base=100.0)
    db.session.add(r)
    db.session.commit()
    return r


def _pedido(loja, status, data_entrega, receita_id, qtd):
    p = PedidoLoja(loja_id=loja.id, status=status, data_entrega=data_entrega,
                   data_pedido=data_entrega)
    db.session.add(p)
    db.session.flush()
    db.session.add(PedidoItem(pedido_id=p.id, receita_id=receita_id,
                              quantidade=qtd))
    db.session.commit()
    return p


def test_cria_plano_com_multiplicador_correto(app, admin_user):
    loja = Loja(nome='Loja A', ativa=True)
    db.session.add(loja)
    db.session.commit()
    # Croissant: rendimento 12/fornada, deficit 48 -> multiplicador 4.
    cro = _receita('Croissant Nutella', rendimento=12)
    _pedido(loja, 'pendente', hoje() + timedelta(days=1), cro.id, 48)

    client = app.test_client()
    _login(client, admin_user)
    r = client.post('/producao/painel/criar-plano-do-deficit',
                    data={'horizonte': 7, 'janela': 6})
    assert r.status_code == 302
    assert '/producao/' in r.headers['Location']

    plano = PlanejamentoProducao.query.first()
    assert plano is not None
    assert plano.data == hoje()
    assert len(plano.itens) == 1
    it = plano.itens[0]
    assert it.receita_id == cro.id
    assert it.multiplicador == 4   # ceil(48 / 12)


def test_multiplicador_arredonda_pra_cima(app, admin_user):
    loja = Loja(nome='Loja A', ativa=True)
    db.session.add(loja)
    db.session.commit()
    # Rendimento 10, deficit 23 -> ceil(2.3) = 3 (produz 30, sobra 7).
    r = _receita('Pao', rendimento=10)
    _pedido(loja, 'pendente', hoje() + timedelta(days=1), r.id, 23)

    client = app.test_client()
    _login(client, admin_user)
    client.post('/producao/painel/criar-plano-do-deficit',
                data={'horizonte': 7, 'janela': 6})

    plano = PlanejamentoProducao.query.first()
    assert plano.itens[0].multiplicador == ceil(23 / 10)


def test_sem_deficit_nao_cria_plano(app, admin_user):
    """Sem receita com Produzir > 0, nao cria plano e redireciona pro painel."""
    client = app.test_client()
    _login(client, admin_user)
    r = client.post('/producao/painel/criar-plano-do-deficit',
                    data={'horizonte': 7, 'janela': 6}, follow_redirects=False)
    assert r.status_code == 302
    assert '/producao/painel' in r.headers['Location']
    assert PlanejamentoProducao.query.count() == 0


def test_so_receitas_com_deficit_entram(app, admin_user):
    """Receitas com estoque suficiente nao entram (produzir=0)."""
    from app.models import EstoqueProducao
    loja = Loja(nome='Loja A', ativa=True)
    db.session.add(loja)
    db.session.commit()
    r1 = _receita('Tem deficit', rendimento=10)
    r2 = _receita('Coberta', rendimento=10)
    db.session.add(EstoqueProducao(receita_id=r2.id, quantidade=100))
    db.session.commit()
    d = hoje() + timedelta(days=1)
    _pedido(loja, 'pendente', d, r1.id, 30)
    _pedido(loja, 'pendente', d, r2.id, 30)   # coberto pelos 100 em estoque

    client = app.test_client()
    _login(client, admin_user)
    client.post('/producao/painel/criar-plano-do-deficit',
                data={'horizonte': 7, 'janela': 6})
    plano = PlanejamentoProducao.query.first()
    assert plano is not None
    assert len(plano.itens) == 1
    assert plano.itens[0].receita_id == r1.id


def test_botao_aparece_no_painel_quando_ha_deficit(app, admin_user):
    loja = Loja(nome='Loja A', ativa=True)
    db.session.add(loja)
    db.session.commit()
    r = _receita('Pao', rendimento=10)
    _pedido(loja, 'pendente', hoje() + timedelta(days=1), r.id, 20)

    client = app.test_client()
    _login(client, admin_user)
    resp = client.get('/producao/painel')
    assert resp.status_code == 200
    assert b'Criar plano do d' in resp.data        # "Criar plano do déficit"


def test_painel_embute_grade_inline_lazy(app, admin_user):
    """A grade loja × dia agora abre como drop-down inline (lazy AJAX) ao expandir
    a linha — não navega mais pra outra tela. O container leva a URL do fragmento
    (?partial=1) e o painel tem o carregador JS."""
    loja = Loja(nome='Loja A', ativa=True)
    db.session.add(loja)
    db.session.commit()
    r = _receita('Pao', rendimento=10)
    _pedido(loja, 'pendente', hoje() + timedelta(days=1), r.id, 20)

    client = app.test_client()
    _login(client, admin_user)
    resp = client.get('/producao/painel')
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert 'grade-inline' in body
    assert 'partial=1' in body                 # container aponta pro fragmento
    assert 'carregarGradeInline' in body       # carregador lazy presente
