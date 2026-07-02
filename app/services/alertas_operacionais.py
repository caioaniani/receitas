"""Alertas de BAIXA PRESA no WhatsApp do dono (03/07/2026).

Dois estados em que o estoque fica ERRADO até alguém agir fisicamente — e que
antes ninguém via até a contagem divergir (auditoria das baixas):

- **Pedido preso em 'separado'** com a data de entrega já passada: o rótulo na
  UI diz "enviado", mas a baixa da indústria SÓ acontece quando o motorista
  escaneia o QR (separado→em_transporte). Parado aqui = o pão saiu (ou nunca
  saiu) sem baixar estoque.
- **Retirada de sobra presa em 'em_transporte'** há mais de N horas: a COLETA
  já baixou o estoque da loja, mas o QR de recebimento nunca foi lido — a
  indústria nunca é creditada e a sobra "some" do ledger.

Cron a cada 30 min (`seru_cron`). Anti-spam: só re-envia se o CONJUNTO de
pendências mudou ou se passaram 6h do último alerta (estado em `AppConfig`,
sobrevive a deploy). Kill-switch: env `ALERTA_BAIXAS_PRESAS=0`.
"""
import hashlib
import json
import logging
import os
from datetime import timedelta

from flask import current_app

from app.extensions import db
from app.utils import agora, hoje

logger = logging.getLogger(__name__)

_CFG_KEY = 'alerta_baixas_presas'
_REALERTA_HORAS = 6


def _retirada_presa_horas():
    try:
        return max(1, int(os.environ.get('RETIRADA_PRESA_HORAS', '12')))
    except (TypeError, ValueError):
        return 12


def verificar_baixas_presas():
    """Levanta as pendências. Retorna {'separados': [...], 'retiradas': [...]}
    (listas de dicts leves, prontas pra mensagem)."""
    from app.models import PedidoLoja, RetiradaSobra

    hoje_d = hoje()
    separados = [
        {'id': p.id, 'loja': p.loja.nome if p.loja else '?',
         'entrega': p.data_entrega.strftime('%d/%m') if p.data_entrega else '?'}
        for p in (PedidoLoja.query
                  .filter(PedidoLoja.status == 'separado',
                          PedidoLoja.data_entrega < hoje_d)
                  .order_by(PedidoLoja.data_entrega)
                  .limit(20).all())
    ]

    limite = agora() - timedelta(hours=_retirada_presa_horas())
    retiradas = [
        {'id': r.id, 'loja': r.loja.nome if r.loja else '?',
         'coletada_em': (r.coletada_em.strftime('%d/%m %H:%M')
                         if r.coletada_em else '?')}
        for r in (RetiradaSobra.query
                  .filter(RetiradaSobra.status == 'em_transporte',
                          RetiradaSobra.coletada_em.isnot(None),
                          RetiradaSobra.coletada_em < limite)
                  .order_by(RetiradaSobra.coletada_em)
                  .limit(20).all())
    ]
    return {'separados': separados, 'retiradas': retiradas}


def _montar_mensagem(d):
    partes = ['🚨 BAIXAS PRESAS — estoque errado até resolver']
    if d['separados']:
        partes.append(f"\n📦 {len(d['separados'])} pedido(s) parados em "
                      '"separado" com entrega VENCIDA (QR de saída não '
                      'escaneado — indústria NÃO baixou):')
        for p in d['separados']:
            partes.append(f"  • #{p['id']} {p['loja']} (entrega {p['entrega']})")
        partes.append('  → escanear o QR de saída, ou /pedidos (enviar).')
    if d['retiradas']:
        h = _retirada_presa_horas()
        partes.append(f"\n♻️ {len(d['retiradas'])} retirada(s) de sobra em "
                      f'transporte há mais de {h}h (loja JÁ baixou; indústria '
                      'NÃO creditada):')
        for r in d['retiradas']:
            partes.append(f"  • retirada #{r['id']} {r['loja']} "
                          f"(coletada {r['coletada_em']})")
        partes.append('  → escanear o QR de recebimento na indústria.')
    return '\n'.join(partes)


def _hash_conjunto(d):
    chave = json.dumps({'s': sorted(p['id'] for p in d['separados']),
                        'r': sorted(r['id'] for r in d['retiradas'])},
                       sort_keys=True)
    return hashlib.sha256(chave.encode()).hexdigest()[:16]


def rodar_e_alertar():
    """Ponto de entrada do cron. Verifica, deduplica e manda o WhatsApp."""
    from app.models import AppConfig
    from app.services import zapi

    if os.environ.get('ALERTA_BAIXAS_PRESAS', '1') == '0':
        return {'ok': True, 'desligado': True}

    d = verificar_baixas_presas()
    if not d['separados'] and not d['retiradas']:
        return {'ok': True, 'pendencias': 0}

    numero = (current_app.config.get('ZAPI_NUMERO_DESTINO') or '').strip()
    if not numero:
        logger.info('alerta_baixas_presas: ZAPI_NUMERO_DESTINO vazio, pulando')
        return {'ok': True, 'sem_numero': True}

    novo_hash = _hash_conjunto(d)
    estado = {}
    try:
        estado = json.loads(AppConfig.get(_CFG_KEY) or '{}')
    except (TypeError, ValueError):
        estado = {}
    ultimo_ts = estado.get('ts')
    recente = False
    if ultimo_ts:
        try:
            from datetime import datetime
            recente = (agora() - datetime.fromisoformat(ultimo_ts)
                       < timedelta(hours=_REALERTA_HORAS))
        except ValueError:
            recente = False
    if estado.get('hash') == novo_hash and recente:
        return {'ok': True, 'dedup': True,
                'pendencias': len(d['separados']) + len(d['retiradas'])}

    res = zapi.enviar_texto(numero, _montar_mensagem(d))
    if res.get('ok'):
        AppConfig.set(_CFG_KEY, json.dumps(
            {'hash': novo_hash, 'ts': agora().isoformat()}))
        db.session.commit()
    return {'ok': bool(res.get('ok')),
            'pendencias': len(d['separados']) + len(d['retiradas']),
            'enviado': bool(res.get('ok'))}
