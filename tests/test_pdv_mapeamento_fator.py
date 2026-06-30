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
