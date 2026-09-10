"""A visão macro mostra a equipe real, sem promover ou expor dados a outros papéis."""
from datetime import date

import pytest
from flask import g

from app.extensions import db
from app.models import (
    Cargo,
    Funcionario,
    Loja,
    PlanoCarreiraCargoVinculo,
    PlanoCarreiraEnquadramento,
    PlanoCarreiraFaixa,
    PlanoCarreiraImportacao,
    RhMovimentacao,
)
from app.services.rh_equipe import carregar_visao


def _client(app, user=None):
    # A fixture mantém o app_context: não compartilhar o cache entre usuários.
    g.pop('_login_user', None)
    client = app.test_client()
    if user is None:
        return client
    with client.session_transaction() as session:
        session['_user_id'] = str(user.id)
        session['_fresh'] = True
    return client


def _dados():
    cargo = Cargo(nome='ATENDENTE', salario_base=2330.4)
    cargo2 = Cargo(nome='Atendente 2', salario_base=2500)
    loja = Loja(nome='Unidade Teste', ativa=True)
    db.session.add_all([cargo, cargo2, loja])
    db.session.flush()
    lider = Funcionario(nome='Líder de teste', cpf='71000000001', ativo=True)
    ana = Funcionario(nome='Ana de teste', cpf='71000000002', cargo=cargo,
                      lider=lider, lojas=[loja], periodo='Manhã', ativo=True,
                      data_admissao=date(2020, 1, 1))
    bia = Funcionario(nome='Bia de teste', cpf='71000000003', cargo=cargo2, ativo=False)
    db.session.add_all([lider, ana, bia])
    imp = PlanoCarreiraImportacao(nome_arquivo='teste.xlsx', sha256='0' * 64)
    db.session.add(imp)
    db.session.flush()
    faixa = PlanoCarreiraFaixa(importacao_id=imp.id, familia='Atendimento',
                               nivel=2, cargo_proposto='Atendente 2')
    db.session.add(faixa)
    db.session.flush()
    db.session.add(PlanoCarreiraCargoVinculo(cargo=cargo2, faixa=faixa))
    db.session.add(PlanoCarreiraEnquadramento(
        importacao_id=imp.id, funcionario_id=ana.id, familia='Atendimento',
        nivel=2, cargo_proposto='Atendente 2', decisao='Em avaliação'))
    db.session.commit()
    return ana, bia, lider, loja


def test_macro_inclui_sem_login_nao_usa_nivel_proposto(app):
    ana, bia, lider, loja = _dados()
    visao = carregar_visao({'ativos': '1'})
    assert visao['resumo']['total'] == 2
    assert visao['resumo']['cargos'] == 1  # sem cargo não é um cargo ocupado
    linha = next(r for r in visao['linhas'] if r['pessoa'].id == ana.id)
    assert ana.usuario_id is None
    assert linha['cargo_nome'] == 'Atendente 1'
    assert linha['nivel'] == 1
    assert linha['lider_nome'] == lider.nome
    assert linha['unidades'] == [loja]
    assert linha['ultima_promocao'] is None
    assert RhMovimentacao.query.count() == 0
    assert ana.cargo.nome == 'ATENDENTE' and ana.salario_efetivo() == 2330.4


def test_filtros_conjugados_e_inativos(app):
    ana, bia, lider, loja = _dados()
    filtros = {'q': 'ana', 'cargo': 'atendente 1', 'loja': str(loja.id),
               'lider': str(lider.id), 'nivel': '1', 'ativos': '1'}
    assert [r['pessoa'].id for r in carregar_visao(filtros)['linhas']] == [ana.id]
    assert carregar_visao(filtros | {'nivel': '2'})['linhas'] == []
    assert carregar_visao({'ativos': '0'})['resumo']['total'] == 3
    assert [r['pessoa'].id for r in carregar_visao({'pendencia': 'sem_lider'})['linhas']] == [lider.id]


@pytest.mark.parametrize('path', ['/rh/equipe', '/rh/cargos/unificar-atendentes'])
def test_macro_owner_only(app, owner_user, admin_user, path):
    _dados()
    assert _client(app, admin_user).get(path).status_code == 403
    assert _client(app, owner_user).get(path).status_code == 200
    assert _client(app).get(path).status_code in (302, 401)


