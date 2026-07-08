"""Cadastro assistido por IA (08/07/2026): a IA propõe produtos a partir
de print/texto usando os parecidos já cadastrados; o humano revisa na tela
e só o POST de salvar grava. A Anthropic é SEMPRE mockada — componente
errado = baixa de estoque errada, então a sanitização contra o banco real
é o que estes testes travam.
"""
import json
from unittest.mock import MagicMock, patch

import pytest

from app.extensions import db
from app.models import MateriaPrima, Produto, ProdutoItem, Receita
from app.services import cadastro_ia as svc


class _FakeBlock:
    type = 'text'

    def __init__(self, text):
        self.text = text


def _fake_client(payload):
    client = MagicMock()
    resp = MagicMock()
    resp.content = [_FakeBlock(json.dumps(payload))]
    resp.usage = None
    client.messages.create.return_value = resp
    return client


@pytest.fixture
def base_catalogo(app):
    """Misto já cadastrado (pão de forma + queijo + presunto) — referência
    que a IA usaria para compor o Misto Cranberry."""
    pao = Receita(nome='Pao de Forma', categoria='Paes', rendimento_qtd=1,
                  rendimento_unidade='unidades', peso_base=1000)
    queijo = MateriaPrima(nome='Queijo prato fatiado', unidade='un',
                          custo_por_kg=2.0)
    misto = Produto(nome='Misto', categoria='Lanches', ativo=True,
                    preco_site=25.0)
    db.session.add_all([pao, queijo, misto])
    db.session.flush()
    db.session.add_all([
        ProdutoItem(produto_id=misto.id, tipo='receita', receita_id=pao.id,
                    item_nome=pao.nome, quantidade=2),
        ProdutoItem(produto_id=misto.id, tipo='mp',
                    materia_prima_id=queijo.id, item_nome=queijo.nome,
                    quantidade=2),
    ])
    db.session.commit()
    return {'pao': pao, 'queijo': queijo, 'misto': misto}


def test_analisar_sanitiza_contra_o_banco(app, base_catalogo, monkeypatch):
    """Id errado da IA é re-resolvido por nome; componente inexistente sem
    'novo' vira órfão sinalizado; produto homônimo marca ja_existe_id."""
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'sk-teste')
    payload = {'itens': [
        {'nome': 'Misto Cranberry', 'preco': 30.0, 'categoria': 'Lanches',
         'baseado_em': 'Misto', 'confianca': 'alta',
         'componentes': [
             # id inventado (999) mas nome existe -> re-resolve por nome
             {'tipo': 'receita', 'id': 999, 'nome': 'Pao de Forma',
              'quantidade': 2, 'novo': False},
             # MP nova de verdade
             {'tipo': 'mp', 'id': None, 'nome': 'Cranberry desidratada',
              'quantidade': 0.05, 'novo': True, 'unidade': 'kg'},
             # receita que nao existe e sem 'novo' MP -> orfao
             {'tipo': 'receita', 'id': None, 'nome': 'Pao de Cranberry',
              'quantidade': 2, 'novo': True},
         ]},
        {'nome': 'Misto', 'preco': 25.0, 'componentes': []},   # ja existe
    ]}
    with app.app_context():
        with patch('anthropic.Anthropic',
                   return_value=_fake_client(payload)):
            out = svc.analisar(texto='MISTO CRANBERRY R$ 30,00')
    assert 'erro' not in out
    novo, existente = out['itens']
    comps = novo['componentes']
    assert comps[0]['id'] == base_catalogo['pao'].id      # re-resolvido
    assert comps[0]['orfao'] is False
    assert comps[1]['novo'] is True and comps[1]['orfao'] is False
    # receita nova NAO e criavel automaticamente -> orfao
    assert comps[2]['novo'] is False and comps[2]['orfao'] is True
    assert existente['ja_existe_id'] == base_catalogo['misto'].id


def test_analisar_sem_api_key(app, monkeypatch):
    monkeypatch.delenv('ANTHROPIC_API_KEY', raising=False)
    with app.app_context():
        out = svc.analisar(texto='X')
    assert 'ANTHROPIC_API_KEY' in out['erro']


def test_analisar_json_invalido(app, monkeypatch):
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'sk-teste')
    client = MagicMock()
    resp = MagicMock()
    resp.content = [_FakeBlock('desculpa, nao consegui')]
    resp.usage = None
    client.messages.create.return_value = resp
    with app.app_context():
        with patch('anthropic.Anthropic', return_value=client):
            out = svc.analisar(texto='X')
    assert 'invalida' in out['erro']


