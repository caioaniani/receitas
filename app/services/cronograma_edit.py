"""Edicao manual das celulas do cronograma de producao (29/06/2026).

O admin ajusta quanto produzir de uma receita num dia, direto na grade. Regras
(decisao do dono, revisada 30/06):
- Edicao POR CELULA: cada dia editado salva o seu proprio CronogramaOverride;
  os outros dias seguem a sugestao calculada. O TOTAL da linha eh a SOMA das
  celulas — entao da pra produzir MAIS (ou menos) que o "Produzir" sugerido, e
  da pra programar receita SEM pedido (linha zerada). (Antes redistribuia
  mantendo o total fixo e clampava no Produzir: nao deixava aumentar nem editar
  linha zerada — "a edicao nao pegava". Bug pego pelo dono 30/06.)
- Salva rascunho ao editar: a edicao vira CronogramaOverride (sobrevive a
  recarregar; o cronograma e o aprovar passam a usar ela).

aplicar_overrides aplica POR CELULA: o dia com override usa o valor manual, os
demais seguem a sugestao. So o dia editado fica "congelado"; os outros ainda
acompanham a demanda nova.
"""
from collections import defaultdict
from datetime import date
from math import ceil

from app.extensions import db


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
    unico dia la salva o override daquela celula e o grid passa a refletir.

    Anti-staleness (E3): o modelo por-celula NAO reverte sozinho quando a demanda
    muda (era o comportamento antigo, que exigia cobrir todo o horizonte). Entao
    aqui comparamos o total MANUAL com o que o calculo sugere AGORA; se divergem e
    a edicao e de um dia anterior, marca rr['override_stale']=True + guarda o
    sugerido e a data da edicao, pro grid avisar 'edicao pode estar desatualizada'
    (a decisao de manter ou resetar fica com o usuario)."""
    from app.models import CronogramaOverride, Receita
    from app.services.producao import fornadas_amassadeira
    from app.utils import hoje
    if not receitas_out:
        return
    rids = [rr['receita_id'] for rr in receitas_out]
    ovs = (CronogramaOverride.query
           .filter(CronogramaOverride.data.in_(list(dias_prod)),
                   CronogramaOverride.receita_id.in_(rids)).all())
    if not ovs:
        return
    por_rid = defaultdict(dict)
    edit_dia = {}                         # rid -> data (date) da edicao mais antiga
    for o in ovs:
        por_rid[o.receita_id][o.data] = o.qtd
        criado = o.criado_em.date() if o.criado_em else None
        if criado is not None:
            atual = edit_dia.get(o.receita_id)
            edit_dia[o.receita_id] = criado if atual is None else min(atual, criado)
    recs = {r.id: r for r in Receita.query.filter(Receita.id.in_(rids)).all()}
    hoje_d = hoje()
    for rr in receitas_out:
        ov = por_rid.get(rr['receita_id'])
        if not ov:
            continue
        rec = recs.get(rr['receita_id'])
        rend = int(rec.rendimento_qtd) if rec and rec.rendimento_qtd else 1
        sugerido_total = sum(c['qtd'] for c in rr['por_dia'])   # calc ANTES do override
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
        # E3: edicao de um dia anterior que ja nao bate com o calculo atual.
        dia_edit = edit_dia.get(rr['receita_id'])
        if rr['total'] != sugerido_total and dia_edit is not None \
                and dia_edit < hoje_d:
            rr['override_stale'] = True
            rr['override_sugerido'] = sugerido_total
            rr['override_desde'] = dia_edit.isoformat()


def editar_celula(receita_id, data_iso, qtd, horizonte_dias=7,
                  janela_semanas=6, inicio_offset_dias=0, equilibrar=False):
    """Edita UMA celula (receita x dia): fixa a qtd manual SO naquele dia (salva
    o override daquela celula), sem clamp — os outros dias seguem a sugestao
    calculada e overrides anteriores. O total da linha vira a SOMA das celulas,
    entao da pra produzir MAIS/menos que o sugerido e programar linha zerada.
    Devolve {receita_id, por_dia:[{data,qtd,fornadas}], total}. None se a
    receita/data nao esta no cronograma."""
    from app.models import Receita
    from app.services.previsao_producao import cronograma_producao
    from app.services.producao import fornadas_amassadeira
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
    novo_qtd = max(0, int(qtd))                       # sem clamp no total: da pra subir
    _salvar_overrides(int(receita_id), [alvo], [novo_qtd])   # so a celula editada

    qtds = [c['qtd'] for c in rr['por_dia']]
    qtds[idx] = novo_qtd                              # os outros dias ficam como estao
    rec = db.session.get(Receita, int(receita_id))
    rend = int(rec.rendimento_qtd) if rec and rec.rendimento_qtd else 1
    por_dia = [{'data': d.isoformat(), 'qtd': q,
                'fornadas': (fornadas_amassadeira(rec, max(1, ceil(q / rend)))
                             if q > 0 else None)}
               for d, q in zip(datas, qtds)]
    return {'receita_id': int(receita_id), 'por_dia': por_dia,
            'total': sum(qtds)}


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
