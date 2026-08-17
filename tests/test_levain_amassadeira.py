"""Sub-receita que ENTRA NA AMASSADEIRA (`Receita.sub_na_amassadeira`).

Caso real 15/07/2026: o dono converteu o Levain de MP pra sub-receita
"Levain (pé)" nas fichas dos sourdoughs — e a quantidade de levain SUMIU da
massa base da TV do padeiro (a cascata exclui sub-receitas de propósito,
regra criada pras subs de MONTAGEM dos Danish). A flag diferencia os dois
casos: sub de amassadeira entra na massa em gramas (qtd × peso_unitario) e
o rendimento volta a ser massa/peso; sub de montagem segue fora.
"""
import pytest

from app.extensions import db
from app.models import MassaBase, MassaBaseItem, Receita, ReceitaIngrediente


@pytest.fixture(autouse=True)
def _hoje_e_segunda_fixa(congela_hoje):
    """Producao so seg-sex (dono 17/08/2026) tornou o shaping do cronograma
    sensivel ao dia da semana — congela hoje() numa SEGUNDA pros cenarios
    hoje()+N deste arquivo cairem sempre em dia util, em qualquer dia em que
    a suite rode (ver conftest.congela_hoje)."""
    congela_hoje()


def _receita(nome, peso_base=1000.0, peso_unitario=500.0, rendimento=3.0,
             **kw):
    r = Receita(nome=nome, categoria='Paes', rendimento_qtd=rendimento,
                rendimento_unidade='un', peso_base=peso_base,
                peso_unitario=peso_unitario, **kw)
    db.session.add(r)
    db.session.flush()
    return r


def _ing(receita, nome, tipo, qtd, sub=None):
    db.session.add(ReceitaIngrediente(
        receita_id=receita.id, ingrediente_nome=nome, tipo=tipo,
        porcentagem=qtd, sub_receita_id=(sub.id if sub else None)))


def _sourdough_com_levain(nome='Sourdough Flag'):
    """Sourdough consumindo Levain (pé) como sub-receita de amassadeira
    (espelho da ficha real: farinha 100%, água 80%, levain 200 un de 1 g)."""
    levain = _receita('Levain (pé) Teste', peso_unitario=1.0,
                      rendimento=2600.0, sub_na_amassadeira=True)
    sd = _receita(nome)
    _ing(sd, 'Farinha', 'mp', 100.0)
    _ing(sd, 'Agua', 'mp', 80.0)
    _ing(sd, 'Levain (pé) Teste', 'receita', 200.0, sub=levain)
    _ing(sd, 'Sal', 'mp', 2.0)
    db.session.commit()
    return sd, levain


def test_sub_de_amassadeira_entra_na_porcao_em_gramas(app):
    from app.services.massa_base import ingredientes_por_porcao
    with app.app_context():
        sd, _ = _sourdough_com_levain()
        porcao = ingredientes_por_porcao(sd)
        # 200 unidades × 1 g = 200 g de levain na massa branca.
        assert porcao['Levain (pé) Teste'] == 200.0
        assert porcao['Farinha'] == 1000.0
        assert porcao['Agua'] == 800.0


def test_rendimento_volta_a_ser_massa_por_peso(app):
    """Com a flag, a sub não conta como 'montagem': rendimento = massa total
    (1000+800+200+20) / 500 g = 4,04 — e não o cadastrado (3). Sem isso o
    MRP inflava ~25% a massa dos sourdoughs."""
    from app.services.massa_base import rendimento_massa_crua
    with app.app_context():
        sd, _ = _sourdough_com_levain('Sourdough Rend')
        assert abs(rendimento_massa_crua(sd) - 2020.0 / 500.0) < 0.01


def test_sub_de_montagem_segue_fora_e_rendimento_cadastrado(app):
    """Regressão do caso Danish (30/06): sub SEM a flag continua fora da
    massa e o rendimento continua sendo o cadastrado."""
    from app.services.massa_base import (
        ingredientes_por_porcao,
        rendimento_massa_crua,
    )
    with app.app_context():
        folhar = _receita('Massa Folhar Teste', peso_unitario=3580.0,
                          sub_na_amassadeira=False)
        danish = _receita('Danish Teste', peso_unitario=150.0,
                          rendimento=31.0)
        _ing(danish, 'Recheio', 'mp', 100.0)
        _ing(danish, 'Massa Folhar Teste', 'receita', 0.032, sub=folhar)
        db.session.commit()
        assert 'Massa Folhar Teste' not in ingredientes_por_porcao(danish)
        assert rendimento_massa_crua(danish) == 31.0


def test_cascata_da_massa_base_mostra_o_levain(app):
    """O caso do dono: dois sourdoughs no mesmo grupo de massa base — o
    levain (comum aos dois) tem que aparecer na BASE da cascata."""
    from app.services.massa_base import calcular_cascata
    with app.app_context():
        sd1, levain = _sourdough_com_levain('Sourdough Casc A')
        sd2 = _receita('Sourdough Casc B')
        _ing(sd2, 'Farinha', 'mp', 100.0)
        _ing(sd2, 'Agua', 'mp', 85.0)
        _ing(sd2, 'Levain (pé) Teste', 'receita', 200.0, sub=levain)
        _ing(sd2, 'Sal', 'mp', 2.0)
        mb = MassaBase(nome='Sourdoughs Teste')
        db.session.add(mb)
        db.session.flush()
        db.session.add_all([
            MassaBaseItem(massa_base_id=mb.id, receita_id=sd1.id, ordem=0),
            MassaBaseItem(massa_base_id=mb.id, receita_id=sd2.id, ordem=1),
        ])
        db.session.commit()
        casc = calcular_cascata(mb)
        assert casc['base'].get('Levain (pé) Teste') == 200.0


def test_mise_en_place_mostra_levain_em_gramas(app):
    from app.services.producao import mise_en_place
    with app.app_context():
        sd, _ = _sourdough_com_levain('Sourdough Mep')
        mep = mise_en_place(sd, 8)   # 8 un / (2020/500=4,04 un por porção)
        lev = next(i for i in mep['ingredientes']
                   if 'Levain' in i['nome'])
        assert lev['unidade'] == 'g'
        # mult = 8 / 4,04 ≈ 1,98 porções → ~396 g de levain.
        assert 390 <= lev['qtd'] <= 400


def test_ficha_salva_a_flag(app, admin_user):
    with app.app_context():
        r = _receita('Levain Flag UI', peso_unitario=1.0)
        db.session.commit()
        rid = r.id
    c = app.test_client()
    c.post('/auth/login', data={'login': 'admin', 'senha': '123'})
    resp = c.post(f'/receitas/{rid}/salvar', data={
        'nome': 'Levain Flag UI', 'categoria': 'Paes',
        'rendimento_qtd': '2600', 'rendimento_unidade': 'un',
        'peso_base': '1000', 'sub_na_amassadeira': '1',
    }, follow_redirects=True)
    assert resp.status_code == 200
    with app.app_context():
        assert Receita.query.get(rid).sub_na_amassadeira is True
