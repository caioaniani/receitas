"""Produção MANDADA mas ainda não confirmada pelo padeiro (01/07/2026).

Cada ordem de produção (`PlanejamentoItem`) guarda `qtd_alvo` (o que a
administração mandou produzir) e `produzido_qtd` (o que o padeiro marcou como
feito, que credita o `EstoqueProducao` real). A diferença — `falta = qtd_alvo -
produzido_qtd` — é produção PENDENTE: mandada, ainda não confirmada.

Este módulo expõe essa pendência como uma camada de PROJEÇÃO ("o verde" da
tela), SEM nunca tocar no estoque real: o `EstoqueProducao` só sobe quando o
padeiro confirma (`producao.produzir_item_plano`). Misturar a pendência no
estoque real faria o balanço achar que tem produto que ainda não existe (e
deixaria vender fantasma) — por isso é sempre calculada na hora, aqui.

Categorias por data da ordem vs. hoje:
- **agendado**: ordem com `data >= hoje` (ainda no prazo pra produzir).
- **vencido**: ordem com `data < hoje` e ainda com falta — era pra já ter sido
  produzida e ninguém confirmou. É o sinal de AUDITORIA ("a indústria não
  apertou produzir").
"""
from collections import defaultdict
from datetime import timedelta

from sqlalchemy import func

from app.extensions import db
from app.utils import agora, hoje


def _falta(qtd_alvo, produzido):
    return max(0, int(qtd_alvo or 0) - int(produzido or 0))


def pendencias_por_receita():
    """{receita_id: {'agendado': N, 'vencido': N}} — unidades de produção
    mandadas (qtd_alvo) e ainda não confirmadas (produzido_qtd), das ordens
    ENVIADAS ao padeiro. NÃO é estoque real: é a projeção (o verde do grid)."""
    from app.models import PlanejamentoItem, PlanejamentoProducao

    hoje_d = hoje()
    out = defaultdict(lambda: {'agendado': 0, 'vencido': 0})
    rows = (db.session.query(
        PlanejamentoProducao.data, PlanejamentoItem.receita_id,
        PlanejamentoItem.qtd_alvo, PlanejamentoItem.produzido_qtd)
        .join(PlanejamentoItem,
              PlanejamentoItem.planejamento_id == PlanejamentoProducao.id)
        .filter(PlanejamentoProducao.enviado_ao_padeiro.isnot(False),
                PlanejamentoItem.dispensada_em.is_(None))   # dispensada some
        .all())
    for data, rid, alvo, produzido in rows:
        falta = _falta(alvo, produzido)
        if falta <= 0 or rid is None or data is None:
            continue
        chave = 'vencido' if data < hoje_d else 'agendado'
        out[rid][chave] += falta
    return dict(out)


