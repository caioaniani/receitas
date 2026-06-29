"""Edicao manual das celulas do cronograma de producao (29/06/2026).

O admin ajusta quanto produzir de uma receita num dia, direto na grade. Regras
(decisao do dono):
- TOTAL da receita fica FIXO: editar um dia redistribui os OUTROS dias pra
  manter a soma = "Produzir" do balanco. (redistribuicao no servidor)
- Salva rascunho ao editar: a distribuicao manual vira CronogramaOverride
  (sobrevive a recarregar; o cronograma e o aprovar passam a usar ela).

Anti-staleness: o cronograma so aplica os overrides de uma receita se eles
cobrem TODOS os dias do horizonte E somam o total atual. Se a demanda mudou
(total != soma), os overrides sao ignorados — volta pra sugestao calculada,
sem precisar limpar nada.
"""
from collections import defaultdict
from datetime import date
from math import ceil

from app.extensions import db


def _redistribuir(por_dia, idx, novo, total):
    """Fixa por_dia[idx]=novo (clamp 0..total) e redistribui o resto pelos
    OUTROS dias, proporcional aos valores atuais (maior resto, >=0). Outros
    todos zero -> distribui igualmente. Soma final == total."""
    from app.services.previsao_producao import _distribuir_inteiro
    n = len(por_dia)
    novo = max(0, min(int(novo), int(total)))
    out = [0] * n
    out[idx] = novo
    resto = int(total) - novo
    outros = [i for i in range(n) if i != idx]
    pesos = [int(por_dia[i] or 0) for i in outros]
    if sum(pesos) <= 0:
        pesos = [1] * len(outros)
    dist = _distribuir_inteiro(resto, pesos)
    for k, i in enumerate(outros):
        out[i] = dist[k]
    return out


def _salvar_overrides(receita_id, datas, qtds):
    """Upsert CronogramaOverride pra cada (data, receita)."""
    from app.models import CronogramaOverride
    existentes = {o.data: o for o in CronogramaOverride.query.filter(
        CronogramaOverride.receita_id == receita_id,
        CronogramaOverride.data.in_(datas)).all()}
    for d, q in zip(datas, qtds):
        o = existentes.get(d)
        if o is not None:
            o.qtd = int(q)
        else:
            db.session.add(CronogramaOverride(
                receita_id=receita_id, data=d, qtd=int(q)))
    db.session.commit()


def aplicar_overrides(receitas_out, dias_prod):
    """Sobrepoe a sugestao calculada pelos overrides MANUAIS, POR CELULA
    (receita, dia): cada celula com override usa o valor manual; as demais
    seguem a sugestao calculada. O total da linha passa a ser a SOMA das
    celulas exibidas (mantem o grid consistente: total == soma das celulas, e a
    redistribuicao do grid preserva esse total). Recalcula fornadas e marca
    rr['editado']=True nas receitas com algum override no horizonte. Muta
    receitas_out in place. No-op quando nao ha override (caminho normal).

    Aplicacao por celula (em vez de exigir o set completo somando o total) e o
    que permite a mao-dupla com a tela 'editar plano' do padeiro: editar um
    unico dia la salva o override daquela celula e o grid passa a refletir."""
    from app.models import CronogramaOverride, Receita
    from app.services.producao import fornadas_amassadeira
    if not receitas_out:
        return
    rids = [rr['receita_id'] for rr in receitas_out]
    ovs = (CronogramaOverride.query
           .filter(CronogramaOverride.data.in_(list(dias_prod)),
                   CronogramaOverride.receita_id.in_(rids)).all())
    if not ovs:
        return
    por_rid = defaultdict(dict)
    for o in ovs:
        por_rid[o.receita_id][o.data] = o.qtd
    recs = {r.id: r for r in Receita.query.filter(Receita.id.in_(rids)).all()}
    for rr in receitas_out:
        ov = por_rid.get(rr['receita_id'])
        if not ov:
            continue
        rec = recs.get(rr['receita_id'])
        rend = int(rec.rendimento_qtd) if rec and rec.rendimento_qtd else 1
        for c in rr['por_dia']:
            d = date.fromisoformat(c['data'])
            if d not in ov:
                continue
            q = ov[d]
            c['qtd'] = q
            c['fornadas'] = (fornadas_amassadeira(rec, max(1, ceil(q / rend)))
                             if q > 0 else None)
        rr['total'] = sum(c['qtd'] for c in rr['por_dia'])
        rr['editado'] = True


def editar_celula(receita_id, data_iso, qtd, horizonte_dias=7,
                  janela_semanas=6, inicio_offset_dias=0, equilibrar=False):
    """Edita uma celula: fixa qtd no dia, redistribui mantendo o total da
    receita, persiste os overrides de TODOS os dias do horizonte e devolve a
    linha recalculada {receita_id, por_dia:[{data,qtd,fornadas}], total}.
    Retorna None se a receita/data nao esta no cronograma."""
    from app.models import Receita
    from app.services.previsao_producao import cronograma_producao
    crono = cronograma_producao(horizonte_dias=horizonte_dias,
                                janela_semanas=janela_semanas,
                                inicio_offset_dias=inicio_offset_dias,
                                equilibrar=equilibrar)
    rr = next((x for x in crono['receitas']
               if x['receita_id'] == int(receita_id)), None)
    if rr is None:
        return None
    datas = [date.fromisoformat(c['data']) for c in rr['por_dia']]
    alvo = date.fromisoformat(data_iso)
    if alvo not in datas:
        return None
    idx = datas.index(alvo)
    atual = [c['qtd'] for c in rr['por_dia']]
    novo = _redistribuir(atual, idx, qtd, rr['total'])
    _salvar_overrides(int(receita_id), datas, novo)

    rec = db.session.get(Receita, int(receita_id))
    rend = int(rec.rendimento_qtd) if rec and rec.rendimento_qtd else 1
    from app.services.producao import fornadas_amassadeira
    por_dia = [{'data': d.isoformat(), 'qtd': q,
                'fornadas': (fornadas_amassadeira(rec, max(1, ceil(q / rend)))
                             if q > 0 else None)}
               for d, q in zip(datas, novo)]
    return {'receita_id': int(receita_id), 'por_dia': por_dia,
            'total': rr['total']}


def resetar_receita(receita_id, datas_iso):
    """Apaga os overrides de uma receita nas datas dadas (volta pra sugestao
    calculada). Retorna quantos apagou."""
    from app.models import CronogramaOverride
    datas = [date.fromisoformat(d) for d in datas_iso]
    q = CronogramaOverride.query.filter(
        CronogramaOverride.receita_id == int(receita_id),
        CronogramaOverride.data.in_(datas))
    n = q.count()
    q.delete(synchronize_session=False)
    db.session.commit()
    return n


def limpar_todos_overrides():
    """Apaga TODAS as edicoes manuais (overrides) do cronograma — tudo volta pra
    sugestao calculada. So mexe no rascunho (CronogramaOverride); NAO toca em
    pedido enviado (PlanejamentoProducao), estoque nem MP. Retorna quantos
    apagou."""
    from app.models import CronogramaOverride
    n = CronogramaOverride.query.count()
    CronogramaOverride.query.delete(synchronize_session=False)
    db.session.commit()
    return n
