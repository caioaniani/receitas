"""Configuração dos planos com opções por grupo, sem alterar compras antigas."""
from decimal import Decimal

import pytest
from test_kits_sucos_owner import catalogo_sucos as catalogo_sucos
from test_kits_sucos_owner import cliente_owner, dados

from app.extensions import db
from app.models import KitCafe, KitCafeOpcao, Produto, Receita
from app.services import kits_cafe


@pytest.fixture
def opcoes(app):
    receitas = [Receita(nome=nome, rendimento_qtd=1, rendimento_unidade='un',
                        peso_base=100, site_ativo=True, preco_site=Decimal(preco))
                for nome, preco in [('Almond', '34.50'), ('Tradicional', '34.50'),
                                    ('Integral', '36.00')]]
    recheado = Produto(nome='Nutella com morango', ativo=True, site_ativo=True,
                       preco_site=Decimal('35.50'))
    db.session.add_all([*receitas, recheado])
    db.session.commit()
    return {'croissant': [f'receita:{receitas[0].id}', f'produto:{recheado.id}'],
            'sourdough': [f'receita:{r.id}' for r in receitas[1:]]}


def _form(catalogo, opcoes):
    return {**dados(catalogo), 'configurar_opcoes': '1',
            **{f'opcao_{grupo}': valores for grupo, valores in opcoes.items()}}


def test_salva_reabre_preserva_e_remove_grupos(app, owner_user, catalogo_sucos, opcoes):
    cliente = cliente_owner(app, owner_user)
    form = _form(catalogo_sucos, opcoes)
    resposta = cliente.post('/admin/kits-cafe/salvar', data=form)
    assert resposta.status_code == 302
    kit = KitCafe.query.one()
    assert KitCafeOpcao.query.count() == 4
    assert kits_cafe.preco_kit(kit) == Decimal('129.50')
    html = cliente.get(resposta.location).get_data(as_text=True)
    assert 'name="opcao_croissant"' in html and 'name="opcao_sourdough"' in html
    form['kit_id'] = str(kit.id)
    form.pop('configurar_opcoes')
    form.pop('opcao_croissant')
    form.pop('opcao_sourdough')
    assert cliente.post('/admin/kits-cafe/salvar', data=form).status_code == 302
    assert len(kit.opcoes) == 4  # Integração legada não apaga escolhas.
    form['configurar_opcoes'] = '1'
    assert cliente.post('/admin/kits-cafe/salvar', data=form).status_code == 302
    assert not kit.opcoes
    assert kits_cafe.preco_kit(kit) == Decimal('60.50')


@pytest.mark.parametrize('alteracao', ['unica', 'duplicada', 'fixo', 'grupo_estranho'])
def test_opcoes_invalidas_nao_criam_kit(app, owner_user, catalogo_sucos, opcoes, alteracao):
    form = _form(catalogo_sucos, opcoes)
    if alteracao == 'unica':
        form['opcao_croissant'] = opcoes['croissant'][:1]
    elif alteracao == 'duplicada':
        form['opcao_croissant'] = [opcoes['croissant'][0]] * 2
    elif alteracao == 'fixo':
        form['opcao_croissant'] = [f'receita:{catalogo_sucos[0].id}', opcoes['croissant'][1]]
    else:
        form['opcao_outro'] = ['receita:1', 'receita:2']
    assert cliente_owner(app, owner_user).post('/admin/kits-cafe/salvar', data=form).status_code == 400
    assert KitCafe.query.count() == KitCafeOpcao.query.count() == 0


def test_mover_opcao_entre_grupos_reutiliza_registros(app, owner_user, catalogo_sucos, opcoes):
    cliente = cliente_owner(app, owner_user)
    form = _form(catalogo_sucos, opcoes)
    assert cliente.post('/admin/kits-cafe/salvar', data=form).status_code == 302
    kit = KitCafe.query.one()
    anteriores = {o.id for o in kit.opcoes}
    form.update(kit_id=str(kit.id), opcao_croissant=opcoes['sourdough'],
                opcao_sourdough=opcoes['croissant'])
    assert cliente.post('/admin/kits-cafe/salvar', data=form).status_code == 302
    assert {o.id for o in kit.opcoes} == anteriores


def test_admin_sem_owner_nao_configura_grupos(app, admin_user, catalogo_sucos, opcoes):
    cliente = cliente_owner(app, admin_user)
    assert cliente.post('/admin/kits-cafe/salvar', data=_form(catalogo_sucos, opcoes)).status_code == 403
    assert KitCafe.query.count() == 0