def listar_pendencias(dias_vencido=30):
    """Ordens de produção pendentes (falta > 0) das ENVIADAS, pra a auditoria.

    Retorna {'vencido': [...], 'agendado': [...], 'dispensadas': [...],
    'vencidos_antigos': N, 'total_vencido': N, 'total_agendado': N}. Cada linha
    traz receita, data, alvo, produzido, falta, dias, criado_por, item_id.
    Itens DISPENSADOS (o admin deu OK) saem de vencido/agendado e vão pra
    `dispensadas` (com quem/quando), pra ficar o rastro sem poluir o pendente.
    Vencido só até `dias_vencido` atrás (ordens mais antigas provavelmente foram
    abandonadas — conta em `vencidos_antigos` em vez de poluir a lista)."""
    from app.models import PlanejamentoItem, PlanejamentoProducao

    hoje_d = hoje()
    limite = hoje_d - timedelta(days=dias_vencido)
    planos = (PlanejamentoProducao.query
              .filter(PlanejamentoProducao.enviado_ao_padeiro.isnot(False),
                      PlanejamentoProducao.data >= limite)
              .order_by(PlanejamentoProducao.data.asc())
              .all())
    vencido, agendado, dispensadas = [], [], []
    for p in planos:
        autor = p.autor.nome if getattr(p, 'autor', None) else None
        for it in p.itens:
            falta = _falta(it.qtd_alvo, it.produzido_qtd)
            if falta <= 0:
                continue
            rec = it.receita
            linha = {
                'item_id': it.id, 'plano_id': p.id, 'data': p.data,
                'receita_id': it.receita_id,
                'receita_nome': rec.nome if rec else '(receita removida)',
                'alvo': int(it.qtd_alvo or 0),
                'produzido': int(it.produzido_qtd or 0),
                'falta': falta,
                'criado_por': autor,
                'dias': (hoje_d - p.data).days,
                # Padeiro produziu menos e deu por feito (17/07/2026): a tela
                # dele esconde; a auditoria mostra o selo pro admin decidir.
                'falta_encerrada': it.falta_encerrada_em is not None,
            }
            if it.dispensada_em is not None:          # admin deu OK -> rastro
                quem = (it.dispensada_por.nome
                        if getattr(it, 'dispensada_por', None) else None)
                linha['dispensada_em'] = it.dispensada_em
                linha['dispensada_por'] = quem
                dispensadas.append(linha)
            else:
                (vencido if p.data < hoje_d else agendado).append(linha)
    vencido.sort(key=lambda x: x['data'], reverse=True)   # mais recente primeiro
    agendado.sort(key=lambda x: x['data'])                # mais próximo primeiro
    dispensadas.sort(key=lambda x: x['dispensada_em'], reverse=True)

    # Ordens vencidas mais ANTIGAS que a janela: só conta (não lista). Não conta
    # as dispensadas (o admin já resolveu).
    antigos = (db.session.query(func.count(PlanejamentoItem.id))
               .join(PlanejamentoProducao,
                     PlanejamentoItem.planejamento_id == PlanejamentoProducao.id)
               .filter(PlanejamentoProducao.enviado_ao_padeiro.isnot(False),
                       PlanejamentoProducao.data < limite,
                       PlanejamentoItem.dispensada_em.is_(None),
                       (func.coalesce(PlanejamentoItem.qtd_alvo, 0)
                        - func.coalesce(PlanejamentoItem.produzido_qtd, 0)) > 0)
               .scalar()) or 0
    return {
        'vencido': vencido, 'agendado': agendado, 'dispensadas': dispensadas,
        'total_vencido': sum(x['falta'] for x in vencido),
        'total_agendado': sum(x['falta'] for x in agendado),
        'vencidos_antigos': int(antigos),
        'dias_vencido': dias_vencido,
    }


def dispensar_item(item_id, user_id):
    """Fecha a pendência de UM item do plano: o admin verificou que não foi
    produzido (ou a menos) e dá OK. Marca dispensada_em/por — NÃO credita estoque
    nem mexe em produzido_qtd (o furo real fica preservado). Reversível.
    Retorna {'ok': True, 'receita': nome} ou {'ok': False, 'erro': ...}."""
    from app.models import PlanejamentoItem

    item = db.session.get(PlanejamentoItem, int(item_id)) if item_id else None
    if item is None:
        return {'ok': False, 'erro': 'Item do plano não encontrado.'}
    if item.dispensada_em is None:
        from app.services.producao import sincronizar_pre_baixa_mp
        item.dispensada_em = agora()
        item.dispensada_por_id = user_id
        # A falta dispensada não vai mais ser produzida — libera a MP
        # reservada pela pré-baixa (plano fora do regime = no-op).
        sincronizar_pre_baixa_mp(item.planejamento, user_id)
        db.session.commit()
    return {'ok': True, 'receita': item.receita.nome if item.receita else '?'}


