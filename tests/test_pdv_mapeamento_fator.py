"""Fator de composicao no mapeamento de produto Seru.

Cobre o bug de estoque pego em 30/06/2026: "0,2" (virgula PT-BR) chegando no
endpoint virava 1.0 EM SILENCIO (float('0,2') levanta ValueError, e o except
caia pra 1.0) — uma venda de produto composto baixaria 5x o estoque correto.
O fator agora passa por `parse_fator_composicao`, que aceita virgula e REJEITA
valor invalido em vez de inventar 1.0.
"""
import pytest

# ── unidade: parse_fator_composicao ──────────────────────────────────────────

def test_parse_fator_aceita_ponto_e_virgula():
    from app.utils import parse_fator_composicao
    assert parse_fator_composicao('0.2') == pytest.approx(0.2)
    assert parse_fator_composicao('0,2') == pytest.approx(0.2)  # virgula PT-BR
    assert parse_fator_composicao('2') == pytest.approx(2.0)


def test_parse_fator_vazio_ou_none_cai_no_default():
    from app.utils import parse_fator_composicao
    assert parse_fator_composicao('') == 1.0
    assert parse_fator_composicao(None) == 1.0
    assert parse_fator_composicao('   ') == 1.0
    assert parse_fator_composicao('', default=3.0) == 3.0


def test_parse_fator_invalido_levanta_em_vez_de_silenciar():
    from app.utils import parse_fator_composicao
    # NUNCA cair pra 1.0 em silencio — baixaria estoque errado.
    with pytest.raises(ValueError):
        parse_fator_composicao('abc')
    with pytest.raises(ValueError):
        parse_fator_composicao('0')      # <= 0 invalido
    with pytest.raises(ValueError):
        parse_fator_composicao('-1')
    with pytest.raises(ValueError):
        parse_fator_composicao('0,0')


# ── integracao: POST /pdv/api/mapear ─────────────────────────────────────────

def _login_admin(app, admin_user):
    c = app.test_client()
    c.post('/auth/login', data={'login': 'admin', 'senha': '123'})
    return c


def test_api_mapear_grava_fator_com_virgula(app, admin_user, catalogo):
    """O bug central: "0,2" tem que virar 0.2 no banco, nao 1.0."""
    from app.models import SeruProdutoMap
    c = _login_admin(app, admin_user)
    rid = catalogo['receita'].id
    r = c.post('/pdv/api/mapear', data={
        'seru_nome': 'PERU INTEGRAL', 'acao': 'vincular',
        'alvo_tipo': 'receita', 'alvo_id': str(rid), 'fator': '0,2'})
    assert r.status_code == 200
    assert r.get_json()['ok'] is True
    with app.app_context():
        mp = SeruProdutoMap.query.filter_by(seru_nome='PERU INTEGRAL').first()
        assert mp is not None
        assert mp.receita_id == rid
        assert mp.fator_quantidade == pytest.approx(0.2)


def test_api_mapear_fator_invalido_retorna_400_sem_gravar(app, admin_user, catalogo):
    from app.models import SeruProdutoMap
    c = _login_admin(app, admin_user)
    rid = catalogo['receita'].id
    r = c.post('/pdv/api/mapear', data={
        'seru_nome': 'PERU INTEGRAL', 'acao': 'vincular',
        'alvo_tipo': 'receita', 'alvo_id': str(rid), 'fator': 'abc'})
    assert r.status_code == 400
    assert r.get_json()['ok'] is False
    with app.app_context():
        mp = SeruProdutoMap.query.filter_by(seru_nome='PERU INTEGRAL').first()
        # Nao pode ter sido vinculado com fator inventado.
        assert mp is None or mp.receita_id is None


def test_api_mapear_fator_zero_retorna_400(app, admin_user, catalogo):
    c = _login_admin(app, admin_user)
    rid = catalogo['receita'].id
    r = c.post('/pdv/api/mapear', data={
        'seru_nome': 'X', 'acao': 'vincular',
        'alvo_tipo': 'receita', 'alvo_id': str(rid), 'fator': '0'})
    assert r.status_code == 400


