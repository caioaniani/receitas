"""Vigia de venda SEM itens no PDV (18/07/2026, caso Nebraska).

Alerta imediato (ciclo do sync, 15min) de cobrança "só valor" — sem
produto, tipicamente sem NF. Dedup por pedido em AppConfig; envio falho
NÃO marca (retenta). API Seru e Z-API SEMPRE mockadas.
"""
import json
from datetime import timedelta
from unittest.mock import patch

from app.extensions import db
from app.models import AppConfig
from app.services import venda_sem_item_vigia as vigia
from app.utils import agora, hoje


def _pedido(pid, total, itens=0, cancelado=False, company='NEBRASKA',
            caixa='678071', nf=False, criado=None):
    dt = criado or agora().replace(hour=15, minute=0, second=0)
    return {
        'id': pid, 'code': f'c-{pid}', 'total': total,
        'canceledAt': '2026-01-01T00:00:00Z' if cancelado else None,
        'createdAt': (dt + timedelta(hours=3)).strftime(
            '%Y-%m-%dT%H:%M:%SZ'),                    # UTC = BRT+3
        'company': {'name': company},
        'cashier': {'code': caixa},
        'taxInvoice': {'status': 'ok', 'number': '1'} if nf else None,
        'items': [{'name': 'Pao', 'quantity': 1, 'total': total,
                   'canceledAt': None}] if itens else [],
    }


def _extrair_itens_fake(p):
    return [{'nome': i.get('name'), 'qtd': i.get('quantity'),
             'total': i.get('total'), 'sku': None,
             'cancelado': bool(i.get('canceledAt'))}
            for i in (p.get('items') or [])]


_SEM_DETALHE = object()   # sentinela: força detalhes_pedido a devolver None


def _detalhe_fake(pedidos, detalhes):
    """detalhes_pedido(id): por padrão o detalhe == o pedido da lista (a
    Seru concorda nos dois). `detalhes` sobrescreve por id — pra simular o
    LAG (lista sem itens/NF, detalhe com itens/NFC-e) ou a falha do detalhe
    (`_SEM_DETALHE` -> None, o vigia adia)."""
    por_id = {p.get('id'): p for p in pedidos}
    if detalhes:
        por_id.update(detalhes)

    def _f(pid):
        v = por_id.get(pid)
        return None if v is _SEM_DETALHE else v
    return _f


def _rodar(pedidos, zapi_ok=True, dono='5511999999999', detalhes=None):
    from flask import current_app
    current_app.config['ZAPI_BOT_DONO_NUMERO'] = dono
    current_app.config['CHATWOOT_VIGIA_INFRA_NUMERO'] = ''
    with patch('app.services.seru.listar_pedidos_completo',
               return_value=pedidos), \
         patch('app.services.seru.extrair_itens',
               side_effect=_extrair_itens_fake), \
         patch('app.services.seru.detalhes_pedido',
               side_effect=_detalhe_fake(pedidos, detalhes)), \
         patch('app.services.zapi.enviar_texto',
               return_value={'ok': zapi_ok}) as env:
        out = vigia.vigiar()
    return out, env


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _sem_cooldown(monkeypatch):
    """Cooldown OFF por padrão nos testes (os de anti-flood religam)."""
    monkeypatch.setenv('VENDA_SEM_ITEM_COOLDOWN_MIN', '0')


def test_detecta_e_alerta_uma_vez(app):
    out, env = _rodar([_pedido('a1', 1135.00), _pedido('a2', 578.00),
                       _pedido('b1', 50.00, itens=1)])
    assert out['enviado'] is True and out['novas'] == 2
    msg = env.call_args[0][1]
    assert 'R$ 1.135,00' in msg and 'SEM NF' in msg and '678071' in msg
    assert 'NEBRASKA' in msg
    # 2ª rodada com os MESMOS pedidos: nada novo, sem WhatsApp
    out2, env2 = _rodar([_pedido('a1', 1135.00), _pedido('a2', 578.00)])
    assert out2['novas'] == 0 and not env2.called


def test_nova_cobranca_alerta_so_o_delta(app):
    _rodar([_pedido('a1', 100.00)])
    out, env = _rodar([_pedido('a1', 100.00), _pedido('a2', 200.00)])
    assert out['novas'] == 1
    msg = env.call_args[0][1]
    assert 'R$ 200,00' in msg and '1 nova(s)' in msg
    # acumulado do dia considera as duas
    assert 'R$ 300,00' in msg


