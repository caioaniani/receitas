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


# ── Transferência MP → receita (o espelho do receita → MP) ──────────────────
def _receita(nome):
    from app.models import Receita
    r = Receita(nome=nome, categoria='Geleias', rendimento_qtd=1,
                rendimento_unidade='un', peso_base=100.0)
    db.session.add(r)
    db.session.commit()
    return r


def test_transferir_mp_para_receita_move_tudo(app, admin_user):
    """MP que na verdade é produzida (Geleia): pedidos, estoque (fusão),
    desperdício, cesta, mapeamento e uso-como-ingrediente vão pra receita."""
    from app.models import (
        Desperdicio,
        Produto,
        ProdutoItem,
        Receita,
        ReceitaIngrediente,
    )
    m = _mp('Geleia Artesanal de Morango')
    destino = _receita('Geleia Artesanal de Morango')
    loja = Loja(nome='Centro', ativa=True)
    db.session.add(loja)
    db.session.flush()
    # pedido + desperdício + estoque (receita JÁ tem linha -> funde 4+2=6)
    p = PedidoLoja(loja_id=loja.id, status='entregue')
    db.session.add(p)
    db.session.flush()
    db.session.add(PedidoItem(pedido_id=p.id, materia_prima_id=m.id,
                              quantidade=10))
    db.session.add(Desperdicio(loja_id=loja.id, materia_prima_id=m.id,
                               quantidade=1))
    el_r = EstoqueLoja(loja_id=loja.id, receita_id=destino.id, quantidade=4)
    el_m = EstoqueLoja(loja_id=loja.id, materia_prima_id=m.id, quantidade=2)
    db.session.add_all([el_r, el_m])
    # cesta + mapeamento + ingrediente em outra ficha (por nome, tipo mp)
    cesta = Produto(nome='Cesta Café', ativo=True)
    db.session.add(cesta)
    db.session.flush()
    db.session.add(ProdutoItem(produto_id=cesta.id, tipo='mp',
                               materia_prima_id=m.id, item_nome=m.nome,
                               quantidade=1))
    db.session.add(VendaMapa(canal='seru', nome_externo='GELEIA',
                             materia_prima_id=m.id))
    outra = _receita('Cheesecake')
    db.session.add(ReceitaIngrediente(receita_id=outra.id, tipo='mp',
                                      ingrediente_nome=m.nome, porcentagem=30))
    db.session.commit()
    el_r_id, cesta_id, outra_id = el_r.id, cesta.id, outra.id

    c = _login(app, admin_user)
    resp = c.post(f'/materias-primas/{m.id}/transferir',
                  data={'destino': 'geleia artesanal de morango'},  # case-insens.
                  follow_redirects=True)
    assert resp.status_code == 200

    pi = PedidoItem.query.one()
    assert pi.materia_prima_id is None and pi.receita_id == destino.id
    assert Desperdicio.query.one().receita_id == destino.id
    el = db.session.get(EstoqueLoja, el_r_id)
    assert el.quantidade == 6                            # fusão 4+2
    assert EstoqueLoja.query.filter_by(materia_prima_id=m.id).count() == 0
    pit = ProdutoItem.query.filter_by(produto_id=cesta_id).one()
    assert pit.tipo == 'receita' and pit.receita_id == destino.id
    vm = VendaMapa.query.one()
    assert vm.receita_id == destino.id and vm.materia_prima_id is None
    ing = ReceitaIngrediente.query.filter_by(receita_id=outra_id).one()
    assert ing.tipo == 'receita' and ing.sub_receita_id == destino.id
    # MP livre -> agora exclui de verdade
    c.post(f'/materias-primas/excluir/{m.id}', follow_redirects=True)
    from app.models import MateriaPrima as MPModel
    assert db.session.get(MPModel, m.id) is None
    assert db.session.get(Receita, destino.id) is not None


def test_transferir_exige_receita_existente(app, admin_user):
    m = _mp('Geleia X')
    c = _login(app, admin_user)
    resp = c.post(f'/materias-primas/{m.id}/transferir',
                  data={'destino': 'Nao Existe'}, follow_redirects=True)
    assert 'não encontrada' in resp.get_data(as_text=True)
    assert db.session.get(MateriaPrima, m.id) is not None


def test_transferir_get_renderiza_vinculos(app, admin_user):
    m = _mp('Geleia Y')
    loja = Loja(nome='Centro', ativa=True)
    db.session.add(loja)
    db.session.flush()
    db.session.add(EstoqueLoja(loja_id=loja.id, materia_prima_id=m.id,
                               quantidade=3))
    db.session.commit()
    c = _login(app, admin_user)
    resp = c.get(f'/materias-primas/{m.id}/transferir')
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert 'Geleia Y' in body
    assert 'estoque de loja' in body                     # lista o vínculo
    assert 'receita-list' in body                        # autocompleta receita
