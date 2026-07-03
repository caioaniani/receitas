"""Custo de cesta/sub-receita resolve pela FK, não pelo nome gravado (03/07/2026).

Caso real (iogurte): a receita foi renomeada pra "Produção - Iogurte Caseiro
100g", mas o `ProdutoItem.item_nome` das cestas ficou "Iogurte Caseiro". A FK
(`receita_id`) estava certa — a baixa de venda funcionava — mas TODO caminho
de custo buscava pelo nome gravado e caía em 0 silencioso: a cesta de 600ml
"custava" só a embalagem e a margem saía inflada (91,5% em vez de ~77%).
Pior: a tela da cesta mostrava o nome velho no input, e o Salvar re-resolve a
FK por nome exato — salvar orfanava o vínculo e parava a baixa de venda.

Agora: custo por `nome_resolvido` (FK primeiro), input da cesta mostra o nome
real, e rename de receita/produto sincroniza os nomes-fallback gravados.
"""
from app.extensions import db
from app.models import (
    MateriaPrima,
    Produto,
    ProdutoItem,
    Receita,
    ReceitaIngrediente,
)
from app.services.custos import (
    calcular_custo_produto,
    calcular_custos_produtos,
    calcular_custos_receitas,
)


def _receita_com_custo(nome, custo_mp_kg=10.0, qtd_g=1000.0, peso_un=100.0):
    """Receita com 1 MP direta: custo_total = qtd_g × (custo_kg/1000),
    rendimento = qtd_g / peso_un. Com os defaults: custo R$ 10,00 / 10 un
    = R$ 1,00 por unidade."""
    mp = MateriaPrima.query.filter_by(nome=f'MP {nome}').first()
    if mp is None:
        mp = MateriaPrima(nome=f'MP {nome}', unidade='g',
                          custo_por_kg=custo_mp_kg)
        db.session.add(mp)
        db.session.flush()
    r = Receita(nome=nome, categoria='Teste', rendimento_qtd=1,
                rendimento_unidade='un', peso_base=1000.0,
                peso_unitario=peso_un)
    db.session.add(r)
    db.session.flush()
    db.session.add(ReceitaIngrediente(
        receita_id=r.id, ingrediente_nome=mp.nome, tipo='mp_direto',
        porcentagem=qtd_g))
    db.session.commit()
    return r


def _login(app, user):
    c = app.test_client()
    with c.session_transaction() as sess:
        sess['_user_id'] = str(user.id)
        sess['_fresh'] = True
    return c


def test_custo_cesta_ignora_item_nome_desatualizado(app):
    """FK aponta pra receita renomeada; item_nome guarda a grafia antiga.
    O custo tem que vir da receita mesmo assim (antes: 0 silencioso)."""
    with app.app_context():
        r = _receita_com_custo('Iogurte CFK Novo Nome')
        p = Produto(nome='Cesta CFK 200ml', categoria='Teste', ativo=True,
                    custo_embalagem=2.0)
        db.session.add(p)
        db.session.flush()
        db.session.add(ProdutoItem(produto_id=p.id, tipo='receita',
                                   item_nome='Iogurte CFK Nome Velho',
                                   receita_id=r.id, quantidade=2))
        db.session.commit()
        resultado = calcular_custos_receitas()
        assert resultado['custos'][r.nome] == 1.0
        custo = calcular_custo_produto(p, resultado['custos'],
                                       resultado['mp_info'])
        assert custo == 4.0          # 2 × R$ 1,00 + R$ 2,00 embalagem
        # calcular_custos_produtos (lista de produtos/margens) idem.
        todos = calcular_custos_produtos(resultado['custos'],
                                         resultado['mp_info'])
        assert todos[p.nome] == 4.0


def test_custo_sub_receita_por_fk(app):
    """Ingrediente tipo='receita' com sub_receita_id certo mas
    ingrediente_nome desatualizado resolve o custo pela FK."""
    with app.app_context():
        filha = _receita_com_custo('Filha CFK Nome Novo')
        pai = Receita(nome='Pai CFK', categoria='Teste', rendimento_qtd=1,
                      rendimento_unidade='un', peso_base=1000.0,
                      peso_unitario=100.0)
        db.session.add(pai)
        db.session.flush()
        db.session.add(ReceitaIngrediente(
            receita_id=pai.id, ingrediente_nome='Filha CFK Nome Velho',
            tipo='receita', porcentagem=2, sub_receita_id=filha.id))
        db.session.commit()
        resultado = calcular_custos_receitas()
        # 2 un da filha (R$ 1,00 cada) = R$ 2,00; peso 2×100g / 100g = 2 un
        # → R$ 1,00 por unidade do pai. Sem a FK, caía no ramo circular
        # (dependência nunca resolvida) e custo 0.
        assert resultado['custos'][pai.nome] == 1.0
        assert pai.nome not in resultado['circulares']


