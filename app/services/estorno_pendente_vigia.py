"""Vigia de ESTORNO QUE NUNCA VAI DISPARAR (26/07/2026).

Caso real: 4 cobranças canceladas entre 22 e 24/07/2026 tinham baixado
estoque (7 itens no total) e NUNCA devolveram. Só apareceram numa auditoria
manual — nada no sistema avisou.

POR QUE ACONTECE: o estorno de um pedido JÁ PROCESSADO é disparado por
`canceledAt` (`seru_sync.processar_pedidos`). Essas cobranças foram
canceladas só pelo `status`, com `canceledAt` VAZIO — então a condição
nunca fecha e o estoque fica baixado para sempre. Mudar esse gatilho mexe na
baixa de estoque e é decisão separada, documentada no CLAUDE.md.

DECISÃO DO DONO (26/07/2026): "deixa pra lá [os 4 antigos], vejo os
próximos" e, quando avisado de que os próximos passariam batidos do mesmo
jeito, escolheu **alertar** em vez de mexer no gatilho. Então este vigia
NÃO toca em estoque: ele só torna visível o que hoje é silencioso.

Funcionamento:
- A detecção mora no próprio `processar_pedidos` (que já tem os pedidos da
  API e o registro de processados na mão) e sai em `stats['estornos_
  pendentes']`. Nenhuma consulta extra à Seru.
- O cron chama `alertar(...)` a cada ciclo, DENTRO do advisory lock do sync
  — execução única entre workers, sem alerta duplicado.
- Dedup POR PEDIDO em AppConfig: cada cobrança avisa UMA vez. Envio falho
  DEVOLVE o claim (retenta no próximo ciclo) — perder um alerta de estoque
  é pior que repetir. Os ids são marcados e COMMITADOS **antes** do envio
  (claim-first, 19/08/2026 — mesma classe do `venda_sem_item_vigia`: kill
  de deploy entre o envio e o commit duplicava o alerta no ciclo do
  container novo).
- Anti-flood igual ao `venda_sem_item_vigia`: cooldown entre mensagens e
  teto diário, ambos por env.

LIMITAÇÃO CONHECIDA (aceita): a janela é a do sync — `hoje -
SYNC_CATCHUP_DIAS` (default 2), filtrada por **`createdAt`**. Cobrança
criada dia 20 e cancelada só no dia 25 já saiu da janela e NÃO é detectada.
Cobre o caso real (cancelamento no mesmo dia ou no dia seguinte); ampliar
custa varredura extra na API e é decisão separada.

Config (env):
- `ESTORNO_PENDENTE_VIGIA=0` desliga (kill-switch).
- `ESTORNO_PENDENTE_COOLDOWN_MIN` (default 60) — intervalo mínimo entre
  mensagens; 0 desliga o cooldown.
- `ESTORNO_PENDENTE_MAX_MSGS_DIA` (default 4) — teto/dia. 0 = SEM teto
  (para silenciar use o kill-switch).

Sob demanda: `GET /admin/vigia-estorno-pendente` (owner; dry-run lista o
estado sem WhatsApp, `?alertar=1` roda o fluxo).
"""
import copy
import json
import logging
import os
from datetime import timedelta

logger = logging.getLogger(__name__)

_KEY_ESTADO = 'estorno_pendente_alertados'
_MAX_LINHAS_MSG = 8
_JANELA_DIAS = 7          # poda do estado (a janela do sync é bem menor)


def _cfg_int(nome, padrao):
    """Int da env, com piso ZERO. Valor negativo CALARIA o vigia pra sempre
    em silencio (`0 >= -1` no teto) — aqui o silencio esconde estoque baixado
    indevidamente, entao negativo vira o default com WARNING. Pra desligar de
    verdade existe o kill-switch."""
    try:
        v = int(os.environ.get(nome, str(padrao)))
    except (TypeError, ValueError):
        return padrao
    if v < 0:
        logger.warning('vigia estorno pendente: %s=%s invalido (negativo) '
                       '— usando %s', nome, v, padrao)
        return padrao
    return v


