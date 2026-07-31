"""Dias de funcionamento da loja (27/07/2026, pedido do dono).

"Cantina nao precisa lancar sobras durante a semana pois so funciona de
sabado e domingo". Antes disso `lojas_sem_desperdicio` cobrava TODA loja
ativa em TODO dia — a Cantina levava 5 lembretes por dia util (Slack 20:10/
15/20/25 + WhatsApp do dono 20:30) por sobra que nao existia.

Regra: `Loja.dias_funcionamento` guarda os dias em que a loja ABRE, em
digitos do `date.weekday()` (0=segunda ... 6=domingo). VAZIO/NULL = abre
todo dia — fail-open DELIBERADO pra loja mal configurada continuar sendo
cobrada (sumir da cobranca em silencio seria o erro caro).
"""
from datetime import date

from app.extensions import db
from app.models import Desperdicio, Loja
from app.services.desperdicio_alerta import lojas_sem_desperdicio

# Datas fixas de referencia (nao dependem do "hoje" da suite):
_SEGUNDA = date(2026, 7, 27)
_SABADO = date(2026, 8, 1)
_DOMINGO = date(2026, 8, 2)


def test_datas_de_referencia_sao_o_que_o_teste_diz():
    """Trava o pressuposto: se as constantes mudarem, o resto mente."""
    assert _SEGUNDA.weekday() == 0
    assert _SABADO.weekday() == 5
    assert _DOMINGO.weekday() == 6


# ── modelo ──────────────────────────────────────────────────────────

def test_sem_configuracao_abre_todo_dia(app):
    """Default de TODAS as lojas existentes: nada muda pra elas."""
    with app.app_context():
        lj = Loja(nome='Loja Comum', ativa=True)
        assert lj.dias_funcionamento is None
        for d in (_SEGUNDA, _SABADO, _DOMINGO):
            assert lj.funciona_em(d)


def test_string_vazia_tambem_abre_todo_dia(app):
    """Fail-open: '' (form salvo sem marcar nada) nao pode fechar a loja."""
    with app.app_context():
        lj = Loja(nome='Loja Vazia', ativa=True, dias_funcionamento='')
        assert lj.funciona_em(_SEGUNDA)


def test_so_fim_de_semana(app):
    with app.app_context():
        lj = Loja(nome='Cantina', ativa=True, dias_funcionamento='56')
        assert not lj.funciona_em(_SEGUNDA)
        assert lj.funciona_em(_SABADO)
        assert lj.funciona_em(_DOMINGO)


# ── cobranca de sobras ──────────────────────────────────────────────

def _lojas():
    cantina = Loja(nome='Cantina', ativa=True, dias_funcionamento='56')
    normal = Loja(nome='Nebraska', ativa=True)
    db.session.add_all([cantina, normal])
    db.session.commit()
    return cantina, normal


def test_cantina_nao_e_cobrada_em_dia_de_semana(app, admin_user):
    """O pedido do dono, literal."""
    with app.app_context():
        _lojas()
        nomes = [lj.nome for lj in lojas_sem_desperdicio(_SEGUNDA)]
        assert 'Cantina' not in nomes
        assert 'Nebraska' in nomes       # loja normal segue cobrada


def test_cantina_E_cobrada_no_fim_de_semana(app, admin_user):
    """Nao pode virar isencao permanente: sabado ela abre e tem sobra."""
    with app.app_context():
        _lojas()
        assert 'Cantina' in [lj.nome for lj in lojas_sem_desperdicio(_SABADO)]
        assert 'Cantina' in [lj.nome for lj in lojas_sem_desperdicio(_DOMINGO)]


def test_cantina_que_lancou_no_sabado_sai_da_lista(app, admin_user, catalogo):
    """A regra antiga (ja lancou -> some) continua valendo junto da nova."""
    with app.app_context():
        cantina, _ = _lojas()
        db.session.add(Desperdicio(loja_id=cantina.id,
                                   receita_id=catalogo['receita'].id,
                                   quantidade=1, data=_SABADO))
        db.session.commit()
        assert 'Cantina' not in [lj.nome
                                 for lj in lojas_sem_desperdicio(_SABADO)]


def test_loja_sem_dias_configurados_e_cobrada_todo_dia(app, admin_user):
    """Regressao do fail-open: a feature nao pode calar a cobranca de quem
    nao configurou nada."""
    with app.app_context():
        _lojas()
        for d in (_SEGUNDA, _SABADO, _DOMINGO):
            assert 'Nebraska' in [lj.nome for lj in lojas_sem_desperdicio(d)]


# ── tela /rh/lojas ──────────────────────────────────────────────────

def _login(client, user):
    with client.session_transaction() as s:
        s['_user_id'] = str(user.id)
        s['_fresh'] = True


def test_form_salva_os_dias_marcados(app, owner_user):
    """RH e owner-only (before_request do blueprint)."""
    with app.app_context():
        lj = Loja(nome='Cantina', ativa=True)
        db.session.add(lj)
        db.session.commit()
        lid = lj.id
        c = app.test_client()
        _login(c, owner_user)
        r = c.post(f'/rh/lojas/{lid}/fiscal',
                   data={'dias_funcionamento': ['5', '6']},
                   follow_redirects=True)
        assert r.status_code == 200
        assert db.session.get(Loja, lid).dias_funcionamento == '56'


def test_form_sem_nenhum_dia_volta_pra_todo_dia(app, owner_user):
    with app.app_context():
        lj = Loja(nome='Cantina', ativa=True, dias_funcionamento='56')
        db.session.add(lj)
        db.session.commit()
        lid = lj.id
        c = app.test_client()
        _login(c, owner_user)
        c.post(f'/rh/lojas/{lid}/fiscal', data={}, follow_redirects=True)
        lj2 = db.session.get(Loja, lid)
        assert lj2.dias_funcionamento is None
        assert lj2.funciona_em(_SEGUNDA)


def test_valor_forjado_no_post_nao_entra_na_coluna(app, owner_user):
    """A coluna e VARCHAR(7): POST com lixo nao pode gravar nem estourar."""
    with app.app_context():
        lj = Loja(nome='Cantina', ativa=True)
        db.session.add(lj)
        db.session.commit()
        lid = lj.id
        c = app.test_client()
        _login(c, owner_user)
        c.post(f'/rh/lojas/{lid}/fiscal',
               data={'dias_funcionamento': ['9', 'xx', '5', '99999999']},
               follow_redirects=True)
        assert db.session.get(Loja, lid).dias_funcionamento == '5'
