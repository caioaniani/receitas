"""Cobrança de sobra POR ITEM no alerta das 20h (01/08/2026).

Caso real: "o pessoal não tem lançado sobra do croissant tradicional".
O alerta antigo cobrava só a LOJA ("lançou algo hoje?") — lançar a sobra de
UM item calava a cobrança de todos os outros. A conferência de 29-31/07
provou o custo (Pão Francês: 1.050 recebidos, 558 vendidos, ZERO sobra
lançada em 14 dias). Receita com `cobra_sobra_diaria` + saldo em EstoqueLoja
+ nenhum Desperdicio do item no dia = cobrança nominal na mensagem.
"""
from datetime import date

from app.extensions import db
from app.models import Desperdicio, EstoqueLoja, Loja
from app.services.desperdicio_alerta import (
    itens_sem_sobra,
    mensagem_pendentes,
)
from tests.conftest import _make_receita

_DIA = date(2026, 8, 1)          # sábado — toda loja sem config funciona


def _cenario(qtd=45, flag=True):
    """Loja + receita (flag opcional) + saldo em EstoqueLoja."""
    lj = Loja(nome='Loja Ribeiro do Vale', ativa=True)
    rec = _make_receita('Croissant Tradicional', categoria='Croissants')
    rec.cobra_sobra_diaria = flag
    db.session.add_all([lj, rec])
    db.session.flush()
    db.session.add(EstoqueLoja(loja_id=lj.id, receita_id=rec.id,
                               quantidade=qtd))
    db.session.commit()
    return lj, rec


# ── itens_sem_sobra ─────────────────────────────────────────────────

def test_item_flagged_com_saldo_e_cobrado(app):
    with app.app_context():
        lj, rec = _cenario()
        pend = itens_sem_sobra(_DIA)
        assert len(pend) == 1
        loja, itens = pend[0]
        assert loja.id == lj.id
        assert itens == [('Croissant Tradicional', 45)]


def test_lancou_o_item_hoje_some_da_cobranca(app):
    with app.app_context():
        lj, rec = _cenario()
        db.session.add(Desperdicio(loja_id=lj.id, receita_id=rec.id,
                                   quantidade=5, data=_DIA))
        db.session.commit()
        assert itens_sem_sobra(_DIA) == []


def test_lancar_OUTRO_item_nao_cala_a_cobranca(app):
    """O caso croissant, literal: a loja lançava a sobra do cookie e o
    croissant sumia do alerta por-loja. Por item, ele continua cobrado."""
    with app.app_context():
        lj, rec = _cenario()
        outro = _make_receita('Cookie Calebaut', categoria='Cookies')
        db.session.add(outro)
        db.session.flush()
        db.session.add(Desperdicio(loja_id=lj.id, receita_id=outro.id,
                                   quantidade=2, data=_DIA))
        db.session.commit()
        pend = itens_sem_sobra(_DIA)
        assert len(pend) == 1
        assert pend[0][1] == [('Croissant Tradicional', 45)]


def test_lancamento_de_ontem_nao_vale_pra_hoje(app):
    with app.app_context():
        lj, rec = _cenario()
        db.session.add(Desperdicio(loja_id=lj.id, receita_id=rec.id,
                                   quantidade=5, data=date(2026, 7, 31)))
        db.session.commit()
        assert len(itens_sem_sobra(_DIA)) == 1


def test_sem_flag_nao_cobra(app):
    with app.app_context():
        _cenario(flag=False)
        assert itens_sem_sobra(_DIA) == []


def test_saldo_zero_nao_cobra(app):
    """Vendeu tudo (saldo 0) = sem sobra a lançar, sem ruído."""
    with app.app_context():
        _cenario(qtd=0)
        assert itens_sem_sobra(_DIA) == []


def test_receita_arquivada_fica_fora(app):
    from app.utils import agora
    with app.app_context():
        lj, rec = _cenario()
        rec.arquivada_em = agora()
        db.session.commit()
        assert itens_sem_sobra(_DIA) == []


def test_loja_fechada_no_dia_fica_fora(app):
    """Mesma régua do alerta por loja (dias_funcionamento)."""
    segunda = date(2026, 7, 27)
    with app.app_context():
        lj, rec = _cenario()
        lj.dias_funcionamento = '56'      # só fim de semana
        db.session.commit()
        assert itens_sem_sobra(segunda) == []
        assert len(itens_sem_sobra(_DIA)) == 1   # sábado cobra


def test_desperdicio_de_produto_nao_conta_pra_receita(app):
    """Desperdicio sem receita_id (produto/MP) não pode calar a cobrança
    da receita — o set de lançados é por receita_id."""
    with app.app_context():
        lj, rec = _cenario()
        db.session.add(Desperdicio(loja_id=lj.id, receita_id=None,
                                   quantidade=1, data=_DIA))
        db.session.commit()
        assert len(itens_sem_sobra(_DIA)) == 1