def _carregar_estado():
    """{'ids': {data: [ids]}, 'ultimo_envio': iso, 'envios': {data: n}},
    já podado. Formato inválido = começa limpo (nunca levanta)."""
    from app.models import AppConfig
    from app.utils import hoje as _hoje
    vazio = {'ids': {}, 'ultimo_envio': None, 'envios': {}}
    raw = AppConfig.get(_KEY_ESTADO)
    if not raw:
        return vazio
    try:
        est = json.loads(raw)
        if not isinstance(est, dict):
            raise ValueError('esperava dict')
    except (ValueError, TypeError):
        logger.warning('vigia estorno pendente: estado inválido — recomeça')
        return vazio
    # A poda NUNCA pode ser mais curta que a janela que o sync varre: id
    # podado cedo demais volta a alertar sozinho a cada ciclo.
    dias = max(_JANELA_DIAS, _cfg_int('SYNC_CATCHUP_DIAS', 2) + 1)
    corte = (_hoje() - timedelta(days=dias)).isoformat()
    # `isinstance` em TUDO: um estado torto nao pode cegar o vigia pra
    # sempre (ele nao se autocorrige — ficaria mudo ate alguem apagar a
    # chave na mao). O que nao entende, descarta.
    bruto_ids = est.get('ids')
    ids, envios = {}, {}
    if isinstance(bruto_ids, dict):
        ids = {d: [str(i) for i in v] for d, v in bruto_ids.items()
               if isinstance(d, str) and isinstance(v, list) and d >= corte}
    bruto_envios = est.get('envios')
    if isinstance(bruto_envios, dict):
        envios = {d: n for d, n in bruto_envios.items()
                  if isinstance(d, str) and isinstance(n, int) and d >= corte}
    ultimo = est.get('ultimo_envio')
    return {'ids': ids,
            'ultimo_envio': ultimo if isinstance(ultimo, str) else None,
            'envios': envios}


def _gravar_estado(estado):
    """Persiste o estado. NUNCA levanta (contrato do `alertar`) — devolve
    False se falhou. Com o claim-first (19/08/2026), um False ANTES do envio
    pula o ciclo (retenta depois — sem claim durável não se envia); um False
    ao DEVOLVER o claim de envio falho deixa os ids marcados sem envio
    (alerta se perde — janela mínima, aceita)."""
    from app.extensions import db
    from app.models import AppConfig
    try:
        AppConfig.set(_KEY_ESTADO, json.dumps(estado, ensure_ascii=False))
        db.session.commit()
        return True
    except Exception:  # noqa: BLE001
        logger.exception('vigia estorno pendente: gravar estado falhou')
        try:
            db.session.rollback()
        except Exception:  # noqa: BLE001
            pass
        return False


def _pode_enviar(estado, agora_dt, hoje_iso):
    """(bool, motivo) — cooldown + teto diário. Espelha o anti-flood do
    `venda_sem_item_vigia`: o que não sai agora ACUMULA (os ids só são
    marcados quando a mensagem sai), nada se perde."""
    teto = _cfg_int('ESTORNO_PENDENTE_MAX_MSGS_DIA', 4)
    if teto and estado['envios'].get(hoje_iso, 0) >= teto:
        return False, f'teto de {teto} mensagens/dia atingido'
    cooldown = _cfg_int('ESTORNO_PENDENTE_COOLDOWN_MIN', 60)
    ultimo = estado.get('ultimo_envio')
    if cooldown and ultimo:
        try:
            from datetime import datetime
            passou = (agora_dt - datetime.fromisoformat(ultimo))
            if passou < timedelta(minutes=cooldown):
                return False, f'cooldown de {cooldown}min'
        except (ValueError, TypeError):
            pass                      # estado corrompido não trava o alerta
    return True, ''


def e_estorno_pendente(pedido, reg):
    """REGRA CANÔNICA: este pedido baixou estoque, aparece cancelado e o
    estorno automático NUNCA vai disparar?

    Condições, todas necessárias:
    - já processado e ainda NÃO estornado (`reg`);
    - baixou alguma coisa (`n_itens_baixados > 0`) — pedido que não baixou
      nada não tem o que devolver;
    - está cancelado pela regra canônica (`seru.pedido_cancelado`: status
      OU canceledAt) MAS **sem** `canceledAt` — é justamente o `canceledAt`
      que o `seru_sync` usa como gatilho do estorno.

    Usada nos DOIS caminhos (o sync e a tela sob demanda) pra eles nunca
    divergirem — se a regra mudar, muda num lugar só."""
    from app.services import seru
    if reg is None or reg.estornado_em:
        return False
    if int(getattr(reg, 'n_itens_baixados', 0) or 0) <= 0:
        return False
    if pedido.get('canceledAt'):
        return False              # o gatilho normal cobre este
    return bool(seru.pedido_cancelado(pedido))


