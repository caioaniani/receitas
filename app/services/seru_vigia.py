"""Vigia do SERU (PDV) — detecta pedidos que PARAM de chegar na API.

Criado em 13/07/2026, no dia do incidente das companies: a API do Seru
respondia normalmente (auth + request OK) mas enxergava 1 pedido no dia
inteiro — as vendas das lojas não estavam subindo pra nuvem deles (painel
"Integração SERU - ALL" do Colibri sem as empresas). O dono só percebeu
olhando o faturamento na tela e perguntou "eu preciso perguntar se parou?"
— não deveria: este vigia pergunta sozinho.

Dois checks, nas MESMAS funções do sync:

1. API viva: auth + listar_pedidos de hoje (1 item). Exceção = API fora.
2. VAZÃO: com limit=1, `totalPages` == nº de pedidos do dia na API. Em
   horário de loja (09-21h BRT), o total tem piso por hora — abaixo do
   piso, os pedidos não estão chegando (PDV dessincronizado, empresa fora
   da integração, pipeline do Seru). Pisos CONSERVADORES de propósito
   (dia fraco/feriado não alarma; ~600 pedidos/dia é o normal).

Alerta o dono no WhatsApp (Z-API) na TRANSIÇÃO saudável→doente, re-alerta
a cada 6h e avisa quando normalizar — mesmo padrão do vigia do site
(estado em AppConfig). Cron a cada 30min em `seru_cron` (kill-switch:
SERU_VIGIA=0). Inspeção manual: GET /admin/vigia-seru (owner).
"""
import logging

from flask import current_app

logger = logging.getLogger(__name__)

_REALERTA_MIN = 360      # re-alerta do MESMO problema a cada 6h
_KEY_QUEBRADO = 'seru_vigia_quebrado_desde'
_KEY_ULTIMO = 'seru_vigia_ultimo_alerta_em'
_KEY_ASSIN = 'seru_vigia_ultima_assinatura'

# Piso de pedidos ACUMULADOS do dia por hora BRT (hora >= chave -> piso).
# Conservador: às 11h o dia normal já passou de 100; o piso pede 20.
_PISOS = ((17, 120), (14, 60), (11, 20), (9, 3))

# Janela em que a contagem é avaliada (fora dela, só o check de API viva).
_HORA_INI, _HORA_FIM = 9, 21


def _piso_para_hora(hora):
    for h, piso in _PISOS:
        if hora >= h:
            return piso
    return 0


def rodar_checks():
    """Roda os dois checks. Retorna {'saudavel', 'problemas', 'pedidos_hoje',
    'hora_brt'} — read-only, nunca levanta (exceção vira problema)."""
    from app.services import seru
    from app.utils import agora, hoje

    agora_dt = agora()
    problemas = []
    pedidos_hoje = None
    try:
        resp = seru.listar_pedidos(hoje(), hoje(), page=1, limit=1)
        # limit=1 -> totalPages == total de pedidos do dia visível na API.
        pedidos_hoje = int((resp or {}).get('totalPages') or 0)
        dados = (resp or {}).get('data') or []
        if pedidos_hoje == 0 and not dados:
            pedidos_hoje = 0
    except Exception as e:  # noqa: BLE001 — o erro cru É o diagnóstico
        problemas.append(f'API do Seru fora: {str(e)[:200]}')

    if pedidos_hoje is not None and _HORA_INI <= agora_dt.hour < _HORA_FIM:
        piso = _piso_para_hora(agora_dt.hour)
        if pedidos_hoje < piso:
            problemas.append(
                f'pedidos de HOJE não estão chegando na API do Seru: '
                f'{pedidos_hoje} visível(is) às {agora_dt.strftime("%H:%M")} '
                f'(esperado >= {piso}). Vendas das lojas não estão subindo '
                f'— conferir sincronização dos PDVs e o painel '
                f'"Integração SERU" do Colibri (empresas selecionadas).')

    return {'saudavel': not problemas, 'problemas': problemas,
            'pedidos_hoje': pedidos_hoje,
            'hora_brt': agora_dt.strftime('%H:%M')}


def _carregar():
    from datetime import datetime as _dt

    from app.models import AppConfig

    def _parse(s):
        if not s:
            return None
        try:
            return _dt.fromisoformat(s)
        except ValueError:
            return None
    return {'quebrado_desde': _parse(AppConfig.get(_KEY_QUEBRADO)),
            'ultimo_alerta_em': _parse(AppConfig.get(_KEY_ULTIMO)),
            'ultima_assinatura': AppConfig.get(_KEY_ASSIN)}


def _gravar(est):
    from app.extensions import db
    from app.models import AppConfig

    def _fmt(v):
        return v.isoformat() if v else None
    AppConfig.set(_KEY_QUEBRADO, _fmt(est.get('quebrado_desde')))
    AppConfig.set(_KEY_ULTIMO, _fmt(est.get('ultimo_alerta_em')))
    AppConfig.set(_KEY_ASSIN, est.get('ultima_assinatura'))
    db.session.commit()


def vigiar():
    """Roda os checks e alerta o dono no WhatsApp quando o Seru adoece.
    Anti-spam idêntico aos outros vigias: transição + re-alerta 6h +
    aviso de normalização."""
    from app.services import zapi
    from app.utils import agora as _agora

    cfg = current_app.config
    dono = ((cfg.get('CHATWOOT_VIGIA_INFRA_NUMERO') or '').strip()
            or (cfg.get('ZAPI_BOT_DONO_NUMERO') or '').strip())

    out = rodar_checks()
    est = _carregar()
    agora_dt = _agora()

    if out['saudavel']:
        if est['quebrado_desde'] is not None:
            if dono:
                zapi.enviar_texto(
                    dono, ('✅ Seru normalizou — os pedidos de hoje voltaram '
                           f'a chegar ({out["pedidos_hoje"]} na API). O sync '
                           'recupera o retroativo sozinho.'))
            _gravar({'quebrado_desde': None, 'ultimo_alerta_em': None,
                     'ultima_assinatura': None})
            return {'rodou': True, 'enviado': bool(dono),
                    'tipo': 'recuperacao', **out}
        return {'rodou': True, 'enviado': False, 'tipo': 'saudavel', **out}

    assinatura = ' | '.join(sorted(out['problemas']))[:900]
    mudou = assinatura != est['ultima_assinatura']
    venceu = (est['ultimo_alerta_em'] is None
              or (agora_dt - est['ultimo_alerta_em']).total_seconds()
              >= _REALERTA_MIN * 60)
    if est['quebrado_desde'] is None:
        est['quebrado_desde'] = agora_dt
    if dono and (mudou or venceu):
        linhas = '\n'.join('• ' + p for p in out['problemas'][:4])
        zapi.enviar_texto(dono, ('🚨 Vigia do SERU — problema detectado:\n\n'
                                 f'{linhas}\n\n'
                                 'Vendas das lojas não estão subindo pro '
                                 'Seru — relatórios e baixas de estoque '
                                 'ficam represados até resolver. Detalhe: '
                                 '/admin/vigia-seru e /pdv/debug-seru'))
        est['ultimo_alerta_em'] = agora_dt
        est['ultima_assinatura'] = assinatura
        _gravar(est)
        return {'rodou': True, 'enviado': True, 'tipo': 'alerta', **out}
    _gravar(est)
    return {'rodou': True, 'enviado': False, 'tipo': 'alerta_suprimido', **out}