def test_api_mapear_sem_fator_usa_1(app, admin_user, catalogo):
    from app.models import SeruProdutoMap
    c = _login_admin(app, admin_user)
    rid = catalogo['receita'].id
    r = c.post('/pdv/api/mapear', data={
        'seru_nome': 'Pao Simples', 'acao': 'vincular',
        'alvo_tipo': 'receita', 'alvo_id': str(rid)})  # sem fator
    assert r.status_code == 200
    with app.app_context():
        mp = SeruProdutoMap.query.filter_by(seru_nome='Pao Simples').first()
        assert mp.fator_quantidade == pytest.approx(1.0)


def test_api_mapear_desfazer_reseta_fator(app, admin_user, catalogo):
    """desfazer volta o map pra pristine — fator nao pode ficar pegajoso."""
    from app.models import SeruProdutoMap
    c = _login_admin(app, admin_user)
    rid = catalogo['receita'].id
    c.post('/pdv/api/mapear', data={
        'seru_nome': 'PERU INTEGRAL', 'acao': 'vincular',
        'alvo_tipo': 'receita', 'alvo_id': str(rid), 'fator': '0,2'})
    r = c.post('/pdv/api/mapear', data={
        'seru_nome': 'PERU INTEGRAL', 'acao': 'desfazer'})
    assert r.status_code == 200
    with app.app_context():
        mp = SeruProdutoMap.query.filter_by(seru_nome='PERU INTEGRAL').first()
        assert mp.estado == 'pendente'
        assert mp.fator_quantidade == pytest.approx(1.0)


# ── integracao: POST /pdv/mapeamentos/produto/<id> (form) ────────────────────

def test_vincular_produto_form_grava_fator_virgula(app, admin_user, catalogo):
    from app.extensions import db
    from app.models import SeruProdutoMap
    with app.app_context():
        mp = SeruProdutoMap(seru_nome='PERU INTEGRAL')
        db.session.add(mp)
        db.session.commit()
        map_id = mp.id
    rid = catalogo['receita'].id
    c = _login_admin(app, admin_user)
    r = c.post(f'/pdv/mapeamentos/produto/{map_id}', data={
        'acao': 'vincular', 'alvo_tipo': 'receita',
        'alvo_id': str(rid), 'fator': '0,2'}, follow_redirects=False)
    assert r.status_code in (302, 303)
    with app.app_context():
        mp = SeruProdutoMap.query.get(map_id)
        assert mp.fator_quantidade == pytest.approx(0.2)


def test_vincular_produto_form_fator_invalido_nao_altera(app, admin_user, catalogo):
    from app.extensions import db
    from app.models import SeruProdutoMap
    with app.app_context():
        mp = SeruProdutoMap(seru_nome='PERU INTEGRAL')
        db.session.add(mp)
        db.session.commit()
        map_id = mp.id
    rid = catalogo['receita'].id
    c = _login_admin(app, admin_user)
    r = c.post(f'/pdv/mapeamentos/produto/{map_id}', data={
        'acao': 'vincular', 'alvo_tipo': 'receita',
        'alvo_id': str(rid), 'fator': 'abc'}, follow_redirects=False)
    assert r.status_code in (302, 303)
    with app.app_context():
        mp = SeruProdutoMap.query.get(map_id)
        # Rejeitou antes de mexer no vinculo.
        assert mp.receita_id is None
        assert mp.estado == 'pendente'


def test_vincular_produto_form_desfazer_reseta_fator(app, admin_user, catalogo):
    from app.extensions import db
    from app.models import SeruProdutoMap
    rid = catalogo['receita'].id
    with app.app_context():
        mp = SeruProdutoMap(seru_nome='PERU INTEGRAL', receita_id=rid,
                            fator_quantidade=0.2)
        db.session.add(mp)
        db.session.commit()
        map_id = mp.id
    c = _login_admin(app, admin_user)
    c.post(f'/pdv/mapeamentos/produto/{map_id}', data={'acao': 'desfazer'},
           follow_redirects=False)
    with app.app_context():
        mp = SeruProdutoMap.query.get(map_id)
        assert mp.estado == 'pendente'
        assert mp.fator_quantidade == pytest.approx(1.0)