# ── mensagem ────────────────────────────────────────────────────────

def test_mensagem_lista_item_com_saldo(app):
    with app.app_context():
        _cenario()
        texto = mensagem_pendentes([], itens_sem_sobra(_DIA))
        assert 'Loja Ribeiro do Vale' in texto
        assert 'Croissant Tradicional (45)' in texto
        assert 'confira o estoque' in texto


def test_mensagem_capa_em_8_itens_por_loja(app):
    with app.app_context():
        lj = Loja(nome='Loja Teste', ativa=True)
        db.session.add(lj)
        db.session.flush()
        for i in range(11):
            rec = _make_receita(f'Pao {i:02d}')
            rec.cobra_sobra_diaria = True
            db.session.add(rec)
            db.session.flush()
            db.session.add(EstoqueLoja(loja_id=lj.id, receita_id=rec.id,
                                       quantidade=10 + i))
        db.session.commit()
        texto = mensagem_pendentes([], itens_sem_sobra(_DIA))
        assert 'e mais 3' in texto


def test_mensagem_junta_loja_sem_nada_e_itens(app):
    with app.app_context():
        lj, rec = _cenario()
        outra = Loja(nome='Loja Nebraska', ativa=True)
        db.session.add(outra)
        db.session.commit()
        texto = mensagem_pendentes([outra], itens_sem_sobra(_DIA))
        assert 'Loja Nebraska' in texto           # não lançou nada
        assert 'Croissant Tradicional (45)' in texto


# ── senders disparam com pendência só de item ───────────────────────

def test_whatsapp_dispara_quando_so_ha_pendencia_de_item(app, monkeypatch):
    """A loja lançou ALGO (sai da lista por-loja) mas o croissant ficou —
    o alerta tem que sair mesmo assim."""
    from app.services import desperdicio_alerta as mod
    with app.app_context():
        lj, rec = _cenario()
        outro = _make_receita('Cookie Calebaut')
        db.session.add(outro)
        db.session.flush()
        db.session.add(Desperdicio(loja_id=lj.id, receita_id=outro.id,
                                   quantidade=1, data=_DIA))
        db.session.commit()

        app.config['ZAPI_NUMERO_DESTINO'] = '5511999999999'
        enviados = []
        monkeypatch.setattr(mod, 'lojas_sem_desperdicio', lambda dia=None: [])
        monkeypatch.setattr(mod, 'itens_sem_sobra',
                            lambda dia=None: itens_sem_sobra(_DIA))
        import app.services.zapi as zapi
        monkeypatch.setattr(zapi, 'disponivel', lambda: True)
        monkeypatch.setattr(zapi, 'enviar_texto',
                            lambda n, t: enviados.append((n, t)) or {'ok': True})
        mod.enviar_alerta_desperdicio()
        assert len(enviados) == 1
        assert 'Croissant Tradicional (45)' in enviados[0][1]


def test_whatsapp_nao_dispara_sem_nenhuma_pendencia(app, monkeypatch):
    from app.services import desperdicio_alerta as mod
    with app.app_context():
        app.config['ZAPI_NUMERO_DESTINO'] = '5511999999999'
        enviados = []
        monkeypatch.setattr(mod, 'lojas_sem_desperdicio', lambda dia=None: [])
        monkeypatch.setattr(mod, 'itens_sem_sobra', lambda dia=None: [])
        import app.services.zapi as zapi
        monkeypatch.setattr(zapi, 'disponivel', lambda: True)
        monkeypatch.setattr(zapi, 'enviar_texto',
                            lambda n, t: enviados.append((n, t)) or {'ok': True})
        mod.enviar_alerta_desperdicio()
        assert enviados == []


# ── ficha persiste a flag ───────────────────────────────────────────

def test_duplicar_copia_a_flag(app, admin_user):
    with app.app_context():
        rec = _make_receita('Croissant Tradicional')
        rec.cobra_sobra_diaria = True
        db.session.add(rec)
        db.session.commit()
        rid = rec.id
        c = app.test_client()
        with c.session_transaction() as s:
            s['_user_id'] = str(admin_user.id)
            s['_fresh'] = True
        r = c.post(f'/receitas/{rid}/duplicar', follow_redirects=True)
        assert r.status_code == 200
        from app.models import Receita
        copia = (Receita.query.filter(Receita.id != rid)
                 .filter(Receita.nome.contains('Croissant')).first())
        assert copia is not None and copia.cobra_sobra_diaria is True