def detectar(data_inicial, data_final):
    """Lista READ-ONLY dos estornos pendentes na janela — bate na API e no
    registro de processados, sem tocar em estoque. É o que a tela sob
    demanda usa; o cron aproveita a detecção que o próprio sync já faz."""
    from app.models import SeruPedidoProcessado
    from app.services import seru
    pedidos = seru.listar_pedidos_completo(data_inicial, data_final)
    out = []
    for p in pedidos or []:
        if not isinstance(p, dict):
            continue
        pid = str(p.get('id') or p.get('orderNumber')
                  or p.get('code') or '').strip()
        if not pid:
            continue
        d = seru.data_local(p.get('createdAt'))
        if not d or not (data_inicial <= d <= data_final):
            continue
        reg = SeruPedidoProcessado.query.get(pid)
        if not e_estorno_pendente(p, reg):
            continue
        try:
            total = float(p.get('total') or 0)
        except (TypeError, ValueError):
            total = 0.0
        out.append({
            'id': pid, 'data': d.isoformat(),
            'loja': seru.nome_company(p),   # company vem dict OU string
            'total': total,
            'itens_baixados': int(reg.n_itens_baixados or 0),
        })
    return out


def estado_dedup():
    """Estado de dedup/anti-flood, ja podado — API publica pra tela sob
    demanda (o irmao `venda_sem_item_vigia` expoe o mesmo)."""
    return _carregar_estado()


def itens_baixados(pedido_id):
    """O que este pedido tirou do estoque, pra o dono saber o que devolver.

    Devolve `([(nome, qtd)], n_fracionarias)`. Lê os `MovEstoqueLoja` pela
    MESMA referência que a baixa gravou ('Seru #<id>', com o sufixo de
    cesta/fração quando houver).

    FRAÇÕES FICAM DE FORA da lista de propósito: uma baixa marcada
    '(fracao)'/'(fator' é a unidade inteira que FECHOU no acumulador, e ela
    pode ter contribuição de VÁRIAS vendas — por isso o próprio estorno
    (`baixa_venda`, fase 1) as pula. Mandar o dono devolver na mão "1x
    Cookie" que era de 5 cafés criaria estoque fantasma. Elas só são
    CONTADAS, pra a mensagem não fingir que não existem.

    Read-only; erro devolve ([], 0) — o detalhe é bônus, o alerta é o que
    importa."""
    try:
        from app.extensions import db
        from app.models import EstoqueLoja, MovEstoqueLoja
        ref = f'Seru #{pedido_id}'
        movs = (MovEstoqueLoja.query
                .filter(MovEstoqueLoja.tipo == 'venda_seru',
                        db.or_(MovEstoqueLoja.referencia == ref,
                               MovEstoqueLoja.referencia.like(ref + ' %')))
                .all())
        out, fracionarias = [], 0
        for m in movs:
            ref_m = m.referencia or ''
            if '(fracao)' in ref_m or '(fator' in ref_m:
                fracionarias += 1
                continue
            el = EstoqueLoja.query.get(m.estoque_loja_id)
            nome = '?'
            if el is not None:
                alvo = el.receita or el.produto or el.materia_prima
                nome = getattr(alvo, 'nome', None) or el.nome_pendente or '?'
            out.append((nome, int(m.quantidade or 0)))
        return out, fracionarias
    except Exception:  # noqa: BLE001 — detalhe é bônus, nunca derruba o alerta
        logger.exception('vigia estorno pendente: detalhe dos movs falhou')
        return [], 0


def _montar_mensagem(novos):
    from app.utils import fmt_brl
    linhas = []
    for c in novos[:_MAX_LINHAS_MSG]:
        det, fracs = itens_baixados(c['id'])
        oque = ', '.join(f'{q}x {n}' for n, q in det) if det else \
            f"{c.get('itens_baixados', 0)} item(ns)"
        if fracs:
            # Ver `itens_baixados`: fracao nao se devolve na mao.
            oque += (f' (+{fracs} baixa(s) fracionaria(s) — NAO devolver na '
                     'mao, sao de varias vendas)')
        linhas.append(
            f"• {c.get('loja') or 'loja ?'} — {fmt_brl(c.get('total') or 0)}"
            f"\n  cobrança {c['id']} ({c.get('data', '?')})"
            f"\n  saiu do estoque: {oque}")
    corpo = '\n'.join(linhas)
    if len(novos) > _MAX_LINHAS_MSG:
        corpo += f'\n• … e mais {len(novos) - _MAX_LINHAS_MSG}'
    return (
        f'⚠️ *{len(novos)} venda(s) cancelada(s) que NÃO devolveram o '
        f'estoque*\n\n{corpo}\n\n'
        'Foram canceladas no PDV sem a data de cancelamento, então o '
        'estorno automático não dispara. O estoque segue baixado até '
        'alguém ajustar na mão (Estoque da loja → ajuste).')


