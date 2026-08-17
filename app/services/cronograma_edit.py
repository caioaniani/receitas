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


def dias_fechados():
    """Set de `date` com cadeado (🔒) ativo no grid do cronograma."""
    from app.models import CronogramaDiaFechado
    return {f.data for f in CronogramaDiaFechado.query.all()}


def alternar_dia_fechado(data_alvo, user_id=None):
    """Fecha/reabre o cadeado de um dia. Retorna True se o dia FICOU fechado.

    Dia fechado: edicao de celula recusada (qualquer caminho) e as acoes em
    massa (limpar edicoes, reset por linha) PULAM o dia. Enviar/atualizar
    producao continua permitido — o cadeado protege o rascunho, nao a ordem."""
    from sqlalchemy.exc import IntegrityError

    from app.models import CronogramaDiaFechado
    existente = CronogramaDiaFechado.query.filter_by(data=data_alvo).first()
    if existente is not None:
        db.session.delete(existente)
        db.session.commit()
        return False
    db.session.add(CronogramaDiaFechado(data=data_alvo,
                                        criado_por_id=user_id))
    try:
        db.session.commit()
    except IntegrityError:
        # Duplo clique quase simultaneo: o outro POST inseriu primeiro
        # (unique de `data` segura) — o dia ja esta fechado, mesmo resultado.
        db.session.rollback()
    return True


def podar_dias_fechados_passados():
    """Apaga cadeados de dias que ja PASSARAM (o grid nunca mais os mostra;
    deixa-los blindaria overrides mortos do "limpar edicoes" pra sempre).
    Chamada barata no GET da tela. Retorna quantos apagou."""
    from app.models import CronogramaDiaFechado
    from app.utils import hoje
    q = CronogramaDiaFechado.query.filter(CronogramaDiaFechado.data < hoje())
    n = q.count()
    if n:
        q.delete(synchronize_session=False)
        db.session.commit()
    return n


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
    # Override legado de receita de RETORNO (criado antes do guard do
    # editar_celula, 13/07/2026) e IGNORADO: retorno nao se produz — aplicar
    # o valor re-poria fornada de devolucao no grid/plano.
    retorno_ids = {r for (r,) in db.session.query(Receita.retorno_receita_id)
                   .filter(Receita.retorno_receita_id.isnot(None)).distinct()}
    ovs = [o for o in ovs if o.receita_id not in retorno_ids]
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
        dia_edit = edit_dia.get(rr['receita_id'])
        # Sugerido/desde ficam SEMPRE na linha editada: a promocao a stale
        # por entrega-em-risco (cronograma_producao, decisao do dono
        # 10/07/2026) precisa deles mesmo em edicao de hoje. O template so
        # os mostra quando override_stale liga.
        rr['override_sugerido'] = sugerido_total
        rr['override_desde'] = (dia_edit or hoje_d).isoformat()
        # E3: edicao de um dia anterior que ja nao bate com o calculo atual.
        if rr['total'] != sugerido_total and dia_edit is not None \
                and dia_edit < hoje_d:
            rr['override_stale'] = True


