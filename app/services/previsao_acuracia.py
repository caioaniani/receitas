"""Acuracia do forecast do pedido semanal (28/06/2026).

Mede se a previsao acerta — antes nao havia NADA medindo, entao calibrar a
recencia (meia-vida) ou qualquer melhoria era no escuro.

Fluxo:
1. `registrar_snapshot()` (cron diario): congela o `previsto` do pedido
   semanal por (data de entrega, loja, receita). Idempotente — grava so a
   PRIMEIRA previsao vista pra cada data-alvo (~7 dias antes), pra medir
   sempre no mesmo lead.
2. `casar_realizados()` (cron diario): pra datas que ja passaram, preenche o
   `realizado` = entregue real daquele (loja, receita, data).
3. `resumo_acuracia()`: agrega vies e WAPE por receita pro painel.

Limitacao conhecida (v1): so capturamos itens que a previsao SUGERIU (qtd>0).
Falso-negativo puro (previu 0, aconteceu) nao entra — evita explodir a tabela
com toda combinacao loja×receita×dia. Da pra ampliar depois se o vies indicar
subprevisao sistematica.
"""
import logging
from datetime import date, timedelta

from sqlalchemy import func

from app.constants import STATUS_PEDIDO_FINALIZADOS
from app.extensions import db
from app.models import PedidoItem, PedidoLoja, PrevisaoSnapshot, Receita
from app.services.previsao_producao import sugerir_pedidos_semana
from app.utils import agora, hoje

logger = logging.getLogger(__name__)

# Entregue de verdade = finalizados MENOS cancelado (estes nao foram demanda).
_STATUS_ENTREGUE = tuple(s for s in STATUS_PEDIDO_FINALIZADOS if s != 'cancelado')


def registrar_snapshot(horizonte_dias=7, janela_semanas=6):
    """Congela a previsao atual por (data_alvo, loja, receita). Idempotente:
    nao sobrescreve um (data_alvo, loja, receita) ja snapshotado. Retorna o
    numero de snapshots novos criados."""
    sug = sugerir_pedidos_semana(horizonte_dias=horizonte_dias,
                                 janela_semanas=janela_semanas)
    datas = [date.fromisoformat(d['data']) for d in sug['dias']]
    if not datas:
        return 0
    existentes = set(
        db.session.query(PrevisaoSnapshot.data_alvo, PrevisaoSnapshot.loja_id,
                         PrevisaoSnapshot.receita_id)
        .filter(PrevisaoSnapshot.data_alvo.in_(datas)).all())
    novos = 0
    for loja in sug['lojas']:
        for dia in loja['dias']:
            data_alvo = date.fromisoformat(dia['data'])
            for it in dia['itens']:
                chave = (data_alvo, loja['loja_id'], it['receita_id'])
                if chave in existentes:
                    continue
                db.session.add(PrevisaoSnapshot(
                    data_alvo=data_alvo, loja_id=loja['loja_id'],
                    receita_id=it['receita_id'], previsto=int(it['qtd'])))
                existentes.add(chave)
                novos += 1
    if novos:
        db.session.commit()
    return novos


def casar_realizados():
    """Preenche `realizado` dos snapshots cuja data_alvo ja passou. Retorna o
    numero de snapshots casados nesta rodada."""
    hoje_d = hoje()
    pendentes = (PrevisaoSnapshot.query
                 .filter(PrevisaoSnapshot.realizado.is_(None),
                         PrevisaoSnapshot.data_alvo < hoje_d).all())
    if not pendentes:
        return 0
    agora_dt = agora()
    for snap in pendentes:
        total = (db.session.query(func.coalesce(func.sum(
                    func.coalesce(PedidoItem.quantidade_recebida,
                                  PedidoItem.quantidade)), 0))
                 .join(PedidoLoja, PedidoItem.pedido_id == PedidoLoja.id)
                 .filter(PedidoLoja.loja_id == snap.loja_id,
                         PedidoItem.receita_id == snap.receita_id,
                         PedidoLoja.data_entrega == snap.data_alvo,
                         PedidoLoja.status.in_(_STATUS_ENTREGUE))
                 .scalar())
        snap.realizado = int(total or 0)
        snap.casado_em = agora_dt
    db.session.commit()
    return len(pendentes)


def resumo_acuracia(dias=30):
    """Agrega vies e WAPE dos snapshots JA casados com data_alvo nos ultimos
    `dias`. Vies = previsto - realizado (positivo = superprevisao). WAPE =
    soma|previsto-realizado| / soma(realizado). Retorna dict com 'total',
    'por_receita' (ordenado por erro absoluto desc) e 'periodo_dias'."""
    corte = hoje() - timedelta(days=int(dias or 30))
    rows = (db.session.query(
                PrevisaoSnapshot.receita_id,
                func.sum(PrevisaoSnapshot.previsto),
                func.sum(PrevisaoSnapshot.realizado),
                func.sum(func.abs(PrevisaoSnapshot.previsto
                                  - PrevisaoSnapshot.realizado)),
                func.count(PrevisaoSnapshot.id))
            .filter(PrevisaoSnapshot.realizado.isnot(None),
                    PrevisaoSnapshot.data_alvo >= corte)
            .group_by(PrevisaoSnapshot.receita_id).all())
    nomes = dict(db.session.query(Receita.id, Receita.nome).all())

    def _pct(num, den):
        return round(100 * num / den, 1) if den else None

    por_receita = []
    tot_prev = tot_real = tot_abs = tot_n = 0
    for rid, prev, real, abserr, n in rows:
        prev, real, abserr = int(prev or 0), int(real or 0), int(abserr or 0)
        tot_prev += prev
        tot_real += real
        tot_abs += abserr
        tot_n += n
        por_receita.append({
            'receita_id': rid, 'nome': nomes.get(rid, f'#{rid}'),
            'previsto': prev, 'realizado': real, 'vies': prev - real,
            'vies_pct': _pct(prev - real, real),
            'wape_pct': _pct(abserr, real), 'n': n,
        })
    por_receita.sort(key=lambda x: -abs(x['vies']))
    total = {
        'previsto': tot_prev, 'realizado': tot_real, 'vies': tot_prev - tot_real,
        'vies_pct': _pct(tot_prev - tot_real, tot_real),
        'wape_pct': _pct(tot_abs, tot_real), 'n': tot_n,
    }
    return {'total': total, 'por_receita': por_receita,
            'periodo_dias': int(dias or 30)}