def test_ignora_cancelada_com_itens_e_total_zero(app):
    out, env = _rodar([
        _pedido('x1', 300.00, cancelado=True),
        _pedido('x2', 300.00, itens=1),
        _pedido('x3', 0.00),
    ])
    assert out['novas'] == 0 and not env.called


def test_piso_por_env(app, monkeypatch):
    monkeypatch.setenv('VENDA_SEM_ITEM_MIN_VALOR', '100')
    out, env = _rodar([_pedido('p1', 50.00), _pedido('p2', 150.00)])
    assert out['novas'] == 1
    assert 'R$ 150,00' in env.call_args[0][1]


def test_envio_falho_nao_marca_e_retenta(app):
    out, _ = _rodar([_pedido('f1', 500.00)], zapi_ok=False)
    assert out['enviado'] is False
    # próximo ciclo: a MESMA cobrança volta a alertar (não foi marcada)
    out2, env2 = _rodar([_pedido('f1', 500.00)], zapi_ok=True)
    assert out2['enviado'] is True and out2['novas'] == 1
    assert env2.called


def test_sem_dono_configurado_nao_marca(app):
    out, env = _rodar([_pedido('d1', 500.00)], dono='')
    assert out['enviado'] is False and not env.called
    # configurado depois: alerta sai
    out2, env2 = _rodar([_pedido('d1', 500.00)])
    assert out2['enviado'] is True and env2.called


def test_kill_switch(app, monkeypatch):
    monkeypatch.setenv('VENDA_SEM_ITEM_VIGIA', '0')
    out = vigia.vigiar()
    assert out['rodou'] is False


def test_estado_poda_dias_fora_da_janela(app):
    velho = (hoje() - timedelta(days=5)).isoformat()
    AppConfig.set(vigia._KEY_ESTADO, json.dumps({'ids': {velho: ['antigo']}}))
    db.session.commit()
    _rodar([_pedido('n1', 100.00)])
    estado = json.loads(AppConfig.get(vigia._KEY_ESTADO))
    assert velho not in estado['ids']
    assert 'n1' in estado['ids'][hoje().isoformat()]


def test_estado_formato_antigo_migra(app):
    """Compat: estado no formato antigo ({data: [ids]} direto) segue
    deduplicando — o vigia pode ter rodado em prod antes do anti-flood."""
    h = hoje().isoformat()
    AppConfig.set(vigia._KEY_ESTADO, json.dumps({h: ['a1']}))
    db.session.commit()
    out, env = _rodar([_pedido('a1', 100.00)])
    assert out['novas'] == 0 and not env.called


def test_cooldown_acumula_sem_perder(app, monkeypatch):
    """Anti-flood (dono 18/07): 1ª alerta na hora; dentro do cooldown as
    novas ACUMULAM (ids não marcados) e saem juntas quando a janela abre."""
    monkeypatch.setenv('VENDA_SEM_ITEM_COOLDOWN_MIN', '60')
    out1, env1 = _rodar([_pedido('c1', 100.00)])
    assert out1['enviado'] is True and env1.called
    # 2ª cobrança logo depois: suprimida, mas NÃO marcada
    out2, env2 = _rodar([_pedido('c1', 100.00), _pedido('c2', 200.00)])
    assert out2['enviado'] is False and 'cooldown' in out2['motivo']
    assert not env2.called
    # cooldown desligado (janela abriu): a acumulada sai agora
    monkeypatch.setenv('VENDA_SEM_ITEM_COOLDOWN_MIN', '0')
    out3, env3 = _rodar([_pedido('c1', 100.00), _pedido('c2', 200.00)])
    assert out3['enviado'] is True and out3['novas'] == 1
    assert 'R$ 200,00' in env3.call_args[0][1]


def test_teto_de_mensagens_por_dia(app, monkeypatch):
    monkeypatch.setenv('VENDA_SEM_ITEM_MAX_MSGS_DIA', '2')
    _rodar([_pedido('m1', 10.00)])
    _rodar([_pedido('m1', 10.00), _pedido('m2', 20.00)])
    out3, env3 = _rodar([_pedido('m1', 10.00), _pedido('m2', 20.00),
                         _pedido('m3', 30.00)])
    assert out3['enviado'] is False and 'teto' in out3['motivo']
    assert not env3.called


def test_api_fora_nao_derruba(app):
    with patch('app.services.seru.listar_pedidos_completo',
               side_effect=RuntimeError('API caiu')):
        out = vigia.vigiar()
    assert out['rodou'] is True and 'erro' in out


def test_rota_admin_exige_owner(app, admin_user):
    uid = admin_user.id
    c = app.test_client()
    with c.session_transaction() as s:
        s['_user_id'] = str(uid)
        s['_fresh'] = True
    # admin comum (nao owner) → 403
    assert c.get('/admin/vigia-venda-sem-item').status_code == 403


