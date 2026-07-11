"""Acuracia do forecast do pedido semanal (28/06/2026; Fase 0 em 02/07/2026).

Mede se a previsao acerta — antes nao havia NADA medindo, entao calibrar a
recencia (meia-vida) ou qualquer melhoria era no escuro.

Fase 0 (02/07/2026) — a v1 media o motor ERRADO, de forma circular:
- congelava `sugerir_pedidos_semana`, APOSENTADO da UI em 01/07 — a acuracia
  calibrava um motor que nao gera pedido nenhum;
- agora congela os DOIS motores vivos (media_pedido = tela da media;
  venda_estoque = tela por venda+estoque), rotulados na coluna `motor`, com
  `lead_dias` (antecedencia) — da pra comparar motor e segmentar por lead;
- registra tambem previsto=0 dos itens exibidos (fecha o falso-negativo
  documentado da v1: previu 0 / loja pediu nao entrava);
- o casamento RE-casa por 48h (pedido marcado entregue depois do cron nao
  fica congelado como realizado=0 pra sempre) e virou UMA query agregada
  (era 1 query por snapshot);
- o resumo segmenta por motor/loja/lead e expoe a CIRCULARIDADE: % dos
  pedidos entregues que nasceram da propria sugestao (rascunho auto-gerado)
  — quando alta, o "acerto" mede menos a demanda e mais o habito de aprovar
  sem editar.

Fluxo: `registrar_snapshot()` + `casar_realizados()` no cron diario 05:30;
`resumo_acuracia()` alimenta /producao/previsao-acuracia.
"""
import logging
from datetime import date, timedelta

from sqlalchemy import func

from app.constants import STATUS_PEDIDO_FINALIZADOS
from app.extensions import db
from app.models import Loja, PedidoItem, PedidoLoja, PrevisaoSnapshot, Receita
from app.utils import agora, hoje

logger = logging.getLogger(__name__)

# Entregue de verdade = finalizados MENOS cancelado (estes nao foram demanda).
_STATUS_ENTREGUE = tuple(s for s in STATUS_PEDIDO_FINALIZADOS if s != 'cancelado')

# Motores vivos medidos (rotulo da coluna `motor`). 'pedido_semana' = legado.
MOTORES_VIVOS = ('media_pedido', 'venda_estoque')

MOTOR_LABEL = {
    'media_pedido': 'Média do pedido',
    'venda_estoque': 'Venda + estoque',
    'pedido_semana': 'Motor antigo (aposentado)',
}


def _registrar_motor(motor, sug, hoje_d):
    """Congela a sugestao de UM motor. Dias travados (ja pedidos) nao sao
    previsao e ficam de fora; MPs ficam de fora (snapshot e por receita);
    previsto=0 de item exibido ENTRA (falso-negativo passa a ser medido).

    Uma linha por (data_alvo, loja, receita, LEAD) — 11/07/2026, aprovado
    pelo dono: o cron diario re-congela a MESMA data a cada antecedencia
    (D-6, D-5, ... D-0), entao a tabela "por lead" compara antecedencias
    da mesma data em vez de leads soltos de datas diferentes. Idempotente
    DENTRO do dia (mesmo lead nao regrava)."""
    datas = [date.fromisoformat(d['data']) for d in sug['dias']]
    if not datas:
        return 0
    existentes = set(
        db.session.query(PrevisaoSnapshot.data_alvo, PrevisaoSnapshot.loja_id,
                         PrevisaoSnapshot.receita_id,
                         PrevisaoSnapshot.lead_dias)
        .filter(PrevisaoSnapshot.data_alvo.in_(datas),
                PrevisaoSnapshot.motor == motor).all())
    novos = 0
    for loja in sug['lojas']:
        ja_tem = set(loja.get('ja_tem') or [])
        for p in loja['produtos']:
            rid = p.get('receita_id')
            if not rid:
                continue                      # MP: fora (snapshot por receita)
            for i, d in enumerate(datas):
                if d.isoformat() in ja_tem:
                    continue                  # dia travado: nao e previsao
                lead = max(0, (d - hoje_d).days)
                chave = (d, loja['loja_id'], rid, lead)
                if chave in existentes:
                    continue
                db.session.add(PrevisaoSnapshot(
                    data_alvo=d, loja_id=loja['loja_id'], receita_id=rid,
                    previsto=int(p['por_dia'][i]), motor=motor,
                    lead_dias=lead))
                existentes.add(chave)
                novos += 1
    return novos