def test_salvar_lote_cria_produto_mp_e_orfao(app, base_catalogo, admin_user):
    """Salvar cria o produto com FK certa por componente, cria a MP nova
    (custo 0 + aviso), deixa órfão o componente sem vínculo e pula o que
    já existe. Preço vai no campo escolhido."""
    itens = [
        {'nome': 'Misto Cranberry', 'preco': 30.0, 'categoria': 'Lanches',
         'componentes': [
             {'tipo': 'receita', 'id': base_catalogo['pao'].id,
              'nome': 'Pao de Forma', 'quantidade': 2},
             {'tipo': 'mp', 'id': None, 'nome': 'Cranberry desidratada',
              'quantidade': 0.05, 'novo': True, 'unidade': 'kg'},
             {'tipo': 'receita', 'id': None, 'nome': 'Pao de Cranberry',
              'quantidade': 2},
         ]},
        {'nome': 'Misto', 'preco': 25.0, 'componentes': []},
    ]
    with app.app_context():
        resumo = svc.salvar_lote(itens, 'preco_atacado', user=admin_user)
    assert resumo['criados'] == ['Misto Cranberry']
    assert resumo['pulados'] == ['Misto']
    assert resumo['mps_criadas'] == ['Cranberry desidratada']
    assert any('custo 0' in a for a in resumo['avisos'])
    assert any('sem vínculo' in a for a in resumo['avisos'])
    prod = Produto.query.filter_by(nome='Misto Cranberry').one()
    assert prod.preco_atacado == 30.0 and prod.preco_site is None
    por_nome = {pi.item_nome: pi for pi in prod.itens}
    assert por_nome['Pao de Forma'].receita_id == base_catalogo['pao'].id
    mp_nova = MateriaPrima.query.filter_by(
        nome='Cranberry desidratada').one()
    assert mp_nova.custo_por_kg == 0 and mp_nova.unidade == 'kg'
    assert por_nome['Cranberry desidratada'].materia_prima_id == mp_nova.id
    orfao = por_nome['Pao de Cranberry']
    assert orfao.tipo == 'receita' and orfao.receita_id is None


def test_salvar_lote_campo_preco_whitelist(app, admin_user):
    with app.app_context():
        with pytest.raises(ValueError):
            svc.salvar_lote([{'nome': 'X', 'preco': 1}], 'nome',
                            user=admin_user)


def test_rotas_analisar_e_salvar(app, admin_user, base_catalogo,
                                 monkeypatch):
    """Fluxo HTTP inteiro: analisar renderiza a revisão; salvar cria só o
    item marcado, com overrides de preço/quantidade aplicados."""
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'sk-teste')
    payload = {'itens': [
        {'nome': 'Queijo Quente Cranberry', 'preco': 32.0,
         'categoria': 'Lanches', 'confianca': 'alta',
         'componentes': [
             {'tipo': 'receita', 'id': base_catalogo['pao'].id,
              'nome': 'Pao de Forma', 'quantidade': 2, 'novo': False},
         ]},
    ]}
    c = app.test_client()
    c.post('/auth/login', data={'login': 'admin', 'senha': '123'})
    r0 = c.get('/produtos/cadastro-ia')
    assert r0.status_code == 200
    with patch('anthropic.Anthropic', return_value=_fake_client(payload)):
        r1 = c.post('/produtos/cadastro-ia/analisar',
                    data={'texto': 'QUEIJO QUENTE CRANBERRY R$ 32,00',
                          'campo_preco': 'preco_site'})
    corpo = r1.get_data(as_text=True)
    assert 'Queijo Quente Cranberry' in corpo
    assert 'Pao de Forma' in corpo

    it_json = json.dumps(payload['itens'][0])
    r2 = c.post('/produtos/cadastro-ia/salvar', data={
        'campo_preco': 'preco_site', 'n_itens': '1',
        'it0_incluir': '1', 'it0_json': it_json,
        'it0_nome': 'Queijo Quente Cranberry', 'it0_preco': '33,50',
        'it0_categoria': 'Lanches',
        'it0_c0_incluir': '1', 'it0_c0_qtd': '3',
    })
    assert r2.status_code == 302
    with app.app_context():
        prod = Produto.query.filter_by(
            nome='Queijo Quente Cranberry').one()
        assert prod.preco_site == 33.5                # override BR "33,50"
        assert prod.itens[0].quantidade == 3          # override de qtd
        assert prod.itens[0].receita_id == base_catalogo['pao'].id


def _hidden_por_nome(html):
    """Extrai os inputs hidden do HTML como um NAVEGADOR veria (o
    HTMLParser decodifica as entidades do atributo, igual ao browser).
    O bug crítico da revisão 08/07/2026 era exatamente aqui: tojson em
    atributo com aspas DUPLAS truncava o JSON no primeiro '\"'."""
    from html.parser import HTMLParser

    campos = {}

    class _P(HTMLParser):
        def handle_starttag(self, tag, attrs):
            a = dict(attrs)
            if tag == 'input' and a.get('type') == 'hidden':
                campos[a.get('name')] = a.get('value')
    _P().feed(html)
    return campos


