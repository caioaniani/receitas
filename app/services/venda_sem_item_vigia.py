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
  não-cancelados NA LISTAGEM — e re-conferida no DETALHE antes de alertar
  (a listagem da Seru atrasa; ver `cobrancas_sem_itens`).
- Dedup POR PEDIDO em AppConfig (`venda_sem_item_alertados` = JSON
  {data: [ids]}, podado pra janela): cada cobrança alerta UMA vez; um
  ciclo com várias novas vira UMA mensagem, agrupada por company, com
  hora, valor, NF (tem/não tem) e caixa.
- Se o envio falhar (Z-API fora), o claim é DEVOLVIDO (ids desmarcados) —
  retenta no próximo ciclo. Perder alerta de possível fraude é pior que
  duplicar.
- Os ids são marcados e COMMITADOS **ANTES** do envio (claim-first,
  19/08/2026, dono: "Continua duplicando"): a ordem antiga (envia → marca)
  duplicava quando o deploy matava o container entre o envio e o commit —
  caso real: cód 21097090 alertado às 19:19 E às 19:20, com push em deploy
  às 19:1x BRT; o container novo re-detectou porque o velho morreu sem
  gravar o estado. Janela residual ACEITA: kill entre o claim e o envio
  perde 1 alerta (a cobrança segue visível no /admin/vigia-venda-sem-item
  e no acumulado da janela das mensagens seguintes).

