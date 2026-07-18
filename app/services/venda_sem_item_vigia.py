"""Vigia de venda SEM itens no PDV — alerta imediato no WhatsApp (18/07/2026).

Caso real (Nebraska, 17/07/2026): 23 cobranças do canal "PDV Fácil" com valor
mas SEM NENHUM produto — R$ 7.028,50 num dia, TODAS sem NF e sem forma de
pagamento, num único caixa. O painel da Seru não as mostra (relatório deles é
por produto/nota) e aqui dentro elas só aparecem no total do pedido
(`faturamento_pedidos`) — não baixam estoque, não entram na previsão de
demanda nem no relatório de produtos. O dono pediu alerta IMEDIATO.

Funcionamento (pedido do dono, 18/07/2026):
- Roda a CADA ciclo do sync Seru (15min, dentro do advisory lock do
  `_run_sync` — execução única entre workers, sem alerta duplicado).
- Varre os pedidos de ONTEM+HOJE na API (mesma janela da captura do
  snapshot); cobrança suspeita = não cancelada, total > piso e ZERO itens
  não-cancelados.
- Dedup POR PEDIDO em AppConfig (`venda_sem_item_alertados` = JSON
  {data: [ids]}, podado pra janela): cada cobrança alerta UMA vez; um
  ciclo com várias novas vira UMA mensagem, agrupada por company, com
  hora, valor, NF (tem/não tem) e caixa.
- Se o envio falhar (Z-API fora), os ids NÃO são marcados — retenta no
  próximo ciclo. Perder alerta de possível fraude é pior que duplicar.

Config (env):
- `VENDA_SEM_ITEM_VIGIA=0` desliga o job no cron (kill-switch, padrão).
- `VENDA_SEM_ITEM_MIN_VALOR` (default 0) — piso em R$ por cobrança; abaixo
  dele não alerta (ex: 100 pra ignorar cobrança avulsa pequena de balcão).

Sob demanda: `GET /admin/vigia-venda-sem-item` (owner; `?alertar=1` roda o
fluxo com WhatsApp). Sonda externa: `/api/claude/vendas-snapshot?pedidos=1`.
"""
import json
import logging
import os
from datetime import timedelta
from decimal import Decimal, InvalidOperation

logger = logging.getLogger(__name__)

_KEY_ESTADO = 'venda_sem_item_alertados'
_MAX_LINHAS_MSG = 8


def min_valor():
    """Piso em R$ por cobrança (Decimal), via env `VENDA_SEM_ITEM_MIN_VALOR`.
    Inválido cai em 0 com WARNING (nunca desliga o vigia em silêncio)."""
    bruto = (os.environ.get('VENDA_SEM_ITEM_MIN_VALOR') or '').strip()
    if not bruto:
        return Decimal('0')
    try:
        v = Decimal(bruto.replace(',', '.'))
        if v < 0:
            raise InvalidOperation(bruto)
        return v
    except Exception:  # noqa: BLE001 — env torta não pode matar o vigia
        logger.warning('VENDA_SEM_ITEM_MIN_VALOR inválido (%r) — usando 0',
                       bruto)
        return Decimal('0')


def cobrancas_sem_itens(data_inicial, data_final):
    """Cobranças com valor e ZERO itens na janela, direto da API do Seru.

    Cada uma: {id, codigo, data, hora, company, total, caixa, tem_nf}.
    Canceladas e total <= piso ficam fora. Levanta exceção se a API cair —
    o chamador decide (o vigiar() engole e reporta)."""
    from app.services import seru

    piso = min_valor()
    out = []
    for p in seru.listar_pedidos_completo(data_inicial, data_final):
        if not isinstance(p, dict) or p.get('canceledAt'):
            continue
        total = Decimal(str(p.get('total') or 0))
        if total <= piso or total <= 0:
            continue
        if any(not it['cancelado'] for it in seru.extrair_itens(p)):
            continue
        dh = seru.datahora_local(p.get('createdAt'))
        if not dh or not (data_inicial <= dh.date() <= data_final):
            continue
        out.append({
            'id': str(p.get('id') or p.get('code') or ''),
            'codigo': p.get('code'),
            'data': dh.date().isoformat(),
            'hora': dh.strftime('%H:%M'),
            'company': ((p.get('company') or {}).get('name') or '(sem loja)'),
            'total': float(total),
            'caixa': (p.get('cashier') or {}).get('code'),
            'tem_nf': bool(p.get('taxInvoice')),
        })
    return out


def _carregar_estado(janela_datas):
    """Estado {data_iso: [ids ja alertados]}, podado pra janela vigente."""
    from app.models import AppConfig
    try:
        bruto = json.loads(AppConfig.get(_KEY_ESTADO) or '{}')
    except (ValueError, TypeError):
        bruto = {}
    validas = {d.isoformat() for d in janela_datas}
    return {k: list(v) for k, v in bruto.items()
            if k in validas and isinstance(v, list)}


