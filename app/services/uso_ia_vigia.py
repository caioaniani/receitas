"""Vigia de CUSTO de IA — teto diário + alerta WhatsApp (11/07/2026).

Toda chamada de IA é registrada em `UsoIA` desde 25/06/2026, mas o
relatório /admin/uso-ia é PASSIVO — só informa quando o dono abre a
página. Um loop de bot ou prompt gigante recorrente dispararia o custo em
silêncio: quase tudo no sistema tem vigia proativo (site, PDV, Chatwoot,
desperdício), menos o gasto de IA. Este vigia compara o gasto de HOJE
(desde 00:00 BRT — mesma base do painel) com um teto em USD e alerta o
dono no WhatsApp na TRANSIÇÃO abaixo→acima do teto, re-alerta a cada 6h
enquanto estourado e avisa uma vez quando normalizar (dia novo abaixo do
teto) — mesmo padrão do vigia do site (estado em AppConfig, sobrevive a
deploy). Cron de hora em hora em `seru_cron`.

Config (env):
- `USO_IA_TETO_DIA_USD` (default 25.0) — teto diário em USD. O maior
  consumidor recorrente é o vigia do chatbot (roda a CADA resposta do
  bot); 25 USD/dia fica bem acima do uso normal e bem abaixo de um
  runaway de horas. Ajustar conforme o /admin/uso-ia real.
- `USO_IA_VIGIA=0` desliga o job no cron (kill-switch, padrão dos vigias).

Chamada de modelo DESCONHECIDO fica com `custo_usd` NULL em UsoIA e NÃO
soma no gasto — o resultado expõe `sem_preco` (quantas existem hoje) pra
não dar falsa segurança: o gasto real pode ser maior.
"""
import logging
import os
from datetime import datetime, time
from decimal import Decimal

from sqlalchemy import func

logger = logging.getLogger(__name__)

_REALERTA_MIN = 360      # re-alerta a cada 6h enquanto estourado
_KEY_ESTOURADO = 'uso_ia_vigia_estourado_desde'
_KEY_ULTIMO = 'uso_ia_vigia_ultimo_alerta_em'
_KEY_ASSIN = 'uso_ia_vigia_ultima_assinatura'

_TETO_DEFAULT = Decimal('25')


def teto_dia_usd():
    """Teto diário em USD (Decimal), via env `USO_IA_TETO_DIA_USD`. Valor
    inválido cai no default com WARNING (nunca desliga o vigia em silêncio
    por erro de digitação no Railway)."""
    bruto = (os.environ.get('USO_IA_TETO_DIA_USD') or '').strip()
    if not bruto:
        return _TETO_DEFAULT
    try:
        teto = Decimal(bruto)
        if teto <= 0:
            raise ValueError(bruto)
        return teto
    except Exception:  # noqa: BLE001 — env torta não pode matar o vigia
        logger.warning('USO_IA_TETO_DIA_USD inválido (%r) — usando default '
                       '%s', bruto, _TETO_DEFAULT)
        return _TETO_DEFAULT


def gasto_hoje():
    """Gasto de HOJE (desde 00:00 BRT): (total_usd Decimal, sem_preco int,
    top list). `top` = 3 maiores funções do dia [(funcao, usd), ...] pro
    alerta dizer QUEM está gastando, não só quanto."""
    from app.extensions import db
    from app.models import UsoIA
    from app.utils import hoje

    corte = datetime.combine(hoje(), time.min)
    total = (db.session.query(func.coalesce(func.sum(UsoIA.custo_usd), 0))
             .filter(UsoIA.criado_em >= corte).scalar()) or 0
    sem_preco = (db.session.query(func.count(UsoIA.id))
                 .filter(UsoIA.criado_em >= corte,
                         UsoIA.custo_usd.is_(None)).scalar()) or 0
    top = (db.session.query(UsoIA.funcao,
                            func.coalesce(func.sum(UsoIA.custo_usd), 0))
           .filter(UsoIA.criado_em >= corte)
           .group_by(UsoIA.funcao)
           .order_by(func.coalesce(func.sum(UsoIA.custo_usd), 0).desc())
           .limit(3).all())
    return (Decimal(total), int(sem_preco),
            [(f, Decimal(c or 0)) for f, c in top])


def rodar_checks():
    """Compara o gasto de hoje com o teto. Read-only, nunca levanta exceção
    (mesmo contrato dos outros vigias)."""
    teto = teto_dia_usd()
    try:
        gasto, sem_preco, top = gasto_hoje()
    except Exception as e:  # noqa: BLE001 — vigia nunca derruba o cron
        logger.exception('vigia uso IA: consulta do gasto explodiu')
        # Erro de BANCO envenena a sessao — sem rollback, o _carregar do
        # vigiar() estoura na sequencia e o alerta "vigia cego" nunca sai
        # (justo no cenario-alvo dele).
        try:
            from app.extensions import db
            db.session.rollback()
        except Exception:  # noqa: BLE001
            pass
        return {'saudavel': False, 'gasto_usd': None,
                'teto_usd': float(teto), 'sem_preco': None, 'top': [],
                'problemas': [f'consulta do gasto de IA explodiu: {e}']}
    problemas = []
    if gasto >= teto:
        problemas.append(
            f'gasto de IA de hoje US$ {gasto:.2f} passou do teto '
            f'US$ {teto:.2f}')
    return {'saudavel': not problemas,
            'gasto_usd': float(gasto), 'teto_usd': float(teto),
            'sem_preco': sem_preco,
            'top': [(f, float(c)) for f, c in top],
            'problemas': problemas}


