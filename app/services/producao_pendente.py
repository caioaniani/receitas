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
from app.utils import hoje


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
        .filter(PlanejamentoProducao.enviado_ao_padeiro.isnot(False))
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

    Retorna {'vencido': [...], 'agendado': [...], 'vencidos_antigos': N,
    'total_vencido': N, 'total_agendado': N}. Cada linha traz receita, data,
    alvo, produzido, falta, dias (de atraso p/ vencido), criado_por, plano_id.
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
    vencido, agendado = [], []
    for p in planos:
        autor = p.autor.nome if getattr(p, 'autor', None) else None
        for it in p.itens:
            falta = _falta(it.qtd_alvo, it.produzido_qtd)
            if falta <= 0:
                continue
            rec = it.receita
            linha = {
                'plano_id': p.id, 'data': p.data,
                'receita_id': it.receita_id,
                'receita_nome': rec.nome if rec else '(receita removida)',
                'alvo': int(it.qtd_alvo or 0),
                'produzido': int(it.produzido_qtd or 0),
                'falta': falta,
                'criado_por': autor,
                'dias': (hoje_d - p.data).days,
            }
            (vencido if p.data < hoje_d else agendado).append(linha)
    vencido.sort(key=lambda x: x['data'], reverse=True)   # mais recente primeiro
    agendado.sort(key=lambda x: x['data'])                # mais próximo primeiro

    # Ordens vencidas mais ANTIGAS que a janela: só conta (não lista).
    antigos = (db.session.query(func.count(PlanejamentoItem.id))
               .join(PlanejamentoProducao,
                     PlanejamentoItem.planejamento_id == PlanejamentoProducao.id)
               .filter(PlanejamentoProducao.enviado_ao_padeiro.isnot(False),
                       PlanejamentoProducao.data < limite,
                       (func.coalesce(PlanejamentoItem.qtd_alvo, 0)
                        - func.coalesce(PlanejamentoItem.produzido_qtd, 0)) > 0)
               .scalar()) or 0
    return {
        'vencido': vencido, 'agendado': agendado,
        'total_vencido': sum(x['falta'] for x in vencido),
        'total_agendado': sum(x['falta'] for x in agendado),
        'vencidos_antigos': int(antigos),
        'dias_vencido': dias_vencido,
    }