def _gravar_estado(estado):
    from app.extensions import db
    from app.models import AppConfig
    AppConfig.set(_KEY_ESTADO, json.dumps(estado))
    db.session.commit()


def _montar_mensagem(novas, todas):
    """Uma mensagem por ciclo, agrupada por company. `todas` dá o contexto
    do dia (total acumulado do padrão, não só as novas)."""
    por_comp = {}
    for c in novas:
        por_comp.setdefault(c['company'], []).append(c)
    blocos = []
    for comp, lst in sorted(por_comp.items()):
        lst.sort(key=lambda x: (x['data'], x['hora']), reverse=True)
        soma = sum(c['total'] for c in lst)
        dia_lst = [c for c in todas if c['company'] == comp
                   and c['data'] == lst[0]['data']]
        dia_soma = sum(c['total'] for c in dia_lst)
        linhas = []
        for c in lst[:_MAX_LINHAS_MSG]:
            nf = 'com NF' if c['tem_nf'] else 'SEM NF'
            linhas.append(f'• {c["data"][8:10]}/{c["data"][5:7]} {c["hora"]} '
                          f'— R$ {c["total"]:,.2f} — {nf} — caixa '
                          f'{c["caixa"] or "?"} (cód {c["codigo"] or "?"})')
        if len(lst) > _MAX_LINHAS_MSG:
            linhas.append(f'• …e mais {len(lst) - _MAX_LINHAS_MSG}')
        blocos.append(
            f'*{comp}*: {len(lst)} nova(s), R$ {soma:,.2f}\n'
            + '\n'.join(linhas)
            + f'\nAcumulado do dia nesse padrão: R$ {dia_soma:,.2f} '
              f'({len(dia_lst)} cobrança(s)).')
    corpo = '\n\n'.join(blocos)
    return ('🚨 *Cobrança SEM itens no PDV* — valor lançado sem nenhum '
            f'produto:\n\n{corpo}\n\n'
            'Venda sem item não emite NF pelo fluxo normal, não baixa '
            'estoque e não entra na previsão. Confira com a loja o que '
            'foi cobrado.')


def vigiar():
    """Varre ontem+hoje, alerta as cobranças NOVAS e marca no estado.
    Nunca levanta exceção (contrato dos vigias — o cron segue vivo)."""
    from flask import current_app

    from app.services import zapi
    from app.utils import hoje as _hoje

    if os.environ.get('VENDA_SEM_ITEM_VIGIA', '1') == '0':
        return {'rodou': False, 'motivo': 'kill-switch'}

    hoje_d = _hoje()
    janela = [hoje_d - timedelta(days=1), hoje_d]
    try:
        todas = cobrancas_sem_itens(janela[0], janela[-1])
    except Exception as e:  # noqa: BLE001 — API fora não derruba o cron
        logger.exception('vigia venda sem item: consulta da API falhou')
        return {'rodou': True, 'erro': f'{type(e).__name__}: {str(e)[:160]}'}

    estado = _carregar_estado(janela)
    ja = {i for ids in estado.values() for i in ids}
    novas = [c for c in todas if c['id'] and c['id'] not in ja]
    if not novas:
        _gravar_estado(estado)          # persiste a poda de dias velhos
        return {'rodou': True, 'novas': 0, 'total_janela': len(todas)}

    cfg = current_app.config
    dono = ((cfg.get('CHATWOOT_VIGIA_INFRA_NUMERO') or '').strip()
            or (cfg.get('ZAPI_BOT_DONO_NUMERO') or '').strip())
    if not dono:
        # Sem destino não marca os ids: quando configurar, alerta tudo.
        logger.warning('vigia venda sem item: %d cobrança(s) nova(s) e '
                       'NENHUM número de dono configurado', len(novas))
        return {'rodou': True, 'novas': len(novas), 'enviado': False,
                'motivo': 'sem numero do dono'}

    msg = _montar_mensagem(novas, todas)
    try:
        r = zapi.enviar_texto(dono, msg, critico=True)
        ok = bool(r.get('ok')) if isinstance(r, dict) else False
    except Exception:  # noqa: BLE001
        logger.exception('vigia venda sem item: envio WhatsApp explodiu')
        ok = False
    if not ok:
        # Não marca: retenta no próximo ciclo (perder alerta é pior).
        return {'rodou': True, 'novas': len(novas), 'enviado': False,
                'motivo': 'envio falhou — retenta no proximo ciclo'}
    for c in novas:
        estado.setdefault(c['data'], []).append(c['id'])
    _gravar_estado(estado)
    return {'rodou': True, 'novas': len(novas), 'enviado': True,
            'valor_novas': round(sum(c['total'] for c in novas), 2)}