def test_roundtrip_html_navegador_salva_de_verdade(app, admin_user,
                                                   base_catalogo,
                                                   monkeypatch):
    """Fluxo fiel ao navegador: o hidden it0_json é extraído do HTML
    RENDERIZADO (não montado na mão) e postado de volta. Trava o bug do
    tojson em atributo com aspas duplas (JSON truncado → nada salvava)."""
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'sk-teste')
    payload = {'itens': [
        {'nome': 'Misto "Especial" Cranberry', 'preco': 30.0,
         'categoria': 'Lanches', 'confianca': 'alta',
         'observacao': 'aspas & <tags> no nome pra estressar o escape',
         'componentes': [
             {'tipo': 'receita', 'id': base_catalogo['pao'].id,
              'nome': 'Pao de Forma', 'quantidade': 2, 'novo': False},
         ]},
    ]}
    c = app.test_client()
    c.post('/auth/login', data={'login': 'admin', 'senha': '123'})
    with patch('anthropic.Anthropic', return_value=_fake_client(payload)):
        r1 = c.post('/produtos/cadastro-ia/analisar',
                    data={'texto': 'x', 'campo_preco': 'preco_site'})
    hidden = _hidden_por_nome(r1.get_data(as_text=True))
    assert 'it0_json' in hidden
    # O navegador devolve o atributo decodificado — tem que ser JSON válido
    it = json.loads(hidden['it0_json'])
    assert it['nome'] == 'Misto "Especial" Cranberry'
    r2 = c.post('/produtos/cadastro-ia/salvar', data={
        'campo_preco': 'preco_site', 'n_itens': '1',
        'it0_incluir': '1', 'it0_json': hidden['it0_json'],
        'it0_nome': it['nome'], 'it0_preco': '30,00',
        'it0_categoria': 'Lanches', 'it0_c0_incluir': '1',
        'it0_c0_qtd': '2',
    })
    assert r2.status_code == 302
    with app.app_context():
        prod = Produto.query.filter_by(
            nome='Misto "Especial" Cranberry').one()
        assert prod.itens[0].receita_id == base_catalogo['pao'].id


def test_salvar_mp_homonima_arquivada_nao_explode(app, admin_user,
                                                  base_catalogo):
    """MP nova homônima de MP ARQUIVADA: não pode criar (unique do banco
    estouraria com 500) — vira aviso + componente órfão."""
    from app.utils import agora
    with app.app_context():
        db.session.add(MateriaPrima(nome='Cranberry desidratada',
                                    unidade='kg', custo_por_kg=80,
                                    arquivada_em=agora()))
        db.session.commit()
        itens = [{'nome': 'Misto Cranberry', 'preco': 30.0,
                  'componentes': [
                      {'tipo': 'mp', 'id': None,
                       'nome': 'Cranberry desidratada',
                       'quantidade': 1, 'novo': True, 'unidade': 'kg'}]}]
        resumo = svc.salvar_lote(itens, 'preco_site', user=admin_user)
        assert resumo['criados'] == ['Misto Cranberry']
        assert resumo['mps_criadas'] == []
        assert any('ARQUIVADA' in a for a in resumo['avisos'])
        assert MateriaPrima.query.filter_by(
            nome='Cranberry desidratada').count() == 1     # não duplicou
        prod = Produto.query.filter_by(nome='Misto Cranberry').one()
        assert prod.itens[0].materia_prima_id is None      # órfão


def test_rota_salvar_preco_invalido_nao_da_500(app, admin_user,
                                               base_catalogo):
    """Preço digitado inválido ("abc") mantém o proposto e avisa — nunca
    derruba a revisão com 500."""
    it_json = json.dumps({'nome': 'Pao Novo', 'preco': 40.0,
                          'componentes': []})
    c = app.test_client()
    c.post('/auth/login', data={'login': 'admin', 'senha': '123'})
    r = c.post('/produtos/cadastro-ia/salvar', data={
        'campo_preco': 'preco_site', 'n_itens': '1',
        'it0_incluir': '1', 'it0_json': it_json,
        'it0_nome': 'Pao Novo', 'it0_preco': 'abc',
    }, follow_redirects=True)
    assert r.status_code == 200
    with app.app_context():
        prod = Produto.query.filter_by(nome='Pao Novo').one()
        assert prod.preco_site == 40.0                     # manteve proposto


def test_rotas_exigem_admin(app, base_catalogo):
    """Funcionário comum não acessa nenhuma das três rotas (403)."""
    from app.models import Usuario
    with app.app_context():
        u = Usuario(nome='Func', login='func', papel='funcionario')
        u.set_senha('12345678')
        db.session.add(u)
        db.session.commit()
    c = app.test_client()
    c.post('/auth/login', data={'login': 'func', 'senha': '12345678'})
    assert c.get('/produtos/cadastro-ia').status_code == 403
    assert c.post('/produtos/cadastro-ia/analisar',
                  data={'texto': 'x'}).status_code == 403
    assert c.post('/produtos/cadastro-ia/salvar',
                  data={'n_itens': '0'}).status_code == 403


def test_rota_salvar_sem_marcados(app, admin_user):
    c = app.test_client()
    c.post('/auth/login', data={'login': 'admin', 'senha': '123'})
    r = c.post('/produtos/cadastro-ia/salvar',
               data={'campo_preco': 'preco_site', 'n_itens': '0'},
               follow_redirects=True)
    assert 'Nenhum item marcado' in r.get_data(as_text=True)
    with app.app_context():
        assert Produto.query.count() == 0
