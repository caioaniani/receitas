"""Seed one-shot da curadoria do Dia dos Pais 2026 (07/08/2026, "Faz isso
pra mim"): zera no plano-do-dia de 09/08 todo item publicado FORA da
curadoria do dono (linhas com quantidade ficam) + preenche o bloqueio
'Mini Pães' na data especial. Guard: sem nenhuma linha curada, NÃO age
(zerar tudo apagaria as cestas junto). Marker: roda uma vez."""
from datetime import date

import pytest

from app.extensions import db
from app.migrations_legacy import _seed_curadoria_dia_pais

ALVO = date(2026, 8, 9)
HOJE_FAKE = date(2026, 8, 7)


@pytest.fixture
def catalogo(app):
    from app.models import LojaDataEspecial, Produto, Receita
    r1 = Receita(nome='Cesta Pai Herói', categoria='Cestas',
                 rendimento_qtd=1, rendimento_unidade='un', peso_base=1,
                 preco_site=300.0)
    r2 = Receita(nome='Sourdough Tradicional', categoria='Pães',
                 rendimento_qtd=1, rendimento_unidade='un', peso_base=1,
                 preco_site=40.0)
    p1 = Produto(nome='Caixa de Mini', categoria='Mini Pães',
                 preco_site=300.0, ativo=True)
    db.session.add_all([r1, r2, p1])
    db.session.add(LojaDataEspecial(data=ALVO, rotulo='Dia dos Pais',
                                    janelas='06:00–10:00',
                                    express_bloqueado=True))
    db.session.commit()
    return r1, r2, p1


def _linha(kind, item_id):
    from app.models import EstoqueSitePlano
    return EstoqueSitePlano.query.filter_by(
        kind=kind, item_id=item_id, data=ALVO).first()


def test_sem_curadoria_nao_zera_nada(app, catalogo):
    from app.models import AppConfig
    _seed_curadoria_dia_pais(app, _hoje=HOJE_FAKE)
    r1, r2, p1 = catalogo
    assert _linha('receita', r1.id) is None          # nada criado
    assert AppConfig.get('seed_curadoria_dia_pais_2026') == 'sem_curadoria'


def test_com_curadoria_zera_o_resto_e_preserva_as_cestas(app, catalogo):
    from app.models import EstoqueSitePlano, LojaDataEspecial
    r1, r2, p1 = catalogo
    # Curadoria do dono: a cesta com quantidade; e uma linha auto-criada
    # 99999 (rastro de venda, não é curadoria) com reserva.
    db.session.add(EstoqueSitePlano(kind='receita', item_id=r1.id,
                                    data=ALVO, qtd_planejada=8,
                                    qtd_reservada=2))
    db.session.add(EstoqueSitePlano(kind='receita', item_id=r2.id,
                                    data=ALVO, qtd_planejada=99999,
                                    qtd_reservada=3))
    db.session.commit()
    _seed_curadoria_dia_pais(app, _hoje=HOJE_FAKE)

    assert _linha('receita', r1.id).qtd_planejada == 8      # dono manda
    ln99 = _linha('receita', r2.id)
    assert ln99.qtd_planejada == 0                          # 99999 zerada
    assert ln99.qtd_reservada == 3                          # reserva fica
    assert _linha('produto', p1.id).qtd_planejada == 0      # criada zerada
    regra = LojaDataEspecial.query.filter_by(data=ALVO).first()
    assert regra.lista_bloqueios() == ['Mini Pães']


def test_marker_impede_segunda_execucao(app, catalogo):
    from app.models import EstoqueSitePlano, Produto
    r1, r2, p1 = catalogo
    db.session.add(EstoqueSitePlano(kind='receita', item_id=r1.id,
                                    data=ALVO, qtd_planejada=8))
    db.session.commit()
    _seed_curadoria_dia_pais(app, _hoje=HOJE_FAKE)
    novo = Produto(nome='Bolo Novo', categoria='Doces', preco_site=50.0,
                   ativo=True)
    db.session.add(novo)
    db.session.commit()
    _seed_curadoria_dia_pais(app, _hoje=HOJE_FAKE)
    assert _linha('produto', novo.id) is None      # 2ª execução = no-op


def test_nao_sobrescreve_bloqueio_do_dono(app, catalogo):
    from app.models import EstoqueSitePlano, LojaDataEspecial
    r1, _, _ = catalogo
    regra = LojaDataEspecial.query.filter_by(data=ALVO).first()
    regra.bloquear_itens = 'Croissants'
    db.session.add(EstoqueSitePlano(kind='receita', item_id=r1.id,
                                    data=ALVO, qtd_planejada=8))
    db.session.commit()
    _seed_curadoria_dia_pais(app, _hoje=HOJE_FAKE)
    db.session.refresh(regra)
    assert regra.lista_bloqueios() == ['Croissants']


def test_depois_da_data_so_marca(app, catalogo):
    from app.models import AppConfig, EstoqueSitePlano
    r1, _, p1 = catalogo
    db.session.add(EstoqueSitePlano(kind='receita', item_id=r1.id,
                                    data=ALVO, qtd_planejada=8))
    db.session.commit()
    _seed_curadoria_dia_pais(app, _hoje=date(2026, 8, 10))
    assert _linha('produto', p1.id) is None
    assert AppConfig.get('seed_curadoria_dia_pais_2026') == 'expirado'


def test_sonda_plano_dia(app, catalogo):
    from app.models import EstoqueSitePlano
    r1, r2, p1 = catalogo
    db.session.add(EstoqueSitePlano(kind='receita', item_id=r1.id,
                                    data=ALVO, qtd_planejada=8))
    db.session.commit()
    app.config['CLAUDE_API_TOKEN'] = 'tok'
    c = app.test_client()
    resp = c.get(f'/api/claude/plano-dia?data={ALVO.isoformat()}',
                 headers={'Authorization': 'Bearer tok'})
    assert resp.status_code == 200
    d = resp.get_json()
    assert d['ok'] is True
    assert any(ln['nome'] == 'Cesta Pai Herói' and ln['qtd_planejada'] == 8
               for ln in d['linhas'])
    nomes_livres = {x['nome'] for x in d['publicados_sem_linha_vendem_livre']}
    assert 'Caixa de Mini' in nomes_livres
    assert d['data_especial']['rotulo'] == 'Dia dos Pais'