def dispensar_itens(item_ids, user_id):
    """Dispensa VÁRIOS itens de uma vez (checkboxes da auditoria). Mesma
    semântica de `dispensar_item`: marca dispensada_em/por, NÃO credita estoque
    nem mexe em produzido_qtd. Ignora ids inválidos e os já dispensados (não
    reescreve quem/quando). Um único commit. Retorna {'ok': bool, 'n': quantos}."""
    from app.models import PlanejamentoItem

    ids = []
    for x in (item_ids or []):
        try:
            ids.append(int(x))
        except (TypeError, ValueError):
            continue
    if not ids:
        return {'ok': False, 'erro': 'Nenhum item marcado.', 'n': 0}
    itens = (PlanejamentoItem.query
             .filter(PlanejamentoItem.id.in_(ids),
                     PlanejamentoItem.dispensada_em.is_(None))
             .all())
    ts = agora()
    for item in itens:
        item.dispensada_em = ts
        item.dispensada_por_id = user_id
    if itens:
        from app.services.producao import sincronizar_pre_baixa_mp
        for plano in {item.planejamento for item in itens}:
            sincronizar_pre_baixa_mp(plano, user_id)
        db.session.commit()
    return {'ok': True, 'n': len(itens)}


def reverter_dispensa(item_id):
    """Desfaz a dispensa (volta a mostrar como pendente). Retorna {'ok': bool}."""
    from app.models import PlanejamentoItem

    item = db.session.get(PlanejamentoItem, int(item_id)) if item_id else None
    if item is None:
        return {'ok': False, 'erro': 'Item do plano não encontrado.'}
    from app.services.producao import sincronizar_pre_baixa_mp
    item.dispensada_em = None
    item.dispensada_por_id = None
    # Falta reaberta volta a reservar MP (plano fora do regime = no-op).
    sincronizar_pre_baixa_mp(item.planejamento)
    db.session.commit()
    return {'ok': True}


def _rendimento(rec):
    return float(rec.rendimento_qtd) if rec and rec.rendimento_qtd else 1.0


def reagendar_para_hoje(item_ids, user_id):
    """MOVE a falta de ordens pendentes/vencidas selecionadas pra a ordem de
    produção de HOJE (a que o padeiro vê em /padeiro).

    Pra cada item selecionado (falta = alvo − produzido > 0):
    - a falta entra no plano `cronograma` de HOJE — soma numa receita que já
      esteja lá, ou cria a linha; garante `enviado_ao_padeiro=True`.
    - a ordem antiga SAI da auditoria (decisão do dono — "mover, não duplicar"):
      se nada foi produzido nela, a linha é removida; se foi produzido em parte,
      o alvo cai pro produzido (falta → 0), preservando o crédito real de estoque
      (nunca abaixa alvo < produzido — mesma trava do produzir_item_plano).

    NÃO credita estoque (isso só acontece quando o padeiro confirma). Retorna
    {'movidos': N, 'unidades': N}."""
    from math import ceil

    from app.models import PlanejamentoItem, PlanejamentoProducao

    ids = [int(i) for i in (item_ids or []) if str(i).strip().isdigit()]
    if not ids:
        return {'movidos': 0, 'unidades': 0}

    hoje_d = hoje()
    plano_hoje = (PlanejamentoProducao.query
                  .filter_by(data=hoje_d, origem='cronograma').first())
    if plano_hoje is None:
        plano_hoje = PlanejamentoProducao(
            data=hoje_d, origem='cronograma', status='aprovado',
            nome='Produção %s' % hoje_d.strftime('%d/%m'),
            criado_por=user_id, enviado_ao_padeiro=True)
        db.session.add(plano_hoje)
        db.session.flush()
    else:
        plano_hoje.enviado_ao_padeiro = True   # garante que o padeiro veja

    # itens ja no plano de hoje, por receita (pra somar em vez de duplicar)
    por_receita = {it.receita_id: it for it in plano_hoje.itens}

    movidos, unidades = 0, 0
    planos_origem = set()
    for item_id in ids:
        old = db.session.get(PlanejamentoItem, item_id)
        if old is None or old.dispensada_em is not None:
            continue
        if old.planejamento_id == plano_hoje.id:   # ja e de hoje, ignora
            continue
        alvo = int(old.qtd_alvo or 0)
        prod = int(old.produzido_qtd or 0)
        falta = max(0, alvo - prod)
        if falta <= 0:
            continue

        # A falta entra como parcela EXTRA (qtd_extra): o re-aprovar/re-enviar
        # do cronograma reconstroi os itens do GRID e apagava o reagendado —
        # os paes sumiam da tela do padeiro (bug 02/07). O sync soma o extra
        # ao alvo do grid e nunca remove item com extra > 0.
        dest = por_receita.get(old.receita_id)
        if dest is not None:
            if dest.dispensada_em is not None:
                # Item de hoje estava DISPENSADO (a tela do padeiro esconde):
                # mandar produzir de novo REABRE — somar num item oculto
                # sumiria com a falta.
                dest.dispensada_em = None
                dest.dispensada_por_id = None
            # Mesma armadilha do marcador de falta ENCERRADA pelo padeiro
            # (17/07/2026): reagendar É o gesto "devolver pra tela dele" —
            # somar num item encerrado (oculto) engoliria a falta devolvida.
            dest.falta_encerrada_em = None
            dest.qtd_alvo = int(dest.qtd_alvo or 0) + falta
            dest.qtd_extra = int(dest.qtd_extra or 0) + falta
            dest.multiplicador = max(1, ceil(dest.qtd_alvo / _rendimento(dest.receita)))
        else:
            novo = PlanejamentoItem(
                planejamento_id=plano_hoje.id, receita_id=old.receita_id,
                qtd_alvo=falta, produzido_qtd=0, qtd_extra=falta,
                multiplicador=max(1, ceil(falta / _rendimento(old.receita))))
            db.session.add(novo)
            por_receita[old.receita_id] = novo

        # fecha a ordem antiga (sai da auditoria)
        planos_origem.add(old.planejamento)
        if prod <= 0:
            db.session.delete(old)             # nada produzido -> some
        else:
            old.qtd_alvo = prod                # falta -> 0, preserva o crédito
        movidos += 1
        unidades += falta

    if movidos:
        # A falta MUDOU de ordem: libera a reserva de MP nas ordens de origem
        # e reserva na de hoje. criar=True porque o reagendamento é gesto
        # explícito de envio (o plano de hoje pode nascer aqui, sem passar
        # pelo enviar_plano_do_dia).
        from app.services.producao import sincronizar_pre_baixa_mp
        for p in planos_origem:
            sincronizar_pre_baixa_mp(p, user_id)
        sincronizar_pre_baixa_mp(plano_hoje, user_id, criar=True)
    db.session.commit()
    return {'movidos': movidos, 'unidades': unidades}


