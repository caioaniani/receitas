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
        item.dispensada_em = agora()
        item.dispensada_por_id = user_id
        db.session.commit()
    return {'ok': True, 'receita': item.receita.nome if item.receita else '?'}


def reverter_dispensa(item_id):
    """Desfaz a dispensa (volta a mostrar como pendente). Retorna {'ok': bool}."""
    from app.models import PlanejamentoItem

    item = db.session.get(PlanejamentoItem, int(item_id)) if item_id else None
    if item is None:
        return {'ok': False, 'erro': 'Item do plano não encontrado.'}
    item.dispensada_em = None
    item.dispensada_por_id = None
    db.session.commit()
    return {'ok': True}
