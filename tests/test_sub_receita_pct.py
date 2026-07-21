"""Sub-receita em % na ficha técnica (13/07/2026).

Novo tipo de ingrediente `sub_pct`: consome uma SUB-RECEITA como % da base
(igual MP %), em vez da quantidade absoluta de unidades (`receita`). Conta
central: `app.utils.unidades_subreceita`.

Trava de EQUIVALÊNCIA (caso real "7 grãos"): num pai com peso_base=1000, a sub
em `sub_pct` a 10% tem de dar EXATAMENTE o mesmo que em `receita` a 100 —
compra, baixa de estoque e custo idênticos. Assim ninguém que já usa o modo
absoluto é afetado, e o % é só um jeito mais legível de dizer o mesmo.
"""
from app.extensions import db
from app.models import (
    EstoqueProducao,
    MateriaPrima,
    Receita,
    ReceitaIngrediente,
)


def test_helper_dois_modos():
    from app.utils import unidades_subreceita
    assert unidades_subreceita('receita', 100, 1000) == 100
    assert unidades_subreceita('sub_pct', 10, 1000) == 100.0     # 10% de 1000
    assert unidades_subreceita('sub_pct', 10, 500) == 50.0
    # 10% de sub_pct (base 1000) == 100 absoluto → equivalência do "7 grãos"
    assert unidades_subreceita('sub_pct', 10, 1000) == unidades_subreceita('receita', 100, 1000)


def _monta(modo, pct, suf):
    """Pai (peso_base 1000, rende 10) consome a sub 'Mix<suf>' no `modo`/`pct`.
    A sub tem 1 MP direta (1000 g/base). Nomes com sufixo pra não colidir
    quando os dois modos coexistem no mesmo teste. Retorna (pai, sub, mp_nome)."""
    mp_nome = f'Semente{suf}'
    db.session.add(MateriaPrima(nome=mp_nome, unidade='g', custo_por_kg=10.0,
                                estoque_atual=0))
    sub = Receita(nome=f'Mix{suf}', categoria='pré-preparo',
                  rendimento_qtd=1000, rendimento_unidade='unidades',
                  peso_base=1000.0, peso_unitario=1.0)
    db.session.add(sub)
    db.session.flush()
    db.session.add(ReceitaIngrediente(
        receita_id=sub.id, tipo='mp_direto', ingrediente_nome=mp_nome,
        porcentagem=1000.0))
    pai = Receita(nome=f'Pao{suf}', categoria='Paes', rendimento_qtd=10,
                  rendimento_unidade='un', peso_base=1000.0)
    db.session.add(pai)
    db.session.flush()
    db.session.add(ReceitaIngrediente(
        receita_id=pai.id, tipo=modo, ingrediente_nome=sub.nome,
        porcentagem=pct, sub_receita_id=sub.id))
    db.session.commit()
    return pai, sub, mp_nome


def _mp_da_compra(res, nome):
    for f in res['compra']['fornecedores']:
        for it in f['itens']:
            if it['nome'] == nome:
                return it
    return None


def test_compra_equivale_absoluto_e_pct(app):
    """Calculadora de compras: sub em `receita` 100 == `sub_pct` 10 (base 1000)."""
    from app.services import calculadora_compras
    with app.app_context():
        pai_a, _, mp_a = _monta('receita', 100, 'A')
        pai_b, _, mp_b = _monta('sub_pct', 10, 'B')
        q_abs = _mp_da_compra(calculadora_compras.calcular(
            [{'tipo': 'receita', 'id': pai_a.id, 'qtd': 50}],
            considerar_estoque=False), mp_a)['quantidade']
        q_pct = _mp_da_compra(calculadora_compras.calcular(
            [{'tipo': 'receita', 'id': pai_b.id, 'qtd': 50}],
            considerar_estoque=False), mp_b)['quantidade']
    assert q_abs > 0 and round(q_abs, 4) == round(q_pct, 4)


