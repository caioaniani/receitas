"""Fix do modo "Quantidade de Produtos" para receita MONTADA (Pain au chocolat
bicolor etc.): só MP em g/un, sem % de padeiro, cada linha é "por unidade".

Regra (app/blueprints/receitas/routes.py::salvar): ao salvar uma receita montada
lançada por "Quantidade de Produtos", a Quantidade é só preview de quantas
produzir — a fornada rende 1, então `rendimento_qtd` é forçado a 1. Salvar a
Quantidade como rendimento dividiria o custo da unidade pela própria quantidade
(custo -> ~0) e furaria a produção (qtd_alvo/rendimento).

Trava o risco de DINHEIRO: custo unitário não pode regredir pra ~0.
"""
from app.extensions import db
from app.models import MateriaPrima, Receita
from app.services.custos import calcular_custos_receitas


def _login(client, admin_user):
    client.post('/auth/login',
                data={'login': admin_user.login, 'senha': '123'},
                follow_redirects=True)


def _receita_vazia(nome='Pain au chocolat Bicolor'):
    r = Receita(nome=nome, categoria='Viennoiserie', rendimento_qtd=1,
                rendimento_unidade='unidades', peso_base=1000.0)
    db.session.add(r)
    db.session.commit()
    return r


def _form_montada(modo, rendimento_qtd):
    """Form de uma receita montada: 100g massa (g) + 3 batons (un) + 2g cacau."""
    return {
        'nome': 'Pain au chocolat Bicolor',
        'categoria': 'Viennoiserie',
        'rendimento_qtd': str(rendimento_qtd),
        'rendimento_unidade': 'unidades',
        'peso_base': '1000',
        'modo_lancamento': modo,
        'ingrediente_tipo[]': ['mp_direto', 'mp_un', 'mp_direto'],
        'ingrediente_nome[]': ['Massa de Croissant', 'Baton Callebaut', 'Cacau'],
        'porcentagem[]': ['100', '3', '2'],
        'eh_base[]': ['0', '0', '0'],
        'nota[]': ['', '', ''],
    }


def test_montada_quantidade_forca_rendimento_1(app, admin_user):
    """Quantidade=100 em receita montada -> rendimento salvo = 1 (não 100)."""
    r = _receita_vazia()
    client = app.test_client()
    _login(client, admin_user)

    resp = client.post('/receitas/%d/salvar' % r.id,
                       data=_form_montada('quantidade', 100))
    assert resp.status_code in (200, 302)
    db.session.refresh(r)
    assert r.rendimento_qtd == 1


def test_montada_farinha_respeita_rendimento(app, admin_user):
    """Modo "Peso da Farinha" = lançamento por fornada: respeita o rendimento
    digitado (ex: bandeja que rende 12). Só o modo Quantidade força 1."""
    r = _receita_vazia('Bandeja Montada')
    client = app.test_client()
    _login(client, admin_user)

    client.post('/receitas/%d/salvar' % r.id,
                data=_form_montada('farinha', 12))
    db.session.refresh(r)
    assert r.rendimento_qtd == 12


def test_massa_percentual_quantidade_respeita_rendimento(app, admin_user):
    """Receita de massa (tem % de padeiro) por Quantidade: a fornada REND a
    quantidade pedida (escala pelo Peso Base), então rendimento = 100."""
    r = _receita_vazia('Pão de Forma')
    client = app.test_client()
    _login(client, admin_user)

    form = {
        'nome': 'Pão de Forma',
        'categoria': 'Paes',
        'rendimento_qtd': '100',
        'rendimento_unidade': 'unidades',
        'peso_base': '1000',
        'modo_lancamento': 'quantidade',
        'ingrediente_tipo[]': ['mp', 'mp'],
        'ingrediente_nome[]': ['Farinha', 'Água'],
        'porcentagem[]': ['100', '60'],
        'eh_base[]': ['1', '0'],
        'nota[]': ['', ''],
    }
    client.post('/receitas/%d/salvar' % r.id, data=form)
    db.session.refresh(r)
    assert r.rendimento_qtd == 100


def test_montada_quantidade_custo_unitario_intacto(app, admin_user):
    """DINHEIRO: salvar montada por Quantidade=100 NÃO pode zerar o custo
    unitário. Com rendimento forçado a 1, o custo/un fica no valor real da
    unidade (~R$ 7,84), não R$ 7,84/100."""
    db.session.add_all([
        MateriaPrima(nome='Massa de Croissant', unidade='g', custo_por_kg=35.43),
        MateriaPrima(nome='Baton Callebaut', unidade='un', custo_por_kg=1.40),
        MateriaPrima(nome='Cacau', unidade='g', custo_por_kg=50.40),
    ])
    r = _receita_vazia()
    db.session.commit()

    client = app.test_client()
    _login(client, admin_user)
    client.post('/receitas/%d/salvar' % r.id,
                data=_form_montada('quantidade', 100))
    db.session.refresh(r)
    assert r.rendimento_qtd == 1

    custos = calcular_custos_receitas()['custos']
    custo_un = custos['Pain au chocolat Bicolor']
    # 100g*0,03543 + 3*1,40 + 2*0,0504 = 3,543 + 4,20 + 0,1008 = 7,84
    assert custo_un > 7.0          # custo da unidade preservado
    assert abs(custo_un - 7.84) < 0.05
