"""Impostos sobre venda (PIS/COFINS/ICMS) nas margens — pedido do dono
13/07/2026 (planilha: 1,65% + 7,6% + 4,0% = 13,25% sobre o preço).

Fonte única em app/services/impostos.py; margem exibida vira LÍQUIDA em
/rentabilidade, relatório de custos, dashboard, api margem-categoria,
copilot e ficha (JS). Alíquotas em AppConfig, editáveis em /rentabilidade.
"""
import pytest

from app.extensions import db
from app.models import AppConfig, Receita
from app.services import impostos


def test_aliquotas_padrao_do_dono(app):
    a = impostos.aliquotas()
    assert a['pis'] == 1.65
    assert a['cofins'] == 7.6
    assert a['icms'] == 4.0
    assert a['total'] == 13.25
    assert impostos.carga_venda() == pytest.approx(0.1325)


def test_aliquota_do_appconfig_sobrepoe_padrao(app):
    AppConfig.set('imposto_icms_pct', 18.0)
    db.session.commit()
    a = impostos.aliquotas()
    assert a['icms'] == 18.0
    assert a['total'] == pytest.approx(27.25)


def test_aliquota_invalida_no_banco_cai_no_padrao(app):
    """Lixo no AppConfig nunca quebra tela de relatório."""
    AppConfig.set('imposto_pis_pct', 'banana')
    AppConfig.set('imposto_cofins_pct', '120')   # acima do teto de sanidade
    db.session.commit()
    a = impostos.aliquotas()
    assert a['pis'] == 1.65
    assert a['cofins'] == 7.6


def test_salvar_aliquotas_valida(app):
    a = impostos.salvar_aliquotas('2,0', '8.0', '12')   # aceita vírgula
    assert a['total'] == 22.0
    with pytest.raises(ValueError):
        impostos.salvar_aliquotas('abc', 7.6, 4.0)
    with pytest.raises(ValueError):
        impostos.salvar_aliquotas(1.65, 7.6, 120)       # acima do teto


def test_margem_liquida_matematica(app):
    carga = impostos.carga_venda()          # 0.1325
    # preço 10, custo 2: líquido = 10×0,8675 − 2 = 6,675 → 66,75%
    assert impostos.lucro_liquido(10, 2, carga) == pytest.approx(6.675)
    assert impostos.margem_liquida(10, 2, carga) == pytest.approx(66.75)
    assert impostos.margem_liquida(0, 2, carga) is None
    assert impostos.lucro_liquido(None, 2, carga) is None


def _login_admin(app, admin_user):
    client = app.test_client()
    client.post('/auth/login', data={'login': admin_user.login, 'senha': '123'},
                follow_redirects=True)
    return client


def test_rentabilidade_mostra_margem_liquida(app, admin_user):
    r = Receita(nome='Pao Imposto', categoria='Paes', rendimento_qtd=1,
                rendimento_unidade='un', peso_base=1000.0, preco_loja=10.0)
    db.session.add(r)
    db.session.commit()
    client = _login_admin(app, admin_user)
    html = client.get('/rentabilidade').get_data(as_text=True)
    assert 'Margem líq.' in html
    assert '13.25' in html or '13,25' in html     # banner com a carga
    # custo 0, preço 10 → margem líquida 86,8% (não os 100% brutos)
    assert '86.8' in html or '86,8' in html


def test_rentabilidade_post_atualiza_aliquotas(app, admin_user):
    client = _login_admin(app, admin_user)
    resp = client.post('/rentabilidade/impostos',
                       data={'pis': '1.65', 'cofins': '7.6', 'icms': '18'},
                       follow_redirects=True)
    assert resp.status_code == 200
    assert impostos.aliquotas()['icms'] == 18.0

    # inválida não persiste (flash de aviso, valores intactos)
    client.post('/rentabilidade/impostos',
                data={'pis': 'x', 'cofins': '7.6', 'icms': '4'},
                follow_redirects=True)
    assert impostos.aliquotas()['icms'] == 18.0


def test_copilot_consultar_margem_liquida(app, admin_user):
    from app.services.copilot import _read_consultar_margem
    r = Receita(nome='Pao Margem Bot', categoria='Paes', rendimento_qtd=1,
                rendimento_unidade='un', peso_base=1000.0, preco_loja=10.0)
    db.session.add(r)
    db.session.commit()
    out = _read_consultar_margem({'nome': 'Pao Margem Bot'}, admin_user)
    assert 'Impostos sobre venda: 13.25%' in out['texto']
    assert 'margem líq. 86.8%' in out['texto']


def test_api_margem_categoria_liquida(app, admin_user):
    r = Receita(nome='Pao Cat Imposto', categoria='CatImposto',
                rendimento_qtd=1, rendimento_unidade='un', peso_base=1000.0,
                preco_venda=10.0)
    db.session.add(r)
    db.session.commit()
    client = _login_admin(app, admin_user)
    d = client.get('/relatorios/dashboards/api/margem-categoria').get_json()
    i = d['labels'].index('CatImposto')
    assert d['valores'][i] == pytest.approx(86.8, abs=0.05)