def registrar_snapshot(horizonte_dias=7, janela_semanas=6):
    """Congela a previsao atual dos MOTORES VIVOS por (data_alvo, loja,
    receita, motor). Idempotente: nao sobrescreve chave ja snapshotada.
    Retorna o numero de snapshots novos criados."""
    from app.services.previsao_producao import (
        media_semanal_pedidos,
        sugerir_pedidos_por_venda,
    )
    hoje_d = hoje()
    novos = 0
    novos += _registrar_motor(
        'media_pedido',
        media_semanal_pedidos(horizonte_dias=horizonte_dias,
                              janela_semanas=janela_semanas), hoje_d)
    novos += _registrar_motor(
        'venda_estoque',
        sugerir_pedidos_por_venda(horizonte_dias=horizonte_dias,
                                  janela_semanas=janela_semanas), hoje_d)
    if novos:
        db.session.commit()
    return novos


def casar_realizados(recasar_horas=48):
    """Preenche `realizado` dos snapshots cuja data_alvo ja passou. Alem dos
    nunca-casados, RE-casa os casados nas ultimas `recasar_horas` — pedido
    marcado 'entregue' DEPOIS do cron das 05:30 nao fica congelado como
    realizado=0 pra sempre (o carimbo casado_em so move quando o valor muda,
    senao a janela deslizaria indefinidamente). Retorna quantos snapshots
    foram (re)casados nesta rodada."""
    hoje_d = hoje()
    agora_dt = agora()
    corte_recasa = agora_dt - timedelta(hours=int(recasar_horas or 48))
    alvo = (PrevisaoSnapshot.query
            .filter(PrevisaoSnapshot.data_alvo < hoje_d,
                    db.or_(PrevisaoSnapshot.realizado.is_(None),
                           PrevisaoSnapshot.casado_em >= corte_recasa))
            .all())
    if not alvo:
        return 0
    datas = {s.data_alvo for s in alvo}
    # UMA query agregada (era 1 por snapshot): entregue real por
    # (loja, receita, data). coalesce(recebida, pedida) — a conferencia da
    # entrega vale mais que o digitado.
    rows = (db.session.query(
                PedidoLoja.loja_id, PedidoItem.receita_id,
                PedidoLoja.data_entrega,
                func.sum(func.coalesce(PedidoItem.quantidade_recebida,
                                       PedidoItem.quantidade)))
            .join(PedidoLoja, PedidoItem.pedido_id == PedidoLoja.id)
            .filter(PedidoLoja.data_entrega.in_(list(datas)),
                    PedidoItem.receita_id.isnot(None),
                    PedidoLoja.status.in_(_STATUS_ENTREGUE))
            .group_by(PedidoLoja.loja_id, PedidoItem.receita_id,
                      PedidoLoja.data_entrega)
            .all())
    real = {(lid, rid, d): int(q or 0) for lid, rid, d, q in rows}
    n = 0
    for snap in alvo:
        total = real.get((snap.loja_id, snap.receita_id, snap.data_alvo), 0)
        if snap.realizado is None or int(snap.realizado) != total:
            snap.realizado = total
            snap.casado_em = agora_dt
            n += 1
    if n:
        db.session.commit()
    return n


def _agrega(rows, chave_nome):
    """Transforma linhas (chave, prev, real, abserr, n) em lista de dicts com
    vies/WAPE, ordenada por |vies| desc."""
    def _pct(num, den):
        return round(100 * num / den, 1) if den else None

    out = []
    for chave, prev, real, abserr, n in rows:
        prev, real, abserr = int(prev or 0), int(real or 0), int(abserr or 0)
        out.append({
            chave_nome: chave,
            'previsto': prev, 'realizado': real, 'vies': prev - real,
            'vies_pct': _pct(prev - real, real),
            'wape_pct': _pct(abserr, real), 'n': n,
        })
    out.sort(key=lambda x: -abs(x['vies']))
    return out