def editar_celula(receita_id, data_iso, qtd, horizonte_dias=7,
                  janela_semanas=6, inicio_offset_dias=0, equilibrar=False,
                  motor='pedidos'):
    """Edita UMA celula (receita x dia): fixa a qtd manual SO naquele dia (salva
    o override daquela celula), sem clamp — os outros dias seguem a sugestao
    calculada e overrides anteriores. O total da linha vira a SOMA das celulas,
    entao da pra produzir MAIS/menos que o sugerido e programar linha zerada.

    Depois de salvar, RECALCULA o cronograma com o override novo e devolve
    tambem as linhas de INSUMO (`insumos`) — editar 10.000 pains reflete na
    Massa para folhar NA HORA, sem F5 (cobranca do dono 03/07/2026: a tela
    parecia nao calcular a massa; o MRP calculava, mas so no reload).

    Devolve {receita_id, por_dia:[{data,qtd,fornadas}], total, insumos}.
    None se a receita/data nao esta no cronograma (nada salvo)."""
    try:
        alvo = date.fromisoformat(data_iso)
    except (TypeError, ValueError):
        return None
    # Cadeado do dia (🔒, dono 08/07/2026): dia fechado nao aceita edicao por
    # NENHUM caminho (grid, mao-dupla do editar-plano) ate reabrir. O check e
    # barato e vem ANTES do calculo completo do cronograma.
    if alvo in dias_fechados():
        return {'erro': 'dia_fechado',
                'msg': 'Este dia está fechado com o cadeado (🔒). Reabra o '
                       'cadeado no cabeçalho do dia para editar.'}
    from app.services.previsao_producao import cronograma_producao
    crono = cronograma_producao(horizonte_dias=horizonte_dias,
                                janela_semanas=janela_semanas,
                                inicio_offset_dias=inicio_offset_dias,
                                equilibrar=equilibrar, motor=motor)
    rr = next((x for x in crono['receitas']
               if x['receita_id'] == int(receita_id)), None)
    if rr is None:
        return None
    datas = [date.fromisoformat(c['data']) for c in rr['por_dia']]
    if alvo not in datas:
        return None
    # Receita de RETORNO não se produz (dono, 13/07/2026): o estoque dela
    # entra por devolução das lojas, nunca por fornada. Recusa a edição por
    # QUALQUER caminho (grid, mão-dupla do editar-plano, IA-aplicar) — um
    # override qtd>0 re-injetaria a linha e ela fluiria pro plano do padeiro.
    from app.models import Receita
    eh_retorno = (db.session.query(Receita.id)
                  .filter(Receita.retorno_receita_id == int(receita_id))
                  .first() is not None)
    if eh_retorno:
        return {'erro': 'receita_retorno',
                'msg': 'Receita de retorno não se produz — o estoque dela '
                       'entra por devolução das lojas (sobras que voltam), '
                       'não por fornada.'}
    # Dia de produção bloqueado: fornada especial produz só sex/sáb (dono
    # 10/08/2026) e receita normal só seg-sex (dono 17/08/2026 — fim de
    # semana não produz). Recusa a edição ANTES de salvar — a tela já trava
    # a célula, mas o guard vale pra qualquer chamador (defesa em
    # profundidade).
    from app.services.previsao_producao import producao_permitida_no_dia
    rec = db.session.get(Receita, int(receita_id))
    if not producao_permitida_no_dia(rec, alvo):
        if getattr(rec, 'fornada_especial', False):
            msg = ('Fornada especial produz só sexta/sábado — a venda '
                   'de sáb/dom sai da véspera. Edite um desses dias.')
        else:
            msg = ('Fim de semana não produz (produção é de segunda a '
                   'sexta) — a demanda de sáb/dom sai de sexta. Edite '
                   'um dia útil.')
        return {'erro': 'dia_bloqueado', 'msg': msg}
    novo_qtd = max(0, int(qtd))                       # sem clamp no total: da pra subir
    _salvar_overrides(int(receita_id), [alvo], [novo_qtd])   # so a celula editada

    # Recalcula JA COM o override novo: a linha editada volta com a celula
    # fixada (aplicar_overrides) e os INSUMOS com a demanda derivada dela.
    crono2 = cronograma_producao(horizonte_dias=horizonte_dias,
                                 janela_semanas=janela_semanas,
                                 inicio_offset_dias=inicio_offset_dias,
                                 equilibrar=equilibrar, motor=motor)
    rr2 = next((x for x in crono2['receitas']
                if x['receita_id'] == int(receita_id)), None)
    if rr2 is None:                                   # defensivo — existia acima
        return None
    insumos = [{'receita_id': x['receita_id'], 'nome': x.get('nome'),
                'por_dia': x.get('por_dia', []), 'total': x.get('total', 0),
                'em_estoque': x.get('em_estoque', 0),
                'consumo_janela': x.get('consumo_janela')}
               for x in crono2['receitas'] if x.get('insumo')]
    return {'receita_id': int(receita_id), 'por_dia': rr2['por_dia'],
            'total': rr2['total'], 'insumos': insumos}


def resetar_receita(receita_id, datas_iso):
    """Apaga os overrides de uma receita nas datas dadas (volta pra sugestao
    calculada). Dia com cadeado (🔒) e PULADO — o override dele fica.
    Retorna (apagados, preservados) — preservados = overrides da receita que
    ficaram por estarem em dia fechado (a tela avisa)."""
    from app.models import CronogramaOverride
    fechados = dias_fechados()
    todas = [date.fromisoformat(x) for x in datas_iso]
    datas = [d for d in todas if d not in fechados]
    puladas = [d for d in todas if d in fechados]
    preservados = 0
    if puladas:
        preservados = CronogramaOverride.query.filter(
            CronogramaOverride.receita_id == int(receita_id),
            CronogramaOverride.data.in_(puladas)).count()
    if not datas:
        return 0, preservados
    q = CronogramaOverride.query.filter(
        CronogramaOverride.receita_id == int(receita_id),
        CronogramaOverride.data.in_(datas))
    n = q.count()
    q.delete(synchronize_session=False)
    db.session.commit()
    return n, preservados


