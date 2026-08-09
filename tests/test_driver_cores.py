"""Cores ÚNICAS por motorista (dono 09/08/2026, manhã do Dia dos Pais:
"Preciso que as cores nao sejam repetidas"). Paleta no cadastro novo
(`criar_driver` sem cor pega uma livre) + seed one-shot acertando os
existentes (NULL/repetida ganha cor livre; primeiro dono da cor mantém)."""
from app.blueprints.entregas.routes import PALETA_DRIVERS, cor_driver_livre
from app.extensions import db
from app.migrations_legacy import _seed_cores_drivers
from app.models import AppConfig, Driver


def _login_admin(client, user):
    with client.session_transaction() as sess:
        sess['_user_id'] = str(user.id)
        sess['_fresh'] = True


def test_paleta_sem_duplicatas_e_fallback_infinito():
    assert len({c.lower() for c in PALETA_DRIVERS}) == len(PALETA_DRIVERS)
    usadas = {c.lower() for c in PALETA_DRIVERS}
    extras = set()
    for _ in range(25):                      # paleta esgotada: gera na hora
        nova = cor_driver_livre(usadas | extras)
        assert nova.lower() not in usadas | extras
        extras.add(nova.lower())


def test_seed_da_cor_unica_pra_todos(app):
    db.session.add_all([
        Driver(nome='Sem Cor 1', ativo=True, token='t-c1'),
        Driver(nome='Sem Cor 2', ativo=True, token='t-c2'),
        Driver(nome='Vermelho A', cor='#e6194b', ativo=True, token='t-c3'),
        Driver(nome='Vermelho B', cor='#E6194B', ativo=True, token='t-c4'),
        Driver(nome='Roxo', cor='#911eb4', ativo=False, token='t-c5'),
    ])
    db.session.commit()
    _seed_cores_drivers(app)
    drivers = Driver.query.all()
    cores = [(d.cor or '').lower() for d in drivers]
    assert all(cores)                               # ninguém sem cor
    assert len(set(cores)) == len(cores)            # nenhuma repetida
    # Primeiro dono da cor mantém; o duplicado é que troca
    assert Driver.query.filter_by(nome='Vermelho A').first().cor == '#e6194b'
    assert Driver.query.filter_by(nome='Vermelho B').first().cor.lower() != '#e6194b'
    assert AppConfig.get('seed_cores_drivers_2026_08').startswith('trocados=')


def test_seed_roda_uma_vez(app):
    db.session.add(Driver(nome='Um', ativo=True, token='t-u1'))
    db.session.commit()
    _seed_cores_drivers(app)
    d2 = Driver(nome='Dois', ativo=True, token='t-u2')
    db.session.add(d2)
    db.session.commit()
    _seed_cores_drivers(app)                        # marker: não re-roda
    db.session.refresh(d2)
    assert d2.cor is None


def test_criar_driver_ganha_cor_livre(app, admin_user):
    db.session.add(Driver(nome='Ocupa1', cor=PALETA_DRIVERS[0],
                          ativo=True, token='t-o1'))
    db.session.commit()
    client = app.test_client()
    _login_admin(client, admin_user)
    r = client.post('/entregas/api/drivers', json={'nome': 'Novato'})
    assert r.status_code == 200 and r.get_json()['ok'] is True
    novo = Driver.query.filter_by(nome='Novato').first()
    assert novo.cor == PALETA_DRIVERS[1]            # pulou a ocupada
    # Cor explícita do usuário continua valendo
    r2 = client.post('/entregas/api/drivers',
                     json={'nome': 'Escolhida', 'cor': '#123456'})
    assert r2.status_code == 200
    assert Driver.query.filter_by(nome='Escolhida').first().cor == '#123456'