def acuracia_por_loja_receita(motor, dias=60, min_n=5):
    """Acuracia por (loja, receita) de UM motor — pro badge na propria grade
    de pedidos (o operador ve o historico de acerto do item onde decide).
    Retorna {(loja_id, receita_id): {vies_pct, wape_pct, n}}; pares com menos
    de `min_n` snapshots casados ficam fora (amostra rasa so faria ruido)."""
    corte = hoje() - timedelta(days=int(dias or 60))
    rows = (db.session.query(
                PrevisaoSnapshot.loja_id, PrevisaoSnapshot.receita_id,
                func.sum(PrevisaoSnapshot.previsto),
                func.sum(PrevisaoSnapshot.realizado),
                func.sum(func.abs(PrevisaoSnapshot.previsto
                                  - PrevisaoSnapshot.realizado)),
                func.count(PrevisaoSnapshot.id))
            .filter(PrevisaoSnapshot.realizado.isnot(None),
                    PrevisaoSnapshot.data_alvo >= corte,
                    PrevisaoSnapshot.motor == motor)
            .group_by(PrevisaoSnapshot.loja_id, PrevisaoSnapshot.receita_id)
            .all())

    def _pct(num, den):
        return round(100 * num / den, 1) if den else None

    out = {}
    for lid, rid, prev, real, abserr, n in rows:
        if n < int(min_n or 0):
            continue
        prev, real, abserr = int(prev or 0), int(real or 0), int(abserr or 0)
        out[(lid, rid)] = {
            'previsto': prev, 'realizado': real,
            'vies_pct': _pct(prev - real, real),
            'wape_pct': _pct(abserr, real), 'n': n,
        }
    return out


def comparativo_motores_por_loja(dias=30):
    """Os DOIS motores lado a lado por loja (vies/WAPE/n) + qual tem menor
    WAPE — responde "qual motor uso nesta loja?" sem alternar filtro. So
    compara quando os dois tem dado; WAPE None (realizado 0) nao vence."""
    corte = hoje() - timedelta(days=int(dias or 30))
    rows = (db.session.query(
                PrevisaoSnapshot.loja_id, PrevisaoSnapshot.motor,
                func.sum(PrevisaoSnapshot.previsto),
                func.sum(PrevisaoSnapshot.realizado),
                func.sum(func.abs(PrevisaoSnapshot.previsto
                                  - PrevisaoSnapshot.realizado)),
                func.count(PrevisaoSnapshot.id))
            .filter(PrevisaoSnapshot.realizado.isnot(None),
                    PrevisaoSnapshot.data_alvo >= corte,
                    PrevisaoSnapshot.motor.in_(MOTORES_VIVOS))
            .group_by(PrevisaoSnapshot.loja_id, PrevisaoSnapshot.motor)
            .all())

    def _pct(num, den):
        return round(100 * num / den, 1) if den else None

    por_loja = {}
    for lid, motor, prev, real, abserr, n in rows:
        prev, real, abserr = int(prev or 0), int(real or 0), int(abserr or 0)
        por_loja.setdefault(lid, {})[motor] = {
            'previsto': prev, 'realizado': real, 'vies': prev - real,
            'vies_pct': _pct(prev - real, real),
            'wape_pct': _pct(abserr, real), 'n': n,
        }
    nomes_loja = dict(db.session.query(Loja.id, Loja.nome).all())
    out = []
    for lid, motores in sorted(por_loja.items(),
                               key=lambda kv: nomes_loja.get(kv[0], '')):
        wapes = {m: v['wape_pct'] for m, v in motores.items()
                 if v.get('wape_pct') is not None}
        melhor = None
        if len(wapes) == len(MOTORES_VIVOS):
            candidato = min(wapes, key=wapes.get)
            # Empate nao elege ninguem (o min pegaria um arbitrario).
            if sum(1 for w in wapes.values()
                   if w == wapes[candidato]) == 1:
                melhor = candidato
        out.append({'loja_id': lid,
                    'nome': nomes_loja.get(lid, f'#{lid}'),
                    'motores': motores, 'melhor': melhor})
    return out


