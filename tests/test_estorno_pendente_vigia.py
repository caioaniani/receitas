"""Vigia de estorno que NUNCA vai disparar (26/07/2026).

Caso real: 4 cobranças canceladas entre 22 e 24/07 tinham baixado estoque
(7 itens) e nunca devolveram — o estorno é keyed em `canceledAt` e elas
foram canceladas só pelo `status`. Decisão do dono: **alertar**, sem mexer
no gatilho. Então o vigia NÃO pode tocar em estoque.
"""
from datetime import timedelta


def _reg(db, pid, *, baixados=2, estornado=False):
    from app.models import SeruPedidoProcessado
    from app.utils import agora
    r = SeruPedidoProcessado(seru_pedido_id=pid, n_itens_total=baixados,
                             n_itens_baixados=baixados,
                             estornado_em=agora() if estornado else None)
    db.session.add(r)
    db.session.commit()
    return r


def _pedido(pid, *, status='canceled', canceled_at=None, total=101.0):
    from app.utils import agora
    return {'id': pid, 'status': status, 'canceledAt': canceled_at,
            'total': total, 'createdAt': agora().isoformat() + 'Z',
            'company': {'name': 'Nebraska'}, 'items': []}


# ── A regra canônica ─────────────────────────────────────────────────────

def test_cancelado_so_por_status_e_pendente(app):
    """O caso dos 4: cancelado por status, SEM canceledAt, já baixou."""
    from app.extensions import db
    from app.services.estorno_pendente_vigia import e_estorno_pendente
    with app.app_context():
        reg = _reg(db, 'A1')
        assert e_estorno_pendente(_pedido('A1'), reg) is True


def test_com_canceledAt_NAO_e_pendente(app):
    """O gatilho normal do sync cobre — alertar seria ruído."""
    from app.extensions import db
    from app.services.estorno_pendente_vigia import e_estorno_pendente
    with app.app_context():
        reg = _reg(db, 'A2')
        p = _pedido('A2', canceled_at='2026-07-24T10:00:00Z')
        assert e_estorno_pendente(p, reg) is False


def test_ja_estornado_NAO_e_pendente(app):
    from app.extensions import db
    from app.services.estorno_pendente_vigia import e_estorno_pendente
    with app.app_context():
        reg = _reg(db, 'A3', estornado=True)
        assert e_estorno_pendente(_pedido('A3'), reg) is False


def test_sem_nada_baixado_NAO_e_pendente(app):
    """Não há o que devolver."""
    from app.extensions import db
    from app.services.estorno_pendente_vigia import e_estorno_pendente
    with app.app_context():
        reg = _reg(db, 'A4', baixados=0)
        assert e_estorno_pendente(_pedido('A4'), reg) is False


def test_pedido_NAO_cancelado_e_ignorado(app):
    from app.extensions import db
    from app.services.estorno_pendente_vigia import e_estorno_pendente
    with app.app_context():
        reg = _reg(db, 'A5')
        assert e_estorno_pendente(_pedido('A5', status='paid'), reg) is False


def test_pedido_nunca_processado_e_ignorado(app):
    """Sem registro não houve baixa por este caminho."""
    from app.services.estorno_pendente_vigia import e_estorno_pendente
    with app.app_context():
        assert e_estorno_pendente(_pedido('Z9'), None) is False


# ── Alerta: dedup, anti-flood e o contrato de não perder ─────────────────

def _pend(pid='A1'):
    from app.utils import hoje
    return [{'id': pid, 'data': hoje().isoformat(), 'loja': 'Nebraska',
             'total': 101.0, 'itens_baixados': 2}]


def test_alerta_uma_vez_por_pedido(app, monkeypatch):
    from app.services import estorno_pendente_vigia as v
    from app.services import zapi
    enviados = []
    monkeypatch.setattr(zapi, 'enviar_texto',
                        lambda n, m, **k: enviados.append(m) or {'ok': True})
    monkeypatch.setenv('ESTORNO_PENDENTE_COOLDOWN_MIN', '0')
    with app.app_context():
        app.config['ZAPI_BOT_DONO_NUMERO'] = '5511999999999'
        r1 = v.alertar(_pend())
        r2 = v.alertar(_pend())          # mesmo pedido, 2º ciclo
    assert r1['enviado'] is True and r1['novos'] == 1
    assert r2['novos'] == 0              # dedup
    assert len(enviados) == 1
    assert 'Nebraska' in enviados[0] and 'NÃO devolveram' in enviados[0]


def test_envio_falho_NAO_marca_retenta_depois(app, monkeypatch):
    """Perder alerta de estoque é pior que repetir."""
    from app.services import estorno_pendente_vigia as v
    from app.services import zapi
    monkeypatch.setenv('ESTORNO_PENDENTE_COOLDOWN_MIN', '0')
    with app.app_context():
        app.config['ZAPI_BOT_DONO_NUMERO'] = '5511999999999'
        monkeypatch.setattr(zapi, 'enviar_texto',
                            lambda n, m, **k: {'ok': False})
        r1 = v.alertar(_pend())
        assert r1['enviado'] is False
        vistos = []
        monkeypatch.setattr(
            zapi, 'enviar_texto',
            lambda n, m, **k: vistos.append(m) or {'ok': True})
        r2 = v.alertar(_pend())          # ainda é novo
    assert r2['enviado'] is True and len(vistos) == 1