def test_orfao_segue_resolvendo_pelo_nome(app):
    """Item órfão (FK NULL) mantém o fallback por item_nome — comportamento
    antigo preservado pra registros pré-B5 não vinculados."""
    with app.app_context():
        r = _receita_com_custo('Orfa CFK')
        p = Produto(nome='Cesta Orfa CFK', categoria='Teste', ativo=True,
                    custo_embalagem=0)
        db.session.add(p)
        db.session.flush()
        db.session.add(ProdutoItem(produto_id=p.id, tipo='receita',
                                   item_nome='Orfa CFK', receita_id=None,
                                   quantidade=3))
        db.session.commit()
        resultado = calcular_custos_receitas()
        custo = calcular_custo_produto(p, resultado['custos'],
                                       resultado['mp_info'])
        assert custo == 3.0


def test_detalhe_cesta_mostra_nome_resolvido_e_custo(app, admin_user):
    """A tela da cesta mostra o nome REAL (via FK) no input — o Salvar
    re-resolve a FK por nome exato, então o nome velho no input orfanava o
    vínculo — e o custo do componente aparece (não mais R$ 0,00)."""
    with app.app_context():
        r = _receita_com_custo('Iogurte Tela CFK')
        p = Produto(nome='Cesta Tela CFK', categoria='Teste', ativo=True,
                    custo_embalagem=2.0)
        db.session.add(p)
        db.session.flush()
        db.session.add(ProdutoItem(produto_id=p.id, tipo='receita',
                                   item_nome='Tela CFK Nome Velho',
                                   receita_id=r.id, quantidade=2))
        db.session.commit()
        c = _login(app, admin_user)
        html = c.get(f'/produtos/{p.id}').get_data(as_text=True)
        assert 'Iogurte Tela CFK' in html
        assert 'Tela CFK Nome Velho' not in html
        # custo/un do componente (R$ 1,00) e total da linha (2 × R$ 1,00).
        # O rodapé "CUSTO TOTAL DA CESTA" soma só os itens no servidor (a
        # embalagem entra via JS), então a prova do fix é a linha do item.
        assert 'R$ 1,00' in html
        assert 'R$ 2,00' in html


def test_rename_receita_sincroniza_nomes_fallback(app, admin_user):
    """Renomear a receita na ficha atualiza item_nome (cestas) e
    ingrediente_nome (receitas-mães) dos vínculos por FK."""
    with app.app_context():
        r = _receita_com_custo('Rename CFK Antes')
        pai = Receita(nome='Pai Rename CFK', categoria='Teste',
                      rendimento_qtd=1, rendimento_unidade='un',
                      peso_base=1000.0, peso_unitario=100.0)
        p = Produto(nome='Cesta Rename CFK', categoria='Teste', ativo=True)
        db.session.add_all([pai, p])
        db.session.flush()
        db.session.add(ReceitaIngrediente(
            receita_id=pai.id, ingrediente_nome='Rename CFK Antes',
            tipo='receita', porcentagem=1, sub_receita_id=r.id))
        pi = ProdutoItem(produto_id=p.id, tipo='receita',
                         item_nome='Rename CFK Antes', receita_id=r.id,
                         quantidade=1)
        db.session.add(pi)
        db.session.commit()
        c = _login(app, admin_user)
        resp = c.post(f'/receitas/{r.id}/salvar',
                      data={'nome': 'Rename CFK Depois',
                            'peso_unitario': '100'})
        assert resp.status_code in (200, 302)
        db.session.expire_all()
        assert Receita.query.get(r.id).nome == 'Rename CFK Depois'
        assert ProdutoItem.query.get(pi.id).item_nome == 'Rename CFK Depois'
        ing = ReceitaIngrediente.query.filter_by(sub_receita_id=r.id).first()
        assert ing.ingrediente_nome == 'Rename CFK Depois'


def test_rename_produto_sincroniza_componente(app, admin_user):
    """Renomear um produto usado como componente de outra cesta atualiza o
    item_nome gravado na cesta-mãe."""
    with app.app_context():
        comp = Produto(nome='Componente CFK Antes', categoria='Teste',
                       ativo=True, custo_direto=5.0)
        mae = Produto(nome='Cesta Mae CFK', categoria='Teste', ativo=True)
        db.session.add_all([comp, mae])
        db.session.flush()
        pi = ProdutoItem(produto_id=mae.id, tipo='produto',
                         item_nome='Componente CFK Antes',
                         produto_componente_id=comp.id, quantidade=1)
        db.session.add(pi)
        db.session.commit()
        c = _login(app, admin_user)
        resp = c.post(f'/produtos/{comp.id}/salvar',
                      data={'nome': 'Componente CFK Depois'})
        assert resp.status_code in (200, 302)
        db.session.expire_all()
        assert ProdutoItem.query.get(pi.id).item_nome == 'Componente CFK Depois'