def test_rota_admin_dry_run_owner(app, owner_user):
    uid = owner_user.id
    c = app.test_client()
    with c.session_transaction() as s:
        s['_user_id'] = str(uid)
        s['_fresh'] = True
    peds = [_pedido('r1', 250.00)]
    with patch('app.services.seru.listar_pedidos_completo',
               return_value=peds), \
         patch('app.services.seru.extrair_itens',
               side_effect=_extrair_itens_fake), \
         patch('app.services.seru.detalhes_pedido',
               side_effect=_detalhe_fake(peds, None)), \
         patch('app.services.zapi.enviar_texto') as env:
        r = c.get('/admin/vigia-venda-sem-item')
    assert r.status_code == 200
    d = r.get_json()
    assert d['ok'] is True and d['novas'] == 1
    assert d['cobrancas'][0]['total'] == 250.00
    assert not env.called                      # dry-run nunca manda WhatsApp


def test_pedido_malformado_nao_cega_a_varredura(app):
    """Achado de revisão: UM pedido com total torto não pode matar a
    varredura inteira (e repetir a cegueira a cada ciclo)."""
    torto = {'id': 'z9', 'total': '1.135,00-lixo', 'createdAt': 'x',
             'company': None, 'items': []}
    out, env = _rodar([torto, _pedido('ok1', 90.00)])
    assert out['enviado'] is True and out['novas'] == 1
    assert 'R$ 90,00' in env.call_args[0][1]


def test_nf_cancelada_conta_como_sem_nf(app):
    ped = _pedido('nf1', 120.00)
    ped['taxInvoice'] = {'status': 'canceled', 'number': '9'}
    out, env = _rodar([ped])
    assert out['enviado'] is True
    assert 'SEM NF' in env.call_args[0][1]


def test_delivery_99food_nao_alerta(app):
    """Dono 18/07: pedido de delivery (99Food) chega sem itens por natureza
    da integração — venda real, rotina, NÃO alerta."""
    ped = _pedido('df1', 81.38)
    ped['salesChannel'] = {'name': '99Food', 'tag': '99food'}
    out, env = _rodar([ped, _pedido('av1', 44.00)])
    assert out['novas'] == 1                       # só a avulsa
    msg = env.call_args[0][1]
    assert 'R$ 44,00' in msg and 'R$ 81,38' not in msg


def test_cancelada_por_status_nao_alerta(app):
    """Caso real 18/07 (cód 19797307): cancelada veio com status='canceled'
    e canceledAt VAZIO — não pode alertar como venda."""
    ped = _pedido('cs1', 45.00)
    ped['status'] = 'canceled'
    out, env = _rodar([ped])
    assert out['novas'] == 0 and not env.called


def test_canal_aparece_na_mensagem(app):
    ped = _pedido('cn1', 64.00)
    ped['salesChannel'] = {'name': 'PDV Fácil', 'tag': 'pdv-facil'}
    _, env = _rodar([ped])
    assert 'PDV Fácil' in env.call_args[0][1]


# ── Re-verificação no detalhe (21/07/2026, caso R$155 O Pao Padaria) ─────────
# A listagem da Seru ATRASA pra cobrança recém-criada (sem itens/NF na lista
# mesmo já com NFC-e autorizada) — o vigia re-confere no detalhe antes de
# acusar. Decisão do dono: só reverificar, sem carência.

def test_lag_da_lista_nfce_no_detalhe_nao_alerta(app):
    """Cobrança sem itens/NF na LISTA, mas com NFC-e AUTORIZADA no DETALHE
    (o lag do caso R$155) — venda real, NÃO alerta."""
    lista = _pedido('r155', 155.00)                 # lista: item-less, sem NF
    detalhe = _pedido('r155', 155.00)
    detalhe['taxInvoice'] = {'status': 'authorized', 'number': '30126'}
    out, env = _rodar([lista], detalhes={'r155': detalhe})
    assert out['novas'] == 0 and not env.called


def test_lag_da_lista_itens_no_detalhe_nao_alerta(app):
    """Sem itens na LISTA, mas COM itens não-cancelados no DETALHE — real."""
    lista = _pedido('r2', 90.00)                    # lista: item-less
    detalhe = _pedido('r2', 90.00, itens=1)         # detalhe: tem item
    out, env = _rodar([lista], detalhes={'r2': detalhe})
    assert out['novas'] == 0 and not env.called