def limpar_todos_overrides():
    """Apaga as edicoes manuais (overrides) do cronograma — tudo volta pra
    sugestao calculada, EXCETO dias com cadeado (🔒): os overrides deles sao
    preservados. So mexe no rascunho (CronogramaOverride); NAO toca em pedido
    enviado (PlanejamentoProducao), estoque nem MP.

    Retorna (apagados, preservados)."""
    from app.models import CronogramaOverride
    fechados = dias_fechados()
    q = CronogramaOverride.query
    preservados = 0
    if fechados:
        preservados = q.filter(
            CronogramaOverride.data.in_(fechados)).count()
        q = q.filter(~CronogramaOverride.data.in_(fechados))
    n = q.count()
    q.delete(synchronize_session=False)
    db.session.commit()
    return n, preservados


def reverter_dia_para_ordem_enviada(data_alvo, horizonte_dias=7,
                                    janela_semanas=6, inicio_offset_dias=0,
                                    equilibrar=False, motor='pedidos'):
    """Desfaz as edicoes do grid de UM dia e traz de volta o que foi ENVIADO
    ao padeiro (pedido do dono 08/07/2026). E o INVERSO do "🔄 atualizar
    producao": em vez de empurrar o grid pra ordem, grava overrides que
    reproduzem o `qtd_alvo` de cada item da ordem (e zera as receitas que o
    grid mostra mas a ordem nao tem). NAO toca no `PlanejamentoProducao` — so
    no rascunho (`CronogramaOverride`). Dia fechado (🔒) recusa. Retorna
    {'ok', 'erro'?, 'n'?}.

    O override gravado e `qtd_alvo - qtd_extra`: o sync do envio SOMA a parcela
    extra (reagendada da auditoria) ao alvo, entao o grid que reproduz o alvo e
    o alvo menos o extra (o difere volta a bater exato — mesma conta do
    `_sync_itens_do_cronograma` na direcao contraria)."""
    from app.models import CronogramaOverride, PlanejamentoProducao
    if data_alvo in dias_fechados():
        return {'ok': False, 'erro': 'dia_fechado'}
    plano = (PlanejamentoProducao.query
             .filter_by(data=data_alvo, origem='cronograma').first())
    if plano is None or plano.enviado_ao_padeiro is False:
        return {'ok': False, 'erro': 'sem_ordem'}

    # qtd de grid que reproduz o qtd_alvo de cada item da ordem (nao dispensado).
    ordem = {}
    for it in plano.itens:
        if it.dispensada_em is not None:
            continue
        extra = int(it.qtd_extra or 0)
        ordem[it.receita_id] = max(0, int(it.qtd_alvo or 0) - extra)

    # Receitas que o grid mostra HOJE (>0) mas NAO estao na ordem (adicionadas
    # depois do envio, ou sugestao nova) — zera pra sumirem, como na ordem.
    from app.services.previsao_producao import cronograma_producao
    crono = cronograma_producao(horizonte_dias=horizonte_dias,
                                janela_semanas=janela_semanas,
                                inicio_offset_dias=inicio_offset_dias,
                                equilibrar=equilibrar, motor=motor)
    iso = data_alvo.isoformat()
    alvo = dict(ordem)
    for rr in crono['receitas']:
        if rr.get('insumo'):
            continue
        rid = rr['receita_id']
        if rid in alvo:
            continue
        cel = next((c for c in rr['por_dia'] if c['data'] == iso), None)
        if cel and int(cel['qtd'] or 0) > 0:
            alvo[rid] = 0

    if not alvo:
        return {'ok': True, 'n': 0}
    existentes = {o.receita_id: o for o in CronogramaOverride.query.filter(
        CronogramaOverride.data == data_alvo,
        CronogramaOverride.receita_id.in_(list(alvo))).all()}
    for rid, q in alvo.items():
        o = existentes.get(rid)
        if o is not None:
            o.qtd = int(q)
        else:
            db.session.add(CronogramaOverride(
                receita_id=rid, data=data_alvo, qtd=int(q)))
    db.session.commit()
    return {'ok': True, 'n': len(ordem)}
