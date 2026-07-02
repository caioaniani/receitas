"""Exclusão de matéria-prima: MP com histórico (estoque de loja, pedidos,
cestas, mapeamentos...) NÃO pode ser excluída — mensagem clara em vez do 500
(IntegrityError cru) que acontecia; MP livre sai normalmente (alerta de
estoque, que é config, vai junto)."""
from app.extensions import db
from app.models import (
    AlertaEstoque,
    EstoqueLoja,
    Loja,
    MateriaPrima,
    PedidoItem,
    PedidoLoja,
    VendaMapa,
)


def _mp(nome='Pão de Queijo (congelado)'):
    m = MateriaPrima(nome=nome, unidade='un', custo_por_kg=0.4662)
    db.session.add(m)
    db.session.commit()
    return m


def _login(app, admin_user):
    c = app.test_client()
    c.post('/auth/login', data={'login': admin_user.login, 'senha': '123'},
           follow_redirects=True)
    return c


def test_mp_com_estoque_de_loja_nao_exclui_sem_500(app, admin_user):
    m = _mp()
    loja = Loja(nome='Centro', ativa=True)
    db.session.add(loja)
    db.session.flush()
    db.session.add(EstoqueLoja(loja_id=loja.id, materia_prima_id=m.id,
                               quantidade=50))
    db.session.commit()
    c = _login(app, admin_user)
    resp = c.post(f'/materias-primas/excluir/{m.id}', follow_redirects=True)
    assert resp.status_code == 200                       # não estoura 500
    body = resp.get_data(as_text=True)
    assert 'Não é possível excluir' in body
    assert 'estoque de loja' in body                     # diz O QUE bloqueia
    assert db.session.get(MateriaPrima, m.id) is not None


def test_mp_com_pedido_e_mapeamento_lista_os_vinculos(app, admin_user):
    m = _mp()
    loja = Loja(nome='Centro', ativa=True)
    db.session.add(loja)
    db.session.flush()
    p = PedidoLoja(loja_id=loja.id, status='entregue')
    db.session.add(p)
    db.session.flush()
    db.session.add(PedidoItem(pedido_id=p.id, materia_prima_id=m.id,
                              quantidade=111))
    db.session.add(VendaMapa(canal='seru', nome_externo='PAO DE QUEIJO',
                             materia_prima_id=m.id))
    db.session.commit()
    c = _login(app, admin_user)
    resp = c.post(f'/materias-primas/excluir/{m.id}', follow_redirects=True)
    body = resp.get_data(as_text=True)
    assert 'pedido(s) de loja' in body
    assert 'mapeamento(s)' in body
    assert db.session.get(MateriaPrima, m.id) is not None


def test_mp_livre_exclui_e_leva_o_alerta_junto(app, admin_user):
    """MP sem histórico sai; o alerta de estoque (config) não bloqueia."""
    m = _mp('MP Sem Uso')
    db.session.add(AlertaEstoque(materia_prima_id=m.id, estoque_minimo=5))
    db.session.commit()
    c = _login(app, admin_user)
    resp = c.post(f'/materias-primas/excluir/{m.id}', follow_redirects=True)
    assert resp.status_code == 200
    assert 'excluído com sucesso' in resp.get_data(as_text=True)
    assert db.session.get(MateriaPrima, m.id) is None
    assert AlertaEstoque.query.count() == 0