def test_nfce_em_contingencia_nao_alerta(app):
    """NFC-e em CONTINGÊNCIA (emitida offline quando a SEFAZ/internet cai) é
    nota fiscal válida COM produtos — NÃO alerta (caso Nebraska cód 19989588,
    23/07/2026: café + cookie, NFC-e nº 2360 em contingência). O detalhe pode
    até estar sem itens por lag, mas a NF em contingência já basta."""
    lista = _pedido('n2360', 27.50)                 # lista: item-less, sem NF
    detalhe = _pedido('n2360', 27.50)
    detalhe['taxInvoice'] = {'status': 'contingency', 'number': '2360'}
    out, env = _rodar([lista], detalhes={'n2360': detalhe})
    assert out['novas'] == 0 and not env.called


def test_fantasma_confirmado_pelo_detalhe_alerta(app):
    """Sem itens e sem NF na LISTA E no DETALHE (fantasma real, tipo cód
    19875201: itens todos cancelados, sem NF) — alerta normalmente."""
    lista = _pedido('f1', 256.00)
    # detalhe idêntico (default) = item-less, sem NF autorizada
    out, env = _rodar([lista])
    assert out['enviado'] is True and out['novas'] == 1
    assert 'R$ 256,00' in env.call_args[0][1]


def test_detalhe_indisponivel_adia_nao_marca(app):
    """Detalhe indisponível (Seru fora): NÃO alerta nesse ciclo e NÃO marca
    o id — retenta no próximo (perder um ciclo < falso alarme)."""
    lista = _pedido('d1', 300.00)
    out, env = _rodar([lista], detalhes={'d1': _SEM_DETALHE})
    assert out['novas'] == 0 and not env.called
    # id não marcado: quando o detalhe voltar (confirmando fantasma), alerta.
    out2, env2 = _rodar([lista])                    # detalhe default (fantasma)
    assert out2['enviado'] is True and out2['novas'] == 1


def test_detalhe_nfce_autorizada_nao_marca_e_some(app):
    """A venda real (NFC-e no detalhe) não é marcada como alertada — se
    virar suspeita de novo por outro motivo, o dedup não a esconde."""
    lista = _pedido('r3', 155.00)
    det = _pedido('r3', 155.00)
    det['taxInvoice'] = {'status': 'authorized', 'number': '1'}
    out, _ = _rodar([lista], detalhes={'r3': det})
    assert out.get('novas', 0) == 0
    estado = vigia.estado_dedup([hoje() - timedelta(days=1), hoje()])
    ids = {i for v in estado['ids'].values() for i in v}
    assert 'r3' not in ids


def test_claim_e_gravado_ANTES_do_envio(app):
    """Anti-duplicata 19/08/2026 (cód 21097090 alertado às 19:19 E 19:20):
    os ids têm que estar COMMITADOS quando o WhatsApp sai — se o deploy
    matar o container entre o envio e a gravação, o container novo NÃO pode
    re-alertar. O mock de envio lê o estado direto do banco no momento do
    disparo e trava a ordem."""
    from flask import current_app
    visto = {}

    def _envia(numero, msg):
        estado = json.loads(AppConfig.get(vigia._KEY_ESTADO) or '{}')
        ids = {i for lst in (estado.get('ids') or {}).values() for i in lst}
        visto['marcado_no_envio'] = 'k1' in ids
        return {'ok': True}

    current_app.config['ZAPI_BOT_DONO_NUMERO'] = '5511999999999'
    current_app.config['CHATWOOT_VIGIA_INFRA_NUMERO'] = ''
    pedidos = [_pedido('k1', 500.00)]
    with patch('app.services.seru.listar_pedidos_completo',
               return_value=pedidos), \
         patch('app.services.seru.extrair_itens',
               side_effect=_extrair_itens_fake), \
         patch('app.services.seru.detalhes_pedido',
               side_effect=_detalhe_fake(pedidos, None)), \
         patch('app.services.zapi.enviar_texto', side_effect=_envia):
        out = vigia.vigiar()
    assert out['enviado'] is True
    assert visto['marcado_no_envio'] is True


def test_envio_falho_devolve_o_claim_inteiro(app):
    """Envio falho não pode consumir cooldown/teto: ultimo_envio e envios
    voltam ao que eram (o claim é devolvido por inteiro, não só os ids)."""
    out, _ = _rodar([_pedido('cw1', 500.00)], zapi_ok=False)
    assert out['enviado'] is False
    estado = vigia.estado_dedup([hoje() - timedelta(days=1), hoje()])
    ids = {i for v in estado['ids'].values() for i in v}
    assert 'cw1' not in ids
    assert estado['ultimo_envio'] is None
    assert estado['envios'] == {}
