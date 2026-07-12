"""Auditoria de MAPEAMENTOS de venda → estoque (12/07/2026, pedido do dono:
"tem dado diferenca nos estoques").

Read-only ESTRITO: levanta, direto do banco, as situacoes de mapeamento que
geram diferenca entre o vendido (Seru/lote) e o baixado no EstoqueLoja:

- loja Seru sem vinculo confirmado (pedido inteiro nao processa);
- produto Seru PENDENTE ou nunca mapeado com venda no periodo (vende e nao
  baixa — o estoque do sistema fica MAIOR que o real);
- produto IGNORADO com venda relevante (pode ser intencional — conferir);
- mapa apontando pra alvo morto (receita/MP arquivada) — baixa nao acontece;
- fator 0 (mapeado mas baixa nada) e fatores fracionarios (LISTA INFORMATIVA:
  o cafe->0.2 Cookie e REGRA DE NEGOCIO confirmada pelo dono, NAO e erro);
- cesta mapeada sem componentes e componente de cesta orfao (FK nula) —
  a venda "baixa" mas nenhum estoque se move;
- movimentos *_sem_estoque recentes (a baixa quis acontecer e o saldo ja
  estava zerado — sintoma de divergencia acumulada);
- SeruDebito travado (fracao acumulada >= 1 que nao virou baixa, ou negativa);
- pedidos Seru processados com itens NAO baixados no periodo.

Consumo: GET /api/claude/auditoria-mapeamentos (Bearer CLAUDE_API_TOKEN).
"""
import logging
from datetime import datetime, time, timedelta

from sqlalchemy import func

from app.extensions import db
from app.models import (
    EstoqueLoja,
    Loja,
    MateriaPrima,
    MovEstoqueLoja,
    Produto,
    ProdutoItem,
    Receita,
    SeruDebito,
    SeruLojaMap,
    SeruPedidoProcessado,
    VendaMapa,
    VendaSeruDiaria,
)
from app.utils import hoje

logger = logging.getLogger(__name__)


def _venda_por_nome(corte):
    """{seru_nome: {'qtd': x, 'faturamento': y}} do periodo, do snapshot."""
    rows = (db.session.query(
                VendaSeruDiaria.seru_nome,
                func.sum(VendaSeruDiaria.qtd),
                func.sum(VendaSeruDiaria.faturamento))
            .filter(VendaSeruDiaria.data >= corte)
            .group_by(VendaSeruDiaria.seru_nome).all())
    return {n: {'qtd': float(q or 0), 'faturamento': float(f or 0)}
            for n, q, f in rows}


def _nome_alvo(m):
    if m.receita_id:
        r = db.session.get(Receita, m.receita_id)
        return ('receita', m.receita_id, r.nome if r else None,
                bool(r and r.arquivada_em))
    if m.produto_id:
        p = db.session.get(Produto, m.produto_id)
        return ('produto', m.produto_id, p.nome if p else None, False)
    if m.materia_prima_id:
        mp = db.session.get(MateriaPrima, m.materia_prima_id)
        return ('mp', m.materia_prima_id, mp.nome if mp else None,
                bool(mp and mp.arquivada_em))
    return (None, None, None, False)