def alertar(pendentes):
    """Avisa o dono sobre os estornos que nunca vão disparar sozinhos.

    `pendentes`: a lista de `stats['estornos_pendentes']` do
    `processar_pedidos`. NÃO mexe em estoque — só avisa (decisão do dono
    26/07/2026). Nunca levanta: o cron segue vivo (contrato dos vigias)."""
    from flask import current_app

    from app.services import zapi
    from app.utils import agora as _agora
    from app.utils import hoje as _hoje

    if os.environ.get('ESTORNO_PENDENTE_VIGIA', '1') == '0':
        return {'rodou': False, 'motivo': 'kill-switch'}
    pendentes = [c for c in (pendentes or []) if c.get('id')]
    if not pendentes:
        return {'rodou': True, 'novos': 0}

    try:
        estado = _carregar_estado()
    except Exception as e:  # noqa: BLE001 — sessão envenenada não cega o vigia
        logger.exception('vigia estorno pendente: leitura do estado explodiu')
        try:
            from app.extensions import db
            db.session.rollback()
        except Exception:  # noqa: BLE001
            pass
        return {'rodou': True, 'erro': f'estado: {type(e).__name__}'}

    ja = {i for ids in estado['ids'].values() for i in ids}
    novos = [c for c in pendentes if c['id'] not in ja]
    if not novos:
        _gravar_estado(estado)          # persiste a poda
        return {'rodou': True, 'novos': 0, 'total_janela': len(pendentes)}

    cfg = current_app.config
    dono = ((cfg.get('CHATWOOT_VIGIA_INFRA_NUMERO') or '').strip()
            or (cfg.get('ZAPI_BOT_DONO_NUMERO') or '').strip())
    if not dono:
        # Sem destino NÃO marca: quando configurar, alerta tudo.
        logger.warning('vigia estorno pendente: %d pendente(s) e NENHUM '
                       'número de dono configurado', len(novos))
        return {'rodou': True, 'novos': len(novos), 'enviado': False,
                'motivo': 'sem numero do dono'}

    agora_dt = _agora()
    hoje_iso = _hoje().isoformat()
    pode, motivo = _pode_enviar(estado, agora_dt, hoje_iso)
    if not pode:
        return {'rodou': True, 'novos': len(novos), 'enviado': False,
                'motivo': motivo}

    # CLAIM-FIRST (19/08/2026, mesma classe do venda_sem_item_vigia): marca
    # e COMMITA os ids ANTES do envio — kill de deploy entre o envio e o
    # commit duplicava o alerta no ciclo do container novo. Envio falho
    # devolve o claim (retenta). Claim que não grava = não envia.
    anterior = copy.deepcopy(estado)
    for c in novos:
        estado['ids'].setdefault(c.get('data') or hoje_iso, []).append(c['id'])
    estado['ultimo_envio'] = agora_dt.isoformat()
    estado['envios'][hoje_iso] = estado['envios'].get(hoje_iso, 0) + 1
    if not _gravar_estado(estado):
        return {'rodou': True, 'novos': len(novos), 'enviado': False,
                'motivo': 'claim falhou — retenta no proximo ciclo'}

    try:
        r = zapi.enviar_texto(dono, _montar_mensagem(novos))
        ok = bool(r.get('ok')) if isinstance(r, dict) else False
    except Exception:  # noqa: BLE001
        logger.exception('vigia estorno pendente: envio WhatsApp explodiu')
        ok = False
    if not ok:
        # Devolve o claim; se a devolução falhar, o claim fica "gasto"
        # (ids marcados sem envio — janela mínima, aceita).
        _gravar_estado(anterior)
        return {'rodou': True, 'novos': len(novos), 'enviado': False,
                'motivo': 'envio falhou — retenta no proximo ciclo'}
    return {'rodou': True, 'novos': len(novos), 'enviado': True}