def test_sub_pct_diferente_quando_pct_diferente(app):
    """Sanidade: sub_pct 20% (base 1000) = 2× a de 10% (não é no-op)."""
    from app.services import calculadora_compras
    with app.app_context():
        pai_a, _, mp_a = _monta('sub_pct', 10, 'A')
        pai_b, _, mp_b = _monta('sub_pct', 20, 'B')
        q10 = _mp_da_compra(calculadora_compras.calcular(
            [{'tipo': 'receita', 'id': pai_a.id, 'qtd': 50}],
            considerar_estoque=False), mp_a)['quantidade']
        q20 = _mp_da_compra(calculadora_compras.calcular(
            [{'tipo': 'receita', 'id': pai_b.id, 'qtd': 50}],
            considerar_estoque=False), mp_b)['quantidade']
    assert round(q20, 4) == round(2 * q10, 4)


def test_baixa_real_equivale(app, admin_user):
    """Baixa de produção (consumir_subreceitas_prontas): `sub_pct` 10 baixa a
    MESMA quantidade da sub que `receita` 100."""
    from app.services.producao import consumir_subreceitas_prontas
    with app.app_context():
        pai_a, sub_a, _ = _monta('receita', 100, 'A')
        pai_b, sub_b, _ = _monta('sub_pct', 10, 'B')
        ep_a = EstoqueProducao(receita_id=sub_a.id, quantidade=100000)
        ep_b = EstoqueProducao(receita_id=sub_b.id, quantidade=100000)
        db.session.add_all([ep_a, ep_b])
        db.session.commit()
        consumir_subreceitas_prontas(pai_a, 30, admin_user.id)
        consumir_subreceitas_prontas(pai_b, 30, admin_user.id)
        db.session.commit()
        db.session.refresh(ep_a)
        db.session.refresh(ep_b)
        baixou_abs = 100000 - ep_a.quantidade
        baixou_pct = 100000 - ep_b.quantidade
    assert baixou_abs > 0 and baixou_abs == baixou_pct


def test_custo_equivale(app):
    """Custo (→ preço/margem): `sub_pct` 10 dá o mesmo custo_un que `receita` 100."""
    from app.services.custos import calcular_custos_receitas
    with app.app_context():
        _monta('receita', 100, 'A')
        _monta('sub_pct', 10, 'B')
        custos = calcular_custos_receitas()
        c_abs = custos.get('PaoA')
        c_pct = custos.get('PaoB')
    assert c_abs and c_pct
    assert round(c_abs, 4) == round(c_pct, 4)


def test_post_ficha_salva_sub_pct_e_resolve_fk(app, admin_user):
    """A ficha salva tipo='sub_pct' E resolve a FK da sub (pra baixa confiável)."""
    with app.app_context():
        sub = Receita(nome='Mix Salvar', categoria='pré-preparo',
                      rendimento_qtd=1000, rendimento_unidade='unidades',
                      peso_base=1000.0, peso_unitario=1.0)
        db.session.add(sub)
        alvo = Receita(nome='Pao Salvar', categoria='Paes',
                       rendimento_qtd=10, rendimento_unidade='un',
                       peso_base=1000.0)
        db.session.add(alvo)
        db.session.commit()
        alvo_id, sub_id = alvo.id, sub.id
    c = app.test_client()
    c.post('/auth/login', data={'login': admin_user.login, 'senha': '123'})
    c.post(f'/receitas/{alvo_id}/salvar', data={
        'nome': 'Pao Salvar', 'categoria': 'Paes',
        'rendimento_qtd': '10', 'peso_base': '1000',
        'ingrediente_tipo[]': ['sub_pct'],
        'ingrediente_nome[]': ['Mix Salvar'],
        'porcentagem[]': ['10'],
        'eh_base[]': ['0'], 'nota[]': [''],
    }, follow_redirects=True)
    with app.app_context():
        ing = ReceitaIngrediente.query.filter_by(receita_id=alvo_id).first()
        assert ing is not None
        assert ing.tipo == 'sub_pct'
        assert ing.sub_receita_id == sub_id      # FK resolvida no salvar
        assert ing.porcentagem == 10.0