def auditar(dias=14):
    """Roda todas as verificacoes. Devolve dict serializavel (JSON)."""
    dias = max(1, min(int(dias or 14), 60))
    corte = hoje() - timedelta(days=dias - 1)
    venda = _venda_por_nome(corte)
    out = {'periodo_dias': dias, 'desde': corte.isoformat()}

    # 1) Lojas Seru sem vinculo utilizavel (pedido inteiro nao processa;
    #    o sync retenta pra sempre e NADA baixa).
    lojas_ruins = []
    vend_loja = dict(db.session.query(
        VendaSeruDiaria.loja_seru, func.sum(VendaSeruDiaria.qtd))
        .filter(VendaSeruDiaria.data >= corte)
        .group_by(VendaSeruDiaria.loja_seru).all())
    for slm in SeruLojaMap.query.all():
        if slm.ignorar:
            continue
        problema = None
        if not slm.loja_id:
            problema = 'sem loja vinculada'
        elif not slm.confirmado_em:
            problema = 'vinculo NAO confirmado (auto-fuzzy — sync nao baixa)'
        if problema:
            lojas_ruins.append({
                'loja_seru': slm.seru_company_name,
                'problema': problema,
                'qtd_vendida_periodo': float(
                    vend_loja.get(slm.seru_company_name, 0) or 0),
            })
    nomes_com_mapa_loja = {s.seru_company_name for s in SeruLojaMap.query}
    for nome, q in vend_loja.items():
        if nome not in nomes_com_mapa_loja:
            lojas_ruins.append({'loja_seru': nome,
                                'problema': 'company sem linha no SeruLojaMap',
                                'qtd_vendida_periodo': float(q or 0)})
    out['lojas_sem_vinculo'] = sorted(
        lojas_ruins, key=lambda x: -x['qtd_vendida_periodo'])

    # 2/3) Mapas do canal seru: pendentes/ignorados com venda no periodo.
    mapas = VendaMapa.query.filter_by(canal='seru').all()
    por_nome = {}
    pendentes, ignorados = [], []
    for m in mapas:
        por_nome.setdefault(m.nome_externo, []).append(m)
        v = venda.get(m.nome_externo)
        if not v or v['qtd'] <= 0:
            continue
        mapeado = bool(m.receita_id or m.produto_id or m.materia_prima_id)
        if m.ignorar:
            ignorados.append({'nome_externo': m.nome_externo, **v})
        elif not mapeado:
            pendentes.append({'nome_externo': m.nome_externo, **v})
    out['pendentes_com_venda'] = sorted(
        pendentes, key=lambda x: -x['qtd'])[:40]
    out['ignorados_com_venda'] = sorted(
        ignorados, key=lambda x: -x['qtd'])[:20]

    # 4) Vendido no periodo SEM nenhuma linha de mapa (nunca visto pelo
    #    sync — ou o sync esta atras).
    out['nomes_sem_mapa'] = sorted(
        [{'nome_externo': n, **v} for n, v in venda.items()
         if n not in por_nome and v['qtd'] > 0],
        key=lambda x: -x['qtd'])[:40]

    # 5) Alvo morto / fator zero / fatores fracionarios (informativo).
    alvos_mortos, fator_zero, fracionarios = [], [], []
    for m in mapas:
        if m.ignorar:
            continue
        tipo, alvo_id, nome_alvo, arquivado = _nome_alvo(m)
        if tipo and (nome_alvo is None or arquivado):
            alvos_mortos.append({
                'nome_externo': m.nome_externo, 'tipo': tipo,
                'alvo_id': alvo_id,
                'problema': ('alvo inexistente' if nome_alvo is None
                             else f'{tipo} arquivada: {nome_alvo}'),
                'qtd_vendida_periodo': venda.get(m.nome_externo,
                                                 {}).get('qtd', 0),
            })
        if tipo and (m.fator_quantidade or 0) <= 0:
            fator_zero.append({'nome_externo': m.nome_externo,
                               'fator': m.fator_quantidade,
                               'alvo': nome_alvo})
        elif tipo and 0 < (m.fator_quantidade or 1) < 1:
            fracionarios.append({'nome_externo': m.nome_externo,
                                 'fator': m.fator_quantidade,
                                 'alvo': nome_alvo,
                                 'qtd_vendida_periodo': venda.get(
                                     m.nome_externo, {}).get('qtd', 0)})
    out['alvos_mortos'] = sorted(
        alvos_mortos, key=lambda x: -x['qtd_vendida_periodo'])
    out['fator_zero'] = fator_zero
    out['fatores_fracionarios_informativo'] = sorted(
        fracionarios, key=lambda x: -x['qtd_vendida_periodo'])[:40]

    # (Duplicata de (canal, nome_externo) e impossivel: unique no schema.)

    # 7) Cesta mapeada sem componentes + componentes orfaos (FK nula):
    #    a venda processa mas NENHUM estoque se move (ou move parcial).
    prod_ids = {m.produto_id for m in mapas
                if m.produto_id and not m.ignorar}
    cestas_vazias, orfaos = [], []
    for pid in prod_ids:
        itens = ProdutoItem.query.filter_by(produto_id=pid).all()
        p = db.session.get(Produto, pid)
        nome_p = p.nome if p else f'#{pid}'
        if not itens:
            cestas_vazias.append({'produto_id': pid, 'produto': nome_p})
            continue
        for it in itens:
            if not it.receita_id and not it.materia_prima_id:
                orfaos.append({'produto_id': pid, 'produto': nome_p,
                               'item_nome': it.item_nome})
    out['cestas_vazias'] = cestas_vazias
    out['componentes_orfaos'] = orfaos

    # 8) Movimentos *_sem_estoque no periodo: a baixa aconteceu com saldo
    #    zerado — sintoma de que o saldo do sistema ja divergia.
    corte_dt = datetime.combine(corte, time.min)
    rows = (db.session.query(
                MovEstoqueLoja.tipo, EstoqueLoja.loja_id,
                EstoqueLoja.receita_id, EstoqueLoja.produto_id,
                EstoqueLoja.materia_prima_id,
                func.sum(MovEstoqueLoja.quantidade),
                func.count(MovEstoqueLoja.id))
            .join(EstoqueLoja,
                  MovEstoqueLoja.estoque_loja_id == EstoqueLoja.id)
            .filter(MovEstoqueLoja.tipo.like('%_sem_estoque'),
                    MovEstoqueLoja.data >= corte_dt)
            .group_by(MovEstoqueLoja.tipo, EstoqueLoja.loja_id,
                      EstoqueLoja.receita_id, EstoqueLoja.produto_id,
                      EstoqueLoja.materia_prima_id)
            .all())
    nomes_loja = dict(db.session.query(Loja.id, Loja.nome).all())
    sem_estoque = []
    for tipo, lid, rid, pid, mid, qtd, n in rows:
        if rid:
            alvo = db.session.get(Receita, rid)
        elif pid:
            alvo = db.session.get(Produto, pid)
        elif mid:
            alvo = db.session.get(MateriaPrima, mid)
        else:
            alvo = None
        sem_estoque.append({
            'tipo': tipo, 'loja': nomes_loja.get(lid, f'#{lid}'),
            'item': getattr(alvo, 'nome', None) or '(linha pendente)',
            'qtd': float(qtd or 0), 'movimentos': int(n),
        })
    out['sem_estoque_recente'] = sorted(
        sem_estoque, key=lambda x: -x['qtd'])[:60]

    # 9) SeruDebito travado: fracao >= 1 deveria ter virado baixa inteira;
    #    negativa e estado invalido (estorno alem do acumulado).
    travados = []
    for d in SeruDebito.query.all():
        f = float(d.fracao_pendente or 0)
        if f >= 1.0 or f < 0:
            travados.append({'loja': nomes_loja.get(d.loja_id,
                                                    f'#{d.loja_id}'),
                             'seru_produto_map_id': d.seru_produto_map_id,
                             'fracao_pendente': f})
    out['debitos_travados'] = travados

    # 10) Pedidos Seru processados com itens NAO baixados no periodo
    #     (produto pendente/ignorado na hora — agregado por loja).
    corte_dt2 = datetime.combine(corte, time.min)
    rows = (db.session.query(
                SeruPedidoProcessado.loja_id,
                func.count(SeruPedidoProcessado.seru_pedido_id),
                func.sum(SeruPedidoProcessado.n_itens_total
                         - SeruPedidoProcessado.n_itens_baixados))
            .filter(SeruPedidoProcessado.processado_em >= corte_dt2,
                    SeruPedidoProcessado.cancelado_em.is_(None),
                    SeruPedidoProcessado.n_itens_baixados
                    < SeruPedidoProcessado.n_itens_total)
            .group_by(SeruPedidoProcessado.loja_id).all())
    out['pedidos_com_itens_nao_baixados'] = [
        {'loja': nomes_loja.get(lid, f'#{lid}' if lid else '(sem loja)'),
         'pedidos': int(n), 'itens_nao_baixados': int(itens or 0)}
        for lid, n, itens in rows]

    return out
