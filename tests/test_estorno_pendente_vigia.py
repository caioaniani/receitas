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
    # createdAt em UTC DE VERDADE, meio-dia BRT (15:00Z): `agora()` é BRT e
    # colar 'Z' nele mentia o fuso — entre 00:00 e 03:00 BRT a conversão
    # UTC→BRT do sync jogava o pedido pro dia ANTERIOR, fora da janela
    # [hoje, hoje], e 4 testes ficavam vermelhos SÓ de madrugada (com
    # Wait-for-CI, deploy travado nessa faixa). Meio-dia nunca cruza a data.
    from datetime import datetime, time

    from app.utils import hoje
    meio_dia_utc = datetime.combine(hoje(), time(15, 0))
    return {'id': pid, 'status': status, 'canceledAt': canceled_at,
            'total': total, 'createdAt': meio_dia_utc.isoformat() + 'Z',
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


def test_teto_diario_segura_mas_acumula(app, monkeypatch):
    """Teto atingido não descarta: os ids não são marcados e voltam."""
    from app.services import estorno_pendente_vigia as v
    from app.services import zapi
    enviados = []
    monkeypatch.setattr(zapi, 'enviar_texto',
                        lambda n, m, **k: enviados.append(m) or {'ok': True})
    monkeypatch.setenv('ESTORNO_PENDENTE_COOLDOWN_MIN', '0')
    monkeypatch.setenv('ESTORNO_PENDENTE_MAX_MSGS_DIA', '1')
    with app.app_context():
        app.config['ZAPI_BOT_DONO_NUMERO'] = '5511999999999'
        v.alertar(_pend('T1'))
        r2 = v.alertar(_pend('T2'))
    assert len(enviados) == 1
    assert r2['enviado'] is False and r2['novos'] == 1


def test_env_negativa_nao_cala_o_vigia(app, monkeypatch):
    """`0 >= -1` deixaria o teto sempre atingido — silêncio permanente
    escondendo estoque baixado indevidamente."""
    from app.services import estorno_pendente_vigia as v
    from app.services import zapi
    enviados = []
    monkeypatch.setattr(zapi, 'enviar_texto',
                        lambda n, m, **k: enviados.append(m) or {'ok': True})
    monkeypatch.setenv('ESTORNO_PENDENTE_COOLDOWN_MIN', '0')
    monkeypatch.setenv('ESTORNO_PENDENTE_MAX_MSGS_DIA', '-1')
    with app.app_context():
        app.config['ZAPI_BOT_DONO_NUMERO'] = '5511999999999'
        r = v.alertar(_pend('N1'))
    assert r['enviado'] is True and len(enviados) == 1


def test_estado_corrompido_nao_cega_o_vigia(app, monkeypatch):
    """Estado torto não se autocorrige — sem isinstance, o vigia ficaria
    mudo até alguém apagar a chave na mão."""
    from app.extensions import db
    from app.models import AppConfig
    from app.services import estorno_pendente_vigia as v
    from app.services import zapi
    enviados = []
    monkeypatch.setattr(zapi, 'enviar_texto',
                        lambda n, m, **k: enviados.append(m) or {'ok': True})
    monkeypatch.setenv('ESTORNO_PENDENTE_COOLDOWN_MIN', '0')
    with app.app_context():
        AppConfig.set('estorno_pendente_alertados',
                      '{"ids": ["lista, nao dict"], "envios": {"x": "abc"}}')
        db.session.commit()
        app.config['ZAPI_BOT_DONO_NUMERO'] = '5511999999999'
        r = v.alertar(_pend('K1'))
    assert r['enviado'] is True and len(enviados) == 1


# ── Detalhe do que saiu do estoque ───────────────────────────────────────

def _mov(db, ref, *, tipo='venda_seru', qtd=3, nome='Cookie'):
    from app.models import EstoqueLoja, Loja, MovEstoqueLoja, Receita
    loja = Loja.query.filter_by(nome='Nebraska').first()
    if loja is None:
        loja = Loja(nome='Nebraska', ativa=True)
        db.session.add(loja)
        db.session.commit()
    r = Receita(nome=nome, categoria='Paes', rendimento_qtd=1,
                rendimento_unidade='un', peso_base=50)
    db.session.add(r)
    db.session.commit()
    el = EstoqueLoja(loja_id=loja.id, receita_id=r.id, quantidade=10)
    db.session.add(el)
    db.session.commit()
    db.session.add(MovEstoqueLoja(estoque_loja_id=el.id, tipo=tipo,
                                  quantidade=qtd, referencia=ref))
    db.session.commit()


def test_detalhe_ignora_baixa_que_nunca_saiu(app):
    """`venda_seru_sem_estoque` não tirou nada — mandar devolver criaria
    estoque fantasma."""
    from app.extensions import db
    from app.services.estorno_pendente_vigia import itens_baixados
    with app.app_context():
        _mov(db, 'Seru #E1', tipo='venda_seru_sem_estoque', nome='Baguete')
        itens, fracs = itens_baixados('E1')
    assert itens == [] and fracs == 0


def test_detalhe_separa_fracao_e_nao_manda_devolver(app):
    """A unidade inteira que fechou no acumulador pode ser de VÁRIAS vendas
    — o próprio estorno a pula. Só conta, não lista."""
    from app.extensions import db
    from app.services.estorno_pendente_vigia import itens_baixados
    with app.app_context():
        _mov(db, 'Seru #E2', nome='Pão Inteiro')
        _mov(db, 'Seru #E2 (fracao)', nome='Cookie Calebaut')
        itens, fracs = itens_baixados('E2')
    assert [n for n, _ in itens] == ['Pão Inteiro']
    assert fracs == 1


def test_mensagem_avisa_para_nao_devolver_fracao(app, monkeypatch):
    from app.extensions import db
    from app.services import estorno_pendente_vigia as v
    from app.services import zapi
    enviados = []
    monkeypatch.setattr(zapi, 'enviar_texto',
                        lambda n, m, **k: enviados.append(m) or {'ok': True})
    monkeypatch.setenv('ESTORNO_PENDENTE_COOLDOWN_MIN', '0')
    with app.app_context():
        _mov(db, 'Seru #E3 (fracao)', nome='Cookie Calebaut')
        app.config['ZAPI_BOT_DONO_NUMERO'] = '5511999999999'
        v.alertar(_pend('E3'))
    assert enviados and 'NAO devolver na mao' in enviados[0]


# ── Integração com o sync: detecta SEM tocar em estoque ──────────────────

def _seru_fake(monkeypatch, pedidos):
    from app.services import seru
    monkeypatch.setattr(seru, 'listar_pedidos_completo',
                        lambda di, df, **k: pedidos)


def test_sync_anota_pendente_e_nao_estorna(app, monkeypatch):
    from app.extensions import db
    from app.services import seru_sync
    with app.app_context():
        _reg(db, 'S1')
        _seru_fake(monkeypatch, [_pedido('S1')])
        from app.utils import hoje
        stats = seru_sync.processar_pedidos(hoje(), hoje())
    assert [c['id'] for c in stats['estornos_pendentes']] == ['S1']
    assert stats['pedidos_cancelados_estornados'] == 0


def test_sync_company_STRING_nao_derruba_o_ciclo(app, monkeypatch):
    """A API manda `company` ora dict ora string (o próprio seru_sync já
    tratava os dois na resolução de loja). Um alerta best-effort que
    estourasse aqui abortaria `processar_pedidos` ANTES do commit — as
    baixas de estoque do ciclo inteiro seriam descartadas."""
    from app.extensions import db
    from app.services import seru_sync
    from app.utils import hoje
    with app.app_context():
        _reg(db, 'S2')
        p = _pedido('S2')
        p['company'] = 'Nebraska'          # string crua
        _seru_fake(monkeypatch, [p])
        stats = seru_sync.processar_pedidos(hoje(), hoje())
    assert [c['loja'] for c in stats['estornos_pendentes']] == ['Nebraska']


def test_sync_deteccao_quebrada_nao_mata_o_loop(app, monkeypatch):
    """Blindagem: alerta é best-effort, estoque não paga a conta dele."""
    from app.extensions import db
    from app.services import estorno_pendente_vigia as v
    from app.services import seru_sync
    from app.utils import hoje
    with app.app_context():
        _reg(db, 'S3')
        _seru_fake(monkeypatch, [_pedido('S3')])
        monkeypatch.setattr(v, 'e_estorno_pendente',
                            lambda p, r: (_ for _ in ()).throw(RuntimeError()))
        stats = seru_sync.processar_pedidos(hoje(), hoje())
    assert stats['estornos_pendentes'] == []
    assert stats['pedidos_ja_processados'] == 1


def test_detectar_respeita_a_janela_por_createdAt(app, monkeypatch):
    from datetime import timedelta as _td

    from app.extensions import db
    from app.services import estorno_pendente_vigia as v
    from app.utils import agora, hoje
    with app.app_context():
        _reg(db, 'J1')
        p = _pedido('J1')
        p['createdAt'] = (agora() - _td(days=20)).isoformat() + 'Z'
        _seru_fake(monkeypatch, [p])
        assert v.detectar(hoje() - _td(days=2), hoje()) == []


def test_detectar_tolera_lixo_da_api(app, monkeypatch):
    from app.extensions import db
    from app.services import estorno_pendente_vigia as v
    from app.utils import hoje
    with app.app_context():
        _reg(db, 'J2')
        bom = _pedido('J2', total='nao-e-numero')
        _seru_fake(monkeypatch, ['string solta', None, {'sem': 'id'}, bom])
        out = v.detectar(hoje(), hoje())
    assert [c['id'] for c in out] == ['J2'] and out[0]['total'] == 0.0


# ── Rota sob demanda ─────────────────────────────────────────────────────

def _logado(app, user):
    c = app.test_client()
    with c.session_transaction() as s:
        s['_user_id'] = str(user.id)
        s['_fresh'] = True
    return c


def test_rota_exige_owner(app, admin_user):
    """Admin comum não vê — é diagnóstico de estoque do dono."""
    assert _logado(app, admin_user).get(
        '/admin/vigia-estorno-pendente').status_code == 403


def test_rota_dry_run_nao_envia_nem_marca(app, owner_user, monkeypatch):
    from app.extensions import db
    from app.services import zapi
    enviados = []
    monkeypatch.setattr(zapi, 'enviar_texto',
                        lambda n, m, **k: enviados.append(m) or {'ok': True})
    c = _logado(app, owner_user)
    with app.app_context():
        _reg(db, 'R1')
        _seru_fake(monkeypatch, [_pedido('R1')])
        r = c.get('/admin/vigia-estorno-pendente')
    assert r.status_code == 200
    j = r.get_json()
    assert j['dry_run'] is True and [x['id'] for x in j['pendentes']] == ['R1']
    assert enviados == []


def test_claim_e_gravado_ANTES_do_envio(app, monkeypatch):
    """Claim-first (19/08/2026, mesma classe do venda_sem_item_vigia): os
    ids têm que estar no banco quando o WhatsApp sai — kill de deploy entre
    o envio e o commit não pode duplicar o alerta no container novo."""
    import json as _json

    from app.models import AppConfig
    from app.services import estorno_pendente_vigia as v
    from app.services import zapi
    monkeypatch.setenv('ESTORNO_PENDENTE_COOLDOWN_MIN', '0')
    visto = {}

    def _envia(n, m, **k):
        est = _json.loads(AppConfig.get(v._KEY_ESTADO) or '{}')
        ids = {i for lst in (est.get('ids') or {}).values() for i in lst}
        visto['marcado_no_envio'] = 'CF1' in ids
        return {'ok': True}

    monkeypatch.setattr(zapi, 'enviar_texto', _envia)
    with app.app_context():
        app.config['ZAPI_BOT_DONO_NUMERO'] = '5511999999999'
        r = v.alertar(_pend('CF1'))
    assert r['enviado'] is True
    assert visto['marcado_no_envio'] is True
