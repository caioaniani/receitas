"""Arquivar MP = tirar de CIRCULAÇÃO (autocompletes, matchers, pickers, telas
de pedido) preservando o histórico — destino da MP que virou receita mas tem
histórico inapagável (movimentações/preço). Reversível. Leituras de custeio
por nome continuam enxergando a MP arquivada (ficha antiga não quebra)."""
from app.extensions import db
from app.models import EstoqueLoja, Loja, MateriaPrima
from app.utils import agora


def _mp(nome='Geleia Artesanal de Morango', arquivada=False, sugerir=False):
    m = MateriaPrima(nome=nome, unidade='un', custo_por_kg=4.64,
                     sugerir_pedido_loja=sugerir,
                     arquivada_em=agora() if arquivada else None)
    db.session.add(m)
    db.session.commit()
    return m


def _login(app, admin_user):
    c = app.test_client()
    c.post('/auth/login', data={'login': admin_user.login, 'senha': '123'},
           follow_redirects=True)
    return c


def test_arquivar_e_desarquivar(app, admin_user):
    m = _mp(sugerir=True)
    c = _login(app, admin_user)
    resp = c.post(f'/materias-primas/arquivar/{m.id}', follow_redirects=True)
    assert resp.status_code == 200
    m = db.session.get(MateriaPrima, m.id)
    assert m.arquivada_em is not None
    assert m.arquivada_por_id == admin_user.id
    assert m.sugerir_pedido_loja is False        # sai das telas de pedido junto
    # reversível
    c.post(f'/materias-primas/arquivar/{m.id}', follow_redirects=True)
    assert db.session.get(MateriaPrima, m.id).arquivada_em is None


def test_arquivada_some_dos_matchers_e_pickers(app, admin_user):
    """Ninguém consegue CONECTAR nada numa MP arquivada: copilot (_resolver_mp),
    catálogos de entrada/conferência em lote, e o form de novo pedido."""
    from app.services.copilot import _resolver_mp
    from app.services.estoque_loja_lote import _carregar_catalogo
    m = _mp(arquivada=True)
    viva = _mp('Farinha Viva')

    assert _resolver_mp('Geleia Artesanal de Morango') == []      # copilot não acha
    assert _resolver_mp('Farinha Viva') != []                     # viva acha

    loja = Loja(nome='Centro', ativa=True)
    db.session.add(loja)
    db.session.commit()
    _, _, materias, _, _ = _carregar_catalogo(loja.id)
    nomes = [nome for _, nome, _ in materias]
    assert 'Geleia Artesanal de Morango' not in nomes             # lote não casa
    assert 'Farinha Viva' in nomes

    c = _login(app, admin_user)
    body = c.get('/pedidos/novo').get_data(as_text=True)
    assert 'Geleia Artesanal de Morango' not in body              # picker de pedido
    assert 'Farinha Viva' in body


def test_arquivada_fora_do_datalist_global(app, admin_user):
    """O datalist mp-list (base.html, usado por fichas/cestas/transferências)
    não oferece MP arquivada."""
    _mp(arquivada=True)
    _mp('Farinha Viva')
    c = _login(app, admin_user)
    body = c.get('/materias-primas/').get_data(as_text=True)
    # o datalist global vem no base.html de qualquer página autenticada
    import re
    datalist = re.search(r'<datalist id="mp-list">(.*?)</datalist>', body,
                         re.S)
    assert datalist is not None
    assert 'Geleia Artesanal de Morango' not in datalist.group(1)
    assert 'Farinha Viva' in datalist.group(1)


def test_banco_separa_arquivadas_com_desarquivar(app, admin_user):
    app.config['UI_V2_ENABLED'] = False  # contrato da tela CLASSICA (viva via cookie ui_classic/?legacy=1)
    _mp(arquivada=True)
    _mp('Farinha Viva')
    c = _login(app, admin_user)
    body = c.get('/materias-primas/').get_data(as_text=True)
    assert 'Arquivadas (1)' in body
    assert 'desarquivar' in body


def test_arquivada_fora_da_tela_de_pedidos_por_venda(app):
    """Mesmo com checkbox ligado no passado, MP arquivada não entra na grade
    de pedidos por venda+estoque."""
    from datetime import datetime, time, timedelta

    from app.models import MovEstoqueLoja
    from app.services.previsao_producao import sugerir_pedidos_por_venda
    from app.utils import hoje
    m = _mp(sugerir=True)
    m.arquivada_em = agora()                     # arquivada DEPOIS do checkbox
    loja = Loja(nome='Centro', ativa=True)
    db.session.add(loja)
    db.session.flush()
    el = EstoqueLoja(loja_id=loja.id, materia_prima_id=m.id, quantidade=10)
    db.session.add(el)
    db.session.flush()
    db.session.add(MovEstoqueLoja(
        estoque_loja_id=el.id, tipo='venda_seru', quantidade=5,
        data=datetime.combine(hoje() - timedelta(days=7), time(12, 0)),
        referencia='t'))
    db.session.commit()
    grade = sugerir_pedidos_por_venda(horizonte_dias=7, janela_semanas=6)
    for lj in grade['lojas']:
        assert all(p.get('materia_prima_id') != m.id for p in lj['produtos'])


def test_custeio_por_nome_continua_enxergando_arquivada(app):
    """Ficha antiga que usa a MP arquivada como ingrediente NÃO quebra: o mapa
    de custos (leitura) continua incluindo a MP arquivada."""
    from app.services.custos import calcular_custos_receitas
    _mp('Essencia Antiga', arquivada=True)
    res = calcular_custos_receitas()
    assert 'Essencia Antiga' in res['mp_info']   # leitura NÃO filtra arquivada