def _carregar():
    from app.models import AppConfig

    def _parse(s):
        if not s:
            return None
        try:
            return datetime.fromisoformat(s)
        except ValueError:
            return None
    return {'estourado_desde': _parse(AppConfig.get(_KEY_ESTOURADO)),
            'ultimo_alerta_em': _parse(AppConfig.get(_KEY_ULTIMO)),
            'ultima_assinatura': AppConfig.get(_KEY_ASSIN)}


def _gravar(est):
    from app.extensions import db
    from app.models import AppConfig

    def _fmt(v):
        return v.isoformat() if v else None
    AppConfig.set(_KEY_ESTOURADO, _fmt(est.get('estourado_desde')))
    AppConfig.set(_KEY_ULTIMO, _fmt(est.get('ultimo_alerta_em')))
    AppConfig.set(_KEY_ASSIN, est.get('ultima_assinatura'))
    db.session.commit()


def vigiar():
    """Roda o check e alerta o dono no WhatsApp quando o gasto estoura.

    Anti-spam do padrão dos vigias, com um cuidado próprio: a ASSINATURA é
    só o teto (não o gasto) — o gasto cresce a cada rodada e, se entrasse
    na assinatura, "mudou" seria sempre True e o alerta spamaria de hora em
    hora. Re-alerta fica por conta da janela de 6h."""
    from flask import current_app

    from app.services import zapi
    from app.utils import agora as _agora

    cfg = current_app.config
    dono = ((cfg.get('CHATWOOT_VIGIA_INFRA_NUMERO') or '').strip()
            or (cfg.get('ZAPI_BOT_DONO_NUMERO') or '').strip())

    out = rodar_checks()
    est = _carregar()
    agora_dt = _agora()

    if out['saudavel']:
        if est['estourado_desde'] is not None:
            if dono:
                zapi.enviar_texto(dono, '✅ Custo de IA normalizou — gasto '
                                        'do dia voltou pra baixo do teto '
                                        f'(US$ {out["teto_usd"]:.2f}/dia).')
            _gravar({'estourado_desde': None, 'ultimo_alerta_em': None,
                     'ultima_assinatura': None})
            return {'rodou': True, 'enviado': bool(dono),
                    'tipo': 'recuperacao', **out}
        return {'rodou': True, 'enviado': False, 'tipo': 'saudavel', **out}

    assinatura = f'teto:{out["teto_usd"]}'
    mudou = assinatura != est['ultima_assinatura']
    venceu = (est['ultimo_alerta_em'] is None
              or (agora_dt - est['ultimo_alerta_em']).total_seconds()
              >= _REALERTA_MIN * 60)
    if est['estourado_desde'] is None:
        est['estourado_desde'] = agora_dt
    if dono and (mudou or venceu):
        if out['gasto_usd'] is None:
            # A propria consulta do gasto quebrou (rodar_checks devolve
            # gasto None + problema descritivo). Sem este ramo, o f-string
            # do gasto estourava TypeError e o cron engolia — o dono nunca
            # saberia que o VIGIA esta cego.
            cabeca = ('🚨 Custo de IA — o vigia não conseguiu medir o '
                      'gasto:\n'
                      + '\n'.join('• ' + p for p in out['problemas'][:5]))
        else:
            linhas = '\n'.join(f'• {f}: US$ {c:.2f}' for f, c in out['top'])
            extra = ''
            if out.get('sem_preco'):
                extra = (f'\n(+{out["sem_preco"]} chamada(s) de modelo sem '
                         'preço na tabela — gasto real pode ser maior.)')
            cabeca = ('🚨 Custo de IA — o gasto de HOJE já passou do teto: '
                      f'US$ {out["gasto_usd"]:.2f} de US$ '
                      f'{out["teto_usd"]:.2f}.\n\n'
                      f'Maiores funções hoje:\n{linhas}{extra}')
        zapi.enviar_texto(dono, (f'{cabeca}\n\n'
                                 'Detalhe: /admin/uso-ia?dias=1 — se for '
                                 'loop/abuso, os kill-switches são os das '
                                 'funções (CHATBOT_*, SLACK_*); o teto é '
                                 'USO_IA_TETO_DIA_USD.'))
        est['ultimo_alerta_em'] = agora_dt
        est['ultima_assinatura'] = assinatura
        _gravar(est)
        return {'rodou': True, 'enviado': True, 'tipo': 'alerta', **out}
    _gravar(est)
    return {'rodou': True, 'enviado': False, 'tipo': 'alerta_suprimido',
            **out}