# ── consolidacao: as 3 telas usam o widget unico PdvMap ──────────────────────

def test_telas_de_mapeamento_carregam_widget_unico(app, admin_user, catalogo):
    """itens-vendidos, mapeamentos e reconciliacao referenciam o mesmo modulo
    (pdv_mapeamento.js) — a fonte unica de fator/fatias/save."""
    c = _login_admin(app, admin_user)
    for rota in ('/pdv/itens-vendidos', '/pdv/mapeamentos'):
        r = c.get(rota)
        assert r.status_code == 200, rota
        assert b'pdv_mapeamento.js' in r.data, f'{rota} sem o modulo'
        assert b'PdvMap' in r.data, f'{rota} nao usa PdvMap'


def test_reconciliacao_usa_widget_unico(app, admin_user, catalogo):
    from unittest.mock import patch
    c = _login_admin(app, admin_user)
    agg = {'total_pedidos': 1, 'total_itens_vendidos': 1, 'faturamento_total': 1.0,
           'produtos': [{'nome': 'X', 'sku': '1', 'qtd': 1, 'faturamento': 1.0,
                         'estado_map': 'pendente'}]}
    with patch('app.services.vendas_itens.agregar_itens', return_value=agg):
        r = c.get('/pdv/reconciliacao')
    assert r.status_code == 200
    assert b'pdv_mapeamento.js' in r.data
    assert b'PdvMap.salvar' in r.data


# ── regressao de comportamento do widget JS (roda se node existir) ───────────

_JS_HARNESS = r'''
global.window = {};
require(process.argv[1]);
const P = global.window.PdvMap, assert = require('assert');
assert.strictEqual(P.parseFator('0,2'), 0.2);
assert.strictEqual(P.parseFator(''), 1);
assert.strictEqual(P.parseFator('abc'), 1);
assert.strictEqual(P.parseFator('0'), 1);
assert.strictEqual(P.fatiasParaFator(2, 10), '0.2');
assert.strictEqual(P.fatiasParaFator(0, 10), '');
assert.strictEqual(P.fatiasParaFator(5, 5), '1');
assert.strictEqual(P.fatorHelp(1).tone, 'muted');
assert.ok(P.fatorHelp(0.2).text.indexOf('5 vendas') >= 0);
const idx = P.construirIndiceAlvo(
    [{id:1,nome:'Pao'},{id:2,nome:'Dup'},{id:4,nome:'Dup'}], [{id:3,nome:'Rec'}]);
assert.strictEqual(P.resolverAlvo('Pao', idx), 'produto:1');
assert.strictEqual(P.resolverAlvo('Dup', idx), null);            // ambiguo -> null
assert.strictEqual(P.resolverAlvo('Dup — produto #4', idx), 'produto:4');
assert.strictEqual(P.resolverAlvo('Rec', idx), 'receita:3');
console.log('ok');
'''


def test_pdv_map_js_comportamento():
    """Trava de regressao do widget: fator com virgula, fatias X/Y, e o alvo
    ambiguo (dois itens com mesmo nome) que NAO pode resolver pro errado —
    baixaria estoque no alvo errado. Pula se node nao estiver instalado."""
    import os
    import shutil
    import subprocess
    node = shutil.which('node')
    if not node:
        pytest.skip('node nao instalado')
    modulo = os.path.join(os.path.dirname(__file__), '..', 'app', 'static',
                          'js', 'pdv_mapeamento.js')
    r = subprocess.run([node, '-e', _JS_HARNESS, os.path.abspath(modulo)],
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, f'JS falhou:\n{r.stdout}\n{r.stderr}'
    assert 'ok' in r.stdout