def produzido_no_dia(dia=None):
    """O que o padeiro CONFIRMOU produzir num dia (default: ONTEM), lido dos
    movimentos REAIS de entrada na indústria (`MovEstoqueProducao` tipo=
    'producao') — a fonte datada da produção, não o `produzido_qtd` acumulado do
    item. Agrupa por receita.

    Retorna {'dia': date, 'itens': [{'receita_id', 'receita_nome', 'qtd'}],
    'total': N}."""
    from datetime import datetime, time

    from app.models import EstoqueProducao, MovEstoqueProducao, Receita

    dia = dia or (hoje() - timedelta(days=1))
    ini = datetime.combine(dia, time.min)
    fim = datetime.combine(dia, time.max)
    rows = (db.session.query(EstoqueProducao.receita_id,
                             func.sum(MovEstoqueProducao.quantidade))
            .join(EstoqueProducao,
                  MovEstoqueProducao.estoque_producao_id == EstoqueProducao.id)
            .filter(MovEstoqueProducao.tipo == 'producao',
                    MovEstoqueProducao.data >= ini,
                    MovEstoqueProducao.data <= fim,
                    EstoqueProducao.receita_id.isnot(None))
            .group_by(EstoqueProducao.receita_id)
            .all())
    nomes = {}
    if rows:
        ids = [rid for rid, _ in rows]
        nomes = {r.id: r.nome for r in
                 Receita.query.filter(Receita.id.in_(ids)).all()}
    itens = [{'receita_id': rid, 'receita_nome': nomes.get(rid, '(receita)'),
              'qtd': int(q or 0)} for rid, q in rows if (q or 0) > 0]
    itens.sort(key=lambda x: x['qtd'], reverse=True)
    return {'dia': dia, 'itens': itens, 'total': sum(x['qtd'] for x in itens)}
