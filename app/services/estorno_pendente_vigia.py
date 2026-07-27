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
  não marca (retenta no próximo ciclo) — perder um alerta de estoque é pior
  que repetir.
- Anti-flood igual ao `venda_sem_item_vigia`: cooldown entre mensagens e
  teto diário, ambos por env.

Config (env):
- `ESTORNO_PENDENTE_VIGIA=0` desliga (kill-switch).
- `ESTORNO_PENDENTE_COOLDOWN_MIN` (default 60) — intervalo mínimo entre
  mensagens; 0 desliga o cooldown.
- `ESTORNO_PENDENTE_MAX_MSGS_DIA` (default 4) — teto/dia. 0 = SEM teto
  (para silenciar use o kill-switch).

Sob demanda: `GET /admin/vigia-estorno-pendente` (owner; dry-run lista o
estado sem WhatsApp, `?alertar=1` roda o fluxo).
"""
import json
import logging
import os
from datetime import timedelta

logger = logging.getLogger(__name__)

_KEY_ESTADO = 'estorno_pendente_alertados'
_MAX_LINHAS_MSG = 8
_JANELA_DIAS = 7          # poda do estado (a janela do sync é bem menor)


def _cfg_int(nome, padrao):
    try:
        return int(os.environ.get(nome, str(padrao)))
    except (TypeError, ValueError):
        return padrao


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
    corte = (_hoje() - timedelta(days=_JANELA_DIAS)).isoformat()
    ids = {d: list(v) for d, v in (est.get('ids') or {}).items()
           if isinstance(v, list) and d >= corte}
    envios = {d: int(n) for d, n in (est.get('envios') or {}).items()
              if d >= corte}
    return {'ids': ids, 'ultimo_envio': est.get('ultimo_envio'),
            'envios': envios}


def _gravar_estado(estado):
    from app.extensions import db
    from app.models import AppConfig
    AppConfig.set(_KEY_ESTADO, json.dumps(estado, ensure_ascii=False))
    db.session.commit()


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


def itens_baixados(pedido_id):
    """O que este pedido tirou do estoque, pra o dono saber o que devolver:
    [(nome_da_linha, quantidade)]. Lê os `MovEstoqueLoja` pela MESMA
    referência que a baixa gravou ('Seru #<id>', com o sufixo de cesta/
    fração quando houver). Read-only; erro devolve []."""
    try:
        from app.extensions import db
        from app.models import EstoqueLoja, MovEstoqueLoja
        ref = f'Seru #{pedido_id}'
        movs = (MovEstoqueLoja.query
                .filter(MovEstoqueLoja.tipo == 'venda_seru',
                        db.or_(MovEstoqueLoja.referencia == ref,
                               MovEstoqueLoja.referencia.like(ref + ' %')))
                .all())
        out = []
        for m in movs:
            el = EstoqueLoja.query.get(m.estoque_loja_id)
            nome = '?'
            if el is not None:
                alvo = el.receita or el.produto or el.materia_prima
                nome = getattr(alvo, 'nome', None) or el.nome_pendente or '?'
            out.append((nome, int(m.quantidade or 0)))
        return out
    except Exception:  # noqa: BLE001 — detalhe é bônus, nunca derruba o alerta
        logger.exception('vigia estorno pendente: detalhe dos movs falhou')
        return []


def _montar_mensagem(novos):
    from app.utils import fmt_brl
    linhas = []
    for c in novos[:_MAX_LINHAS_MSG]:
        det = itens_baixados(c['id'])
        oque = ', '.join(f'{q}x {n}' for n, q in det) if det else \
            f"{c.get('itens_baixados', 0)} item(ns)"
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

    try:
        r = zapi.enviar_texto(dono, _montar_mensagem(novos))
        ok = bool(r.get('ok')) if isinstance(r, dict) else False
    except Exception:  # noqa: BLE001
        logger.exception('vigia estorno pendente: envio WhatsApp explodiu')
        ok = False
    if not ok:
        return {'rodou': True, 'novos': len(novos), 'enviado': False,
                'motivo': 'envio falhou — retenta no proximo ciclo'}

    for c in novos:
        estado['ids'].setdefault(c.get('data') or hoje_iso, []).append(c['id'])
    estado['ultimo_envio'] = agora_dt.isoformat()
    estado['envios'][hoje_iso] = estado['envios'].get(hoje_iso, 0) + 1
    _gravar_estado(estado)
    return {'rodou': True, 'novos': len(novos), 'enviado': True}