def test_sem_numero_do_dono_nao_marca(app, monkeypatch):
    from app.services import estorno_pendente_vigia as v
    with app.app_context():
        app.config['ZAPI_BOT_DONO_NUMERO'] = ''
        app.config['CHATWOOT_VIGIA_INFRA_NUMERO'] = ''
        r = v.alertar(_pend())
    assert r['enviado'] is False and r['novos'] == 1


def test_kill_switch(app, monkeypatch):
    from app.services import estorno_pendente_vigia as v
    monkeypatch.setenv('ESTORNO_PENDENTE_VIGIA', '0')
    with app.app_context():
        assert v.alertar(_pend())['rodou'] is False


def test_cooldown_segura_mas_nao_perde(app, monkeypatch):
    from app.services import estorno_pendente_vigia as v
    from app.services import zapi
    enviados = []
    monkeypatch.setattr(zapi, 'enviar_texto',
                        lambda n, m, **k: enviados.append(m) or {'ok': True})
    monkeypatch.setenv('ESTORNO_PENDENTE_COOLDOWN_MIN', '60')
    with app.app_context():
        app.config['ZAPI_BOT_DONO_NUMERO'] = '5511999999999'
        v.alertar(_pend('B1'))
        r2 = v.alertar(_pend('B2'))      # outro pedido, dentro do cooldown
    assert len(enviados) == 1
    assert r2['enviado'] is False and r2['novos'] == 1   # acumulou


def test_mensagem_diz_o_que_saiu_do_estoque(app, monkeypatch):
    """O dono precisa saber O QUE devolver, não só que houve problema."""
    from app.extensions import db
    from app.models import EstoqueLoja, Loja, MovEstoqueLoja, Receita
    from app.services import estorno_pendente_vigia as v
    from app.services import zapi
    enviados = []
    monkeypatch.setattr(zapi, 'enviar_texto',
                        lambda n, m, **k: enviados.append(m) or {'ok': True})
    monkeypatch.setenv('ESTORNO_PENDENTE_COOLDOWN_MIN', '0')
    with app.app_context():
        loja = Loja(nome='Nebraska', ativa=True)
        r = Receita(nome='Pão Francês', categoria='Paes', rendimento_qtd=1,
                    rendimento_unidade='un', peso_base=50)
        db.session.add_all([loja, r])
        db.session.commit()
        el = EstoqueLoja(loja_id=loja.id, receita_id=r.id, quantidade=10)
        db.session.add(el)
        db.session.commit()
        db.session.add(MovEstoqueLoja(estoque_loja_id=el.id,
                                      tipo='venda_seru', quantidade=3,
                                      referencia='Seru #C7'))
        db.session.commit()
        app.config['ZAPI_BOT_DONO_NUMERO'] = '5511999999999'
        v.alertar(_pend('C7'))
    assert enviados and '3x Pão Francês' in enviados[0]


def test_vigia_NAO_mexe_em_estoque(app, monkeypatch):
    """Decisão do dono: alertar, não corrigir. O saldo tem que ficar igual."""
    from app.extensions import db
    from app.models import EstoqueLoja, Loja, Receita
    from app.services import estorno_pendente_vigia as v
    from app.services import zapi
    monkeypatch.setattr(zapi, 'enviar_texto', lambda n, m, **k: {'ok': True})
    monkeypatch.setenv('ESTORNO_PENDENTE_COOLDOWN_MIN', '0')
    with app.app_context():
        loja = Loja(nome='Nebraska', ativa=True)
        r = Receita(nome='Pão', categoria='Paes', rendimento_qtd=1,
                    rendimento_unidade='un', peso_base=50)
        db.session.add_all([loja, r])
        db.session.commit()
        el = EstoqueLoja(loja_id=loja.id, receita_id=r.id, quantidade=7)
        db.session.add(el)
        db.session.commit()
        eid = el.id
        app.config['ZAPI_BOT_DONO_NUMERO'] = '5511999999999'
        v.alertar(_pend('D1'))
        db.session.expire_all()
        assert EstoqueLoja.query.get(eid).quantidade == 7


def test_estado_podado_nao_cresce_pra_sempre(app, monkeypatch):
    import json

    from app.extensions import db
    from app.models import AppConfig
    from app.services import estorno_pendente_vigia as v
    from app.utils import hoje
    with app.app_context():
        velho = (hoje() - timedelta(days=30)).isoformat()
        AppConfig.set('estorno_pendente_alertados',
                      json.dumps({'ids': {velho: ['X1']}, 'envios': {}}))
        db.session.commit()
        est = v._carregar_estado()
    assert velho not in est['ids']