def resumo_acuracia(dias=30, motor=None):
    """Agrega vies e WAPE dos snapshots JA casados com data_alvo nos ultimos
    `dias`, opcionalmente filtrado por `motor`. Vies = previsto - realizado
    (positivo = superprevisao). WAPE = soma|previsto-realizado| /
    soma(realizado). Retorna dict com 'total', 'por_receita', 'por_loja',
    'por_lead', 'motores' (n de snapshots casados por motor),
    'circularidade_pct' e 'periodo_dias'."""
    corte = hoje() - timedelta(days=int(dias or 30))
    base = [PrevisaoSnapshot.realizado.isnot(None),
            PrevisaoSnapshot.data_alvo >= corte]
    if motor:
        base.append(PrevisaoSnapshot.motor == motor)

    _m = (func.sum(PrevisaoSnapshot.previsto),
          func.sum(PrevisaoSnapshot.realizado),
          func.sum(func.abs(PrevisaoSnapshot.previsto
                            - PrevisaoSnapshot.realizado)),
          func.count(PrevisaoSnapshot.id))

    rows_r = (db.session.query(PrevisaoSnapshot.receita_id, *_m)
              .filter(*base).group_by(PrevisaoSnapshot.receita_id).all())
    rows_l = (db.session.query(PrevisaoSnapshot.loja_id, *_m)
              .filter(*base).group_by(PrevisaoSnapshot.loja_id).all())
    rows_lead = (db.session.query(PrevisaoSnapshot.lead_dias, *_m)
                 .filter(*base).group_by(PrevisaoSnapshot.lead_dias).all())

    nomes = dict(db.session.query(Receita.id, Receita.nome).all())
    nomes_loja = dict(db.session.query(Loja.id, Loja.nome).all())

    por_receita = _agrega(rows_r, 'receita_id')
    for x in por_receita:
        x['nome'] = nomes.get(x['receita_id'], f"#{x['receita_id']}")
    por_loja = _agrega(rows_l, 'loja_id')
    for x in por_loja:
        x['nome'] = nomes_loja.get(x['loja_id'], f"#{x['loja_id']}")
    por_lead = _agrega(rows_lead, 'lead_dias')
    for x in por_lead:
        x['nome'] = ('lead ?' if x['lead_dias'] is None
                     else 'D-%d' % x['lead_dias'])
    por_lead.sort(key=lambda x: (x['lead_dias'] is None, x['lead_dias'] or 0))

    def _pct(num, den):
        return round(100 * num / den, 1) if den else None

    tot_prev = sum(x['previsto'] for x in por_receita)
    tot_real = sum(x['realizado'] for x in por_receita)
    # WAPE do total = soma dos |erro| POR SNAPSHOT (coluna agregada das
    # linhas), nao |Σprev - Σreal| (que cancelaria erros opostos) — igual v1.
    tot_abs = sum(int(r[3] or 0) for r in rows_r)
    total = {
        'previsto': tot_prev, 'realizado': tot_real,
        'vies': tot_prev - tot_real,
        'vies_pct': _pct(tot_prev - tot_real, tot_real),
        'wape_pct': _pct(tot_abs, tot_real),
        'n': sum(x['n'] for x in por_receita),
    }

    # Quantos snapshots casados existem por motor no periodo (pro filtro da
    # tela mostrar o que ja tem dado).
    motores = dict(db.session.query(PrevisaoSnapshot.motor,
                                    func.count(PrevisaoSnapshot.id))
                   .filter(PrevisaoSnapshot.realizado.isnot(None),
                           PrevisaoSnapshot.data_alvo >= corte)
                   .group_by(PrevisaoSnapshot.motor).all())

    # CIRCULARIDADE: % dos pedidos entregues do periodo que nasceram da
    # propria sugestao (rascunho auto-gerado, observacao fixa do gerar). Alta
    # = o "realizado" e em parte eco da previsao (aprovado sem editar).
    tot_ped = (db.session.query(func.count(PedidoLoja.id))
               .filter(PedidoLoja.status.in_(_STATUS_ENTREGUE),
                       PedidoLoja.data_entrega >= corte).scalar()) or 0
    tot_auto = (db.session.query(func.count(PedidoLoja.id))
                .filter(PedidoLoja.status.in_(_STATUS_ENTREGUE),
                        PedidoLoja.data_entrega >= corte,
                        PedidoLoja.observacao.like('Gerado do histórico%'))
                .scalar()) or 0

    return {'total': total, 'por_receita': por_receita, 'por_loja': por_loja,
            'por_lead': por_lead, 'motores': motores,
            'motor': motor, 'motor_label': MOTOR_LABEL.get(motor),
            'circularidade_pct': _pct(tot_auto, tot_ped),
            'pedidos_entregues': tot_ped, 'pedidos_auto': tot_auto,
            'periodo_dias': int(dias or 30)}
