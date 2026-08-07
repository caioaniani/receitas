"""Seed one-shot dos motoristas do Dia dos Pais (07/08/2026, print do dono
no WhatsApp: "cadastrar esses motoristas"). Regras: nunca duplicar pessoa
(match por nome sem acento/caixa OU por telefone canonico), nunca
sobrescrever dado do dono (telefone so preenche se vazio), marker impede
segunda execucao. + sonda read-only /api/claude/drivers (sem token/pin)."""
import pytest

from app.extensions import db
from app.migrations_legacy import _SEED_DRIVERS_2026_08, _seed_drivers_entrega
from app.models import AppConfig, Driver

MARKER = 'seed_drivers_entrega_2026_08'


def test_cria_os_11_com_token_e_telefone(app):
    _seed_drivers_entrega(app)
    drivers = Driver.query.all()
    assert len(drivers) == len(_SEED_DRIVERS_2026_08) == 11
    por_nome = {d.nome: d for d in drivers}
    assert por_nome['Hélio'].telefone == '5511983811876'
    assert por_nome['Andreia'].telefone == '5511998909264'
    for d in drivers:
        assert d.ativo is True
        assert d.token and len(d.token) >= 16
        assert d.pin is None            # PIN e gesto do dono na tela
    assert AppConfig.get(MARKER) == 'criados=11'


def test_marker_impede_segunda_execucao(app):
    _seed_drivers_entrega(app)
    Driver.query.filter_by(nome='Sibele').delete()
    db.session.commit()
    _seed_drivers_entrega(app)          # marker gravado -> nao ressuscita
    assert Driver.query.filter_by(nome='Sibele').count() == 0
    assert Driver.query.count() == 10


def test_nome_existente_sem_acento_nao_duplica_e_ganha_telefone(app):
    # Prod pode ja ter 'Marcia' digitada sem acento e sem telefone.
    db.session.add(Driver(nome='marcia', ativo=True, token='tok-marcia-1234'))
    db.session.commit()
    _seed_drivers_entrega(app)
    marcias = Driver.query.filter(Driver.nome.ilike('m%rcia')).all()
    assert len(marcias) == 1            # nao criou a 'Márcia' da lista
    assert marcias[0].nome == 'marcia'  # nome do dono intocado
    assert marcias[0].telefone == '5511998137354'
    assert Driver.query.count() == 11


def test_existente_com_telefone_fica_intocado(app):
    d = Driver(nome='Rodrigo', telefone='11 91111-2222', ativo=False,
               token='tok-rodrigo-123')
    db.session.add(d)
    db.session.commit()
    _seed_drivers_entrega(app)
    db.session.refresh(d)
    assert d.telefone == '11 91111-2222'    # divergente NAO sobrescreve
    assert d.ativo is False                 # ativo tambem e do dono
    assert Driver.query.count() == 11


def test_match_por_telefone_com_nome_diferente_nao_duplica(app):
    # Mesma pessoa cadastrada com apelido e telefone sem o 55.
    db.session.add(Driver(nome='Lú', telefone='(11) 91187-4548',
                          ativo=True, token='tok-lu-99999999'))
    db.session.commit()
    _seed_drivers_entrega(app)
    assert Driver.query.filter_by(nome='Luís').count() == 0
    assert Driver.query.count() == 11


@pytest.fixture
def _token_sonda(app):
    app.config['CLAUDE_API_TOKEN'] = 'tok-teste'
    return {'Authorization': 'Bearer tok-teste'}


def test_sonda_drivers_lista_sem_token_nem_pin(client, app, _token_sonda):
    db.session.add(Driver(nome='Zeca', telefone='5511900001111', ativo=True,
                          token='segredo-do-driver', pin='1234'))
    db.session.add(Driver(nome='Fora', ativo=False, token='tok-fora-1234'))
    db.session.commit()

    r = client.get('/api/claude/drivers', headers=_token_sonda)
    assert r.status_code == 200
    data = r.get_json()
    assert data['total'] == 1                       # so ativos por default
    d = data['drivers'][0]
    assert d['nome'] == 'Zeca'
    assert d['tem_token'] is True and d['tem_pin'] is True
    assert 'token' not in d and 'pin' not in d      # nunca vaza o segredo
    assert 'segredo-do-driver' not in r.get_data(as_text=True)

    r2 = client.get('/api/claude/drivers?todos=1', headers=_token_sonda)
    assert r2.get_json()['total'] == 2


def test_sonda_drivers_exige_token(client, app, _token_sonda):
    assert client.get('/api/claude/drivers').status_code == 401