ANTI-FLOOD (pedido do dono 18/07/2026 — "cuidado com os disparos no
WhatsApp pra não bloquear a conta"): a PRIMEIRA cobrança nova alerta na
hora; as seguintes ACUMULAM e saem juntas na próxima janela — no máximo 1
mensagem por `VENDA_SEM_ITEM_COOLDOWN_MIN` (default 60min) e
`VENDA_SEM_ITEM_MAX_MSGS_DIA` (default 6) mensagens/dia. Cobrança
acumulada nunca se perde: ids só são marcados quando a mensagem SAI, então
a próxima janela lista tudo que juntou. O envio NÃO usa `critico=True` de
propósito — respeita também o teto/hora global do zapi (mensagem segurada
volta ok=False e retenta, mesmo caminho do envio falho).

Config (env):
- `VENDA_SEM_ITEM_VIGIA=0` desliga o job no cron (kill-switch, padrão).
- `VENDA_SEM_ITEM_MIN_VALOR` (default 0) — piso em R$ por cobrança; abaixo
  dele não alerta (ex: 100 pra ignorar cobrança avulsa pequena de balcão).
- `VENDA_SEM_ITEM_COOLDOWN_MIN` (default 60) — intervalo mínimo entre
  mensagens; 0 desliga o cooldown (volta a 1 msg por ciclo de 15min).
- `VENDA_SEM_ITEM_MAX_MSGS_DIA` (default 6) — teto de mensagens por dia;
  estourado, acumula até o dia virar. ATENÇÃO: 0 = SEM teto (não "zero
  mensagens" — pra silenciar use o kill-switch VENDA_SEM_ITEM_VIGIA=0).

Limitação conhecida (aceita): a janela é [ontem, hoje] por `createdAt` —
pedido criado ANTES de ontem que só hoje perde os itens (esvaziado) não
entra na varredura.

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


def canais_ignorados():
    """Tags de canal que NÃO alertam (delivery — chegam sem itens por
    natureza). Default = SEM_ITENS_CANAIS_DELIVERY; env
    `VENDA_SEM_ITEM_CANAIS_IGNORADOS` (CSV de tags) substitui."""
    from app.constants import SEM_ITENS_CANAIS_DELIVERY
    bruto = (os.environ.get('VENDA_SEM_ITEM_CANAIS_IGNORADOS') or '').strip()
    if not bruto:
        return set(SEM_ITENS_CANAIS_DELIVERY)
    return {t.strip().lower() for t in bruto.split(',') if t.strip()}


# NFC-e/NF-e com nota fiscal EMITIDA (não é venda fantasma "sem nota"):
# 'authorized' = autorizada pela SEFAZ; 'contingency' = emitida OFFLINE quando
# internet/SEFAZ cai (DANFE impressa e entregue ao cliente, transmitida depois)
# — é documento fiscal válido, COM produtos. Contingência entrou em 23/07/2026
# (caso Nebraska cód 19989588: café + cookie, NFC-e nº 2360 em contingência que
# o vigia acusava como "sem NF"). Só falta de nota MESMA (taxInvoice None ou
# status cancelado/negado) é o padrão suspeito.
_NF_STATUS_FISCAL = {'authorized', 'contingency'}


def _nf_autorizada(pedido):
    """True se o pedido tem NFC-e/NF-e fiscalmente EMITIDA (autorizada OU em
    contingência) — venda com nota NUNCA é o padrão "sem item/sem NF"."""
    ti = pedido.get('taxInvoice') if isinstance(pedido, dict) else None
    if not isinstance(ti, dict):
        return False
    return (ti.get('status') or '').strip().lower() in _NF_STATUS_FISCAL


def cobrancas_sem_itens(data_inicial, data_final):
    """Cobranças com valor e ZERO itens na janela, direto da API do Seru.

    Cada uma: {id, codigo, data, hora, company, total, caixa, tem_nf}.
    Canceladas e total <= piso ficam fora. Levanta exceção se a API cair —
    o chamador decide (o vigiar() engole e reporta).

    RE-VERIFICAÇÃO no DETALHE (21/07/2026, caso R$155 O Pao Padaria): a
    LISTAGEM da Seru ATRASA pra cobrança recém-criada — ela aparece sem
    itens e sem NF na lista mesmo JÁ tendo NFC-e autorizada (a R$155 foi
    criada 14:58, NFC-e às 15:06, e às 15:31 a lista ainda a mostrava
    vazia → falso positivo de "venda sem item"). O `GET /orders/{id}`
    (detalhe) é a fonte autoritativa e traz os itens + a taxInvoice de
    verdade. Então: a listagem é só um PRÉ-FILTRO barato; toda suspeita
    é re-conferida no detalhe antes de virar alerta. Detalhe indisponível
    = NÃO alerta nesse ciclo (retenta no próximo; perder um ciclo é melhor
    que falso alarme). Decisão do dono: só reverificar, sem carência —
    alerta segue imediato pras cobranças que o detalhe confirma fantasma."""
    from app.services import seru

    piso = min_valor()
    ignorados = canais_ignorados()
    out = []
    for p in seru.listar_pedidos_completo(data_inicial, data_final):
        try:
            # Cancelado por canceledAt OU status (helper canonico) — caso
            # 18/07: cancelada sem canceledAt alertou como venda.
            if not isinstance(p, dict) or seru.pedido_cancelado(p):
                continue
            # Canal de DELIVERY (99food etc.) chega sem itens por natureza
            # da integracao — venda real, rotina, NAO alerta (dono 18/07).
            if seru.canal_tag(p) in ignorados:
                continue
            total = Decimal(str(p.get('total') or 0))
            if total <= piso or total <= 0:
                continue
            # PRÉ-FILTRO barato pela listagem: item não-cancelado já na
            # lista = venda real, nem paga a chamada do detalhe.
            if any(not it['cancelado'] for it in seru.extrair_itens(p)):
                continue
            dh = seru.datahora_local(p.get('createdAt'))
            if not dh or not (data_inicial <= dh.date() <= data_final):
                continue
            pid = str(p.get('id') or p.get('code') or '')
            det_id = str(p.get('id') or '')
            if not pid:
                # sem id nem code não dá pra deduplicar — loga em vez de
                # sumir em silêncio (achado de revisão; API sempre manda id)
                logger.warning('vigia venda sem item: pedido sem id/code '
                               'ignorado (total R$ %s)', total)
                continue
            # RE-VERIFICA no DETALHE (fonte autoritativa) antes de acusar.
            det = None
            if det_id:
                try:
                    det = seru.detalhes_pedido(det_id)
                except Exception:  # noqa: BLE001 — detalhe fora não pode
                    # virar alerta às cegas nem cegar a varredura toda.
                    logger.exception('vigia venda sem item: detalhe de %s '
                                     'falhou', det_id)
                    det = None
            if not isinstance(det, dict):
                # Sem confirmação: NÃO alerta agora — retenta no próximo
                # ciclo (id não vira alerta nem entra no dedup).
                logger.info('vigia venda sem item: %s sem detalhe confiável '
                            '— adiando pro próximo ciclo', pid)
                continue
            # Venda REAL se o DETALHE tem item não-cancelado OU NFC-e
            # autorizada — o falso positivo do lag morre aqui.
            if any(not it['cancelado'] for it in seru.extrair_itens(det)) \
                    or _nf_autorizada(det):
                continue
            # Confirmado FANTASMA pelo detalhe: monta a linha com os dados
            # do detalhe (mais atuais que a lista).
            nf = det.get('taxInvoice') or {}
            nf_status = ((nf.get('status') or '').lower()
                         if isinstance(nf, dict) else '')
            tem_nf = bool(nf) and nf_status not in (
                'canceled', 'cancelled', 'denied', 'rejected', 'error')
            canal = det.get('salesChannel') or p.get('salesChannel')
            comp = (det.get('company') or p.get('company') or {})
            out.append({
                'id': pid,
                'codigo': det.get('code') or p.get('code'),
                'data': dh.date().isoformat(),
                'hora': dh.strftime('%H:%M'),
                'company': (comp.get('name') if isinstance(comp, dict)
                            else None) or '(sem loja)',
                'total': float(total),
                'caixa': (det.get('cashier') or p.get('cashier')
                          or {}).get('code'),
                'tem_nf': tem_nf,
                'canal': (canal.get('name') if isinstance(canal, dict)
                          else None) or '?',
            })
        except Exception:  # noqa: BLE001 — UM pedido torto não pode cegar
            # a varredura INTEIRA (e repetir a cegueira a cada ciclo
            # enquanto ele estiver na janela) — achado de revisão.
            logger.exception('vigia venda sem item: pedido malformado '
                             'ignorado: %r', str(p)[:200])
    return out


def _int_env(nome, default):
    bruto = (os.environ.get(nome) or '').strip()
    if not bruto:
        return default
    try:
        v = int(bruto)
        if v < 0:
            raise ValueError(bruto)
        return v
    except ValueError:
        logger.warning('%s inválido (%r) — usando %s', nome, bruto, default)
        return default


def estado_dedup(janela_datas):
    """API pública do estado de dedup (rota admin/diagnóstico)."""
    return _carregar_estado(janela_datas)


def _carregar_estado(janela_datas):
    """Estado {'ids': {data: [ids]}, 'ultimo_envio': iso|None,
    'envios': {data: n}}, com ids/envios podados pra janela vigente.
    Aceita o formato antigo ({data: [ids]} direto) por compat."""
    from app.models import AppConfig
    try:
        bruto = json.loads(AppConfig.get(_KEY_ESTADO) or '{}')
    except (ValueError, TypeError):
        bruto = {}
    if bruto and 'ids' not in bruto:
        # formato antigo: o dict inteiro era o mapa data -> [ids]
        bruto = {'ids': bruto}
    validas = {d.isoformat() for d in janela_datas}
    ids = {k: list(v) for k, v in (bruto.get('ids') or {}).items()
           if k in validas and isinstance(v, list)}
    envios = {k: int(v) for k, v in (bruto.get('envios') or {}).items()
              if k in validas and isinstance(v, int)}
    return {'ids': ids, 'ultimo_envio': bruto.get('ultimo_envio'),
            'envios': envios}


def _gravar_estado(estado):
    from app.extensions import db
    from app.models import AppConfig
    AppConfig.set(_KEY_ESTADO, json.dumps(estado))
    db.session.commit()


def _pode_enviar(estado, agora_dt, hoje_iso):
    """Anti-flood: cooldown entre mensagens + teto de mensagens/dia."""
    cap = _int_env('VENDA_SEM_ITEM_MAX_MSGS_DIA', 6)
    if cap and estado['envios'].get(hoje_iso, 0) >= cap:
        return False, f'teto de {cap} msgs/dia atingido — acumulando'
    cooldown = _int_env('VENDA_SEM_ITEM_COOLDOWN_MIN', 60)
    ultimo = estado.get('ultimo_envio')
    if cooldown and ultimo:
        try:
            from datetime import datetime as _dt
            delta = (agora_dt - _dt.fromisoformat(ultimo)).total_seconds()
            if delta < cooldown * 60:
                falta = int((cooldown * 60 - delta) // 60) + 1
                return False, f'cooldown ({falta}min restantes) — acumulando'
        except (ValueError, TypeError):
            pass
    return True, None


def _montar_mensagem(novas, todas):
    """Uma mensagem por ciclo, agrupada por company. `todas` dá o contexto
    da JANELA ontem+hoje (acumulado do padrão, não só as novas)."""
    from app.utils import fmt_brl
    por_comp = {}
    for c in novas:
        por_comp.setdefault(c['company'], []).append(c)
    blocos = []
    for comp, lst in sorted(por_comp.items()):
        lst.sort(key=lambda x: (x['data'], x['hora']), reverse=True)
        soma = sum(c['total'] for c in lst)
        # Acumulado na JANELA inteira (não só o dia mais recente — as novas
        # podem abranger ontem E hoje no 1º run/retentativa).
        jan_lst = [c for c in todas if c['company'] == comp]
        jan_soma = sum(c['total'] for c in jan_lst)
        linhas = []
        for c in lst[:_MAX_LINHAS_MSG]:
            nf = 'com NF' if c['tem_nf'] else 'SEM NF'
            linhas.append(f'• {c["data"][8:10]}/{c["data"][5:7]} {c["hora"]} '
                          f'— {fmt_brl(c["total"])} — {nf} — '
                          f'{c.get("canal") or "?"} — caixa '
                          f'{c["caixa"] or "?"} (cód {c["codigo"] or "?"})')
        if len(lst) > _MAX_LINHAS_MSG:
            linhas.append(f'• …e mais {len(lst) - _MAX_LINHAS_MSG}')
        blocos.append(
            f'*{comp}*: {len(lst)} nova(s), {fmt_brl(soma)}\n'
            + '\n'.join(linhas)
            + f'\nAcumulado ontem+hoje nesse padrão: {fmt_brl(jan_soma)} '
              f'({len(jan_lst)} cobrança(s)).')
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

    try:
        estado = _carregar_estado(janela)
    except Exception as e:  # noqa: BLE001 — sessão envenenada por falha
        # anterior do cron (PendingRollback) deixava o vigia CEGO em
        # silêncio a cada ciclo — mesmo hardening do uso_ia_vigia.
        logger.exception('vigia venda sem item: leitura do estado explodiu')
        try:
            from app.extensions import db
            db.session.rollback()
        except Exception:  # noqa: BLE001
            pass
        return {'rodou': True, 'erro': f'estado: {type(e).__name__}'}
    ja = {i for ids in estado['ids'].values() for i in ids}
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

    from app.utils import agora as _agora
    agora_dt = _agora()
    hoje_iso = hoje_d.isoformat()
    pode, motivo = _pode_enviar(estado, agora_dt, hoje_iso)
    if not pode:
        # Anti-flood: NÃO marca os ids — as novas acumulam e saem juntas
        # na próxima janela (nada se perde).
        return {'rodou': True, 'novas': len(novas), 'enviado': False,
                'motivo': motivo}

    msg = _montar_mensagem(novas, todas)
    try:
        # SEM critico=True de propósito: respeita o teto/hora global do
        # zapi (anti-flood do WhatsApp). Mensagem segurada volta ok=False
        # e cai no retenta abaixo.
        r = zapi.enviar_texto(dono, msg)
        ok = bool(r.get('ok')) if isinstance(r, dict) else False
    except Exception:  # noqa: BLE001
        logger.exception('vigia venda sem item: envio WhatsApp explodiu')
        ok = False
    if not ok:
        # Não marca: retenta no próximo ciclo (perder alerta é pior).
        return {'rodou': True, 'novas': len(novas), 'enviado': False,
                'motivo': 'envio falhou — retenta no proximo ciclo'}
    for c in novas:
        estado['ids'].setdefault(c['data'], []).append(c['id'])
    estado['ultimo_envio'] = agora_dt.isoformat()
    estado['envios'][hoje_iso] = estado['envios'].get(hoje_iso, 0) + 1
    _gravar_estado(estado)
    return {'rodou': True, 'novas': len(novas), 'enviado': True,
            'valor_novas': round(sum(c['total'] for c in novas), 2)}