def test_promocao_informada_nao_altera_cargo_e_aparece_na_macro(app, owner_user):
    ana, _, _, _ = _dados()
    client = _client(app, owner_user)
    url = f'/rh/funcionarios/{ana.id}/carreira'
    antes = (ana.cargo_id, ana.salario_efetivo(), ana.lider_id)
    payload = {'data_efetiva': '2025-04-15', 'observacao': 'Registro do RH'}
    assert client.post(url, data=payload).status_code == 302
    client.post(url, data=payload)
    assert RhMovimentacao.query.count() == 1
    assert (ana.cargo_id, ana.salario_efetivo(), ana.lider_id) == antes
    html = client.get('/rh/equipe').get_data(as_text=True)
    assert '15/04/2025' in html and 'Atendente 1' in html
    assert 'Registro do RH' in client.get(url).get_data(as_text=True)


def test_promocao_post_restrito_e_data_invalida_nao_grava(app, owner_user, admin_user):
    ana, _, _, _ = _dados()
    url = f'/rh/funcionarios/{ana.id}/carreira'
    assert _client(app, admin_user).post(url, data={'data_efetiva':'2025-01-01'}).status_code == 403
    client = _client(app, owner_user)
    for data in ('errada', '2999-01-01', '2019-01-01'):
        assert client.post(url, data={'data_efetiva': data}).status_code == 302
    assert RhMovimentacao.query.count() == 0


def test_unificacao_explicita_mantem_salarios_e_reimport_nao_recria_duplicata(app, owner_user):
    from app.services import plano_carreira_import as carreira
    from tests.test_plano_carreira import _xlsx
    ana, _, _, _ = _dados()
    duplicate = Cargo(nome='Atendente 1', salario_base=2130.4)
    db.session.add(duplicate)
    db.session.commit()
    client = _client(app, owner_user)
    client.get('/rh/cargos/unificar-atendentes')
    assert duplicate.ativo
    response = client.post('/rh/cargos/unificar-atendentes')
    assert response.status_code == 302
    assert not duplicate.ativo and ana.salario_efetivo() == 2330.4
    carreira.aplicar(_xlsx(), 'teste.xlsx', owner_user.id)
    assert not db.session.get(Cargo, duplicate.id).ativo
    assert carreira.cargo_da_faixa('Atendimento', 1).id == ana.cargo_id


def test_edition_nao_relacionada_nao_cria_promocao_e_mudanca_guarda_autor(app, owner_user):
    ana, bia, _, _ = _dados()
    client = _client(app, owner_user)
    payload = {'nome': ana.nome, 'cpf': ana.cpf, 'cargo_id': str(ana.cargo_id),
               'ativo': '1', 'telefone': '11999999999'}
    client.post(f'/rh/funcionarios/{ana.id}/salvar', data=payload)
    assert RhMovimentacao.query.count() == 0
    client.post(f'/rh/funcionarios/{ana.id}/salvar', data=payload | {'cargo_id':str(bia.cargo_id)})
    registro = RhMovimentacao.query.one()
    assert registro.tipo == 'alteracao'
    assert registro.nivel_anterior == 1 and registro.nivel_novo == 2
    assert registro.data_efetiva is None and registro.registrado_por_id == owner_user.id


def test_empty_and_escape(app, owner_user):
    client = _client(app, owner_user)
    assert 'Nenhuma pessoa encontrada' in client.get('/rh/equipe').get_data(as_text=True)
    response = client.get('/rh/equipe?q=<script>alert(1)</script>')
    assert '<script>alert(1)</script>' not in response.get_data(as_text=True)


def test_novas_acoes_exigem_csrf(app, owner_user):
    app.config['WTF_CSRF_ENABLED'] = True
    ana, _, _, _ = _dados()
    duplicado = Cargo(nome='Atendente 1', salario_base=2130.4)
    db.session.add(duplicado)
    db.session.commit()
    client = _client(app, owner_user)
    resposta = client.post('/rh/cargos/unificar-atendentes')
    assert resposta.status_code == 302 and resposta.location == '/'
    resposta = client.post(f'/rh/funcionarios/{ana.id}/carreira', data={
        'data_efetiva': '2025-01-01'})
    assert resposta.status_code == 302 and resposta.location == '/'
    with client.session_transaction() as sessao:
        assert any('token-ausente' in texto for _, texto in sessao['_flashes'])
    assert duplicado.ativo
    assert RhMovimentacao.query.count() == 0


def test_unificacao_sem_cargos_nao_afirma_sucesso(app, owner_user):
    client = _client(app, owner_user)
    response = client.post('/rh/cargos/unificar-atendentes', follow_redirects=True)
    assert 'Não há cargos Atendente para unificar.' in response.get_data(as_text=True)


def test_card_cargos_abre_distribuicao(app, owner_user):
    client = _client(app, owner_user)
    response = client.get('/rh/equipe?cargos=1')
    assert 'id="rh-equipe-cargos" open' in response.get_data(as_text=True)
