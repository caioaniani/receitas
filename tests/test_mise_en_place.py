"""Modal mise en place: receita escalada pra quantidade de produção.

mise_en_place(receita, unidades) ajusta cada ingrediente (farinha, água...)
pro total a produzir; endpoint /padeiro-testes/receita/<id>.json serve isso.
"""
from app.extensions import db
from app.models import Receita, ReceitaIngrediente
from app.services.producao import mise_en_place


def _receita_pao():
    r = Receita(nome='Pão Francês', categoria='Paes', rendimento_qtd=10,
                rendimento_unidade='un', peso_base=1000.0,
                modo_preparo='Misturar tudo.\n\nFermentar 48h.')
    db.session.add(r)
    db.session.flush()
    db.session.add(ReceitaIngrediente(receita_id=r.id, tipo='mp',
                                      ingrediente_nome='Farinha', porcentagem=100))
    db.session.add(ReceitaIngrediente(receita_id=r.id, tipo='mp',
                                      ingrediente_nome='Água', porcentagem=60))
    db.session.commit()
    return r


def test_mise_en_place_escala(app):
    r = _receita_pao()
    # 20 un -> mult 2 (rendimento 10); farinha 1000g×2 = 2000g
    mep = mise_en_place(r, 20)
    assert mep['unidades'] == 20
    assert mep['farinha_g'] == 2000.0
    ings = {i['nome']: i for i in mep['ingredientes']}
    assert ings['Farinha']['qtd'] == 2000.0      # 100% de 1000 × 2
    assert ings['Água']['qtd'] == 1200.0         # 60% de 1000 × 2
    assert ings['Água']['pct'] == 60
    assert len(mep['etapas']) == 2               # 2 etapas do modo de preparo


def test_mise_en_place_197(app):
    """197 paes (o caso do usuario): farinha escalada proporcional."""
    r = _receita_pao()
    mep = mise_en_place(r, 197)
    # mult = 197/10 = 19.7; farinha = 1000 × 19.7 = 19700g
    assert mep['farinha_g'] == 19700.0


def test_rota_receita_mise(app, admin_user):
    r = _receita_pao()
    client = app.test_client()
    client.post('/auth/login', data={'login': admin_user.login, 'senha': '123'},
                follow_redirects=True)
    resp = client.get('/padeiro-testes/receita/%d.json?unidades=20' % r.id)
    assert resp.status_code == 200
    data = resp.get_json()
    assert data['nome'] == 'Pão Francês'
    assert data['unidades'] == 20
    assert data['farinha_g'] == 2000.0
