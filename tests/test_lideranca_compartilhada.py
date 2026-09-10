"""Liderança compartilhada acompanha a equipe sem trocar vínculos diretos."""
import pytest

from app.extensions import db
from app.models import EquipeLiderCompartilhado, Loja
from app.services import treino_ledger, treino_lideranca
from tests.test_treino_lideranca import _login, _pessoa


@pytest.fixture
def equipe(app):
    loja = Loja(nome='Ribeiro', ativa=True)
    db.session.add(loja)
    sabrina_u, sabrina = _pessoa('Sabrina', '71001')
    kelvin_u, kelvin = _pessoa('Kelvin', '71002')
    _, pessoa = _pessoa('Equipe Sabrina', '71003')
    _, outra = _pessoa('Outra equipe', '71004')
    for f in (sabrina, kelvin, pessoa, outra):
        f.lojas = [loja]
        f.periodo = 'Manhã'
    pessoa.lider_id = sabrina.id
    db.session.commit()
    return sabrina_u, sabrina, kelvin_u, kelvin, pessoa, outra, loja


def compartilhar(equipe, owner):
    _, sabrina, _, kelvin, *_ = equipe
    return treino_lideranca.compartilhar_equipe(sabrina.id, kelvin.id, owner.id)


def test_kelvin_compartilha_acompanhamento_sem_substituir_sabrina(app, equipe, owner_user):
    _, sabrina, kelvin_u, kelvin, pessoa, outra, _ = equipe
    compartilhar(equipe, owner_user)
    assert pessoa.lider_id == sabrina.id
    assert treino_lideranca.liderados_do(sabrina) == [pessoa]
    assert treino_lideranca.liderados_do(kelvin) == [pessoa]
    assert treino_lideranca.pode_observar(kelvin, pessoa)
    assert not treino_lideranca.pode_observar(kelvin, outra)
    assert not treino_lideranca.pode_observar(kelvin, sabrina)
    assert treino_ledger.papel_treino(kelvin_u) == 'GESTOR'
    client = _login(app, kelvin_u.id)
    page = client.get('/treino/gestor/')
    assert page.status_code == 200
    assert b'Equipe Sabrina' in page.data
    assert b'Outra equipe' not in page.data
    assert client.get(f'/treino/gestor/observar/{pessoa.id}').status_code == 200
    assert client.get(f'/treino/gestor/observar/{outra.id}').status_code == 403


@pytest.mark.parametrize('mudanca', ['lider_inativo', 'parceiro_inativo', 'loja_inativa',
                                     'turno_parceiro', 'turno_pessoa', 'loja_pessoa', 'sem_conta'])
def test_acesso_compartilhado_respeita_escopo_vivo(equipe, owner_user, mudanca):
    _, sabrina, _, kelvin, pessoa, _, loja = equipe
    compartilhar(equipe, owner_user)
    if mudanca == 'lider_inativo':
        sabrina.ativo = False
    elif mudanca == 'parceiro_inativo':
        kelvin.ativo = False
    elif mudanca == 'loja_inativa':
        loja.ativa = False
    elif mudanca == 'turno_parceiro':
        kelvin.periodo = 'Tarde'
    elif mudanca == 'turno_pessoa':
        pessoa.periodo = 'Tarde'
    elif mudanca == 'loja_pessoa':
        pessoa.lojas = []
    else:
        kelvin.usuario_id = None
    db.session.commit()
    assert treino_lideranca.liderados_do(kelvin) == []
    assert not treino_lideranca.pode_observar(kelvin, pessoa)


def test_nao_propagacao_para_outras_equipes_e_idempotencia(equipe, owner_user):
    _, sabrina, _, kelvin, pessoa, outra, _ = equipe
    a = compartilhar(equipe, owner_user)
    b = compartilhar(equipe, owner_user)
    assert a.id == b.id
    assert EquipeLiderCompartilhado.query.count() == 1
    outra.lider_id = kelvin.id
    db.session.commit()
    assert treino_lideranca.liderados_do(sabrina) == [pessoa]
    assert set(f.id for f in treino_lideranca.liderados_do(kelvin)) == {pessoa.id, outra.id}


def test_concessao_exige_organizacao_e_revogacao_preserva_historico(app, equipe, owner_user):
    _, sabrina, kelvin_u, kelvin, _, _, _ = equipe
    client = _login(app, kelvin_u.id)
    url = '/rh/lideranca/compartilhar'
    data = {'lider_id': sabrina.id, 'parceiro_id': kelvin.id}
    response = client.post(url, data=data)
    assert response.status_code in (302, 403)
    assert EquipeLiderCompartilhado.query.count() == 0
    client.get('/auth/logout')
    client.post('/auth/login', data={'login': owner_user.login, 'senha': '123'})
    assert client.post(url, data=data).status_code == 302
    v = EquipeLiderCompartilhado.query.one()
    assert v.criado_por_id == owner_user.id
    assert client.post(f'/rh/lideranca/compartilhada/{v.id}/remover').status_code == 302
    assert not v.ativo
    assert v.removido_por_id == owner_user.id
    assert treino_lideranca.liderados_do(kelvin) == []


def test_recusa_mesma_pessoa_e_outro_turno(equipe, owner_user):
    _, sabrina, _, kelvin, *_ = equipe
    with pytest.raises(treino_lideranca.LiderancaError):
        treino_lideranca.compartilhar_equipe(sabrina.id, sabrina.id, owner_user.id)
    kelvin.periodo = 'Tarde'
    db.session.commit()
    with pytest.raises(treino_lideranca.LiderancaError):
        compartilhar(equipe, owner_user)
    assert EquipeLiderCompartilhado.query.count() == 0


def test_lider_com_acesso_restrito_pode_editar_checklist_liberado(app, equipe, owner_user):
    from app.models import ChecklistResponsavel
    _, _, kelvin_u, kelvin, _, _, loja = equipe
    kelvin.funcao = 'ATENDENTE CHEFE'
    db.session.add(ChecklistResponsavel(funcionario_id=kelvin.id, loja_id=loja.id,
        periodo='Manhã', criado_por_id=owner_user.id, ativo=True))
    db.session.commit()
    client = _login(app, kelvin_u.id)
    resposta = client.post('/checklist/itens', data={
        'loja': loja.id, 'tipo': 'abertura', 'acao': 'novo', 'texto': 'Conferir café'})
    assert resposta.status_code == 200
    assert resposta.is_json
