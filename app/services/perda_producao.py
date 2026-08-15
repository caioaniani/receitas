"""Perda de PRODUÇÃO lançada pelo padeiro (13/08/2026, pedido do dono:
"colocar as perdas na tela do padeiro, eles precisam ter uma aba para
lançar se queimou algo"). Decisões dele via AskUserQuestion:

1. Perda de item PRONTO **debita o EstoqueProducao** (saturando em 0 —
   nunca negativa; a falta fica no ledger como `perda_producao_sem_estoque`
   e num aviso visível, padrão da casa).
2. **Fornada queimada** (queimou ANTES de lançar a produção): consome MP +
   sub-receitas prontas da ficha pelo MESMO motor do produzir
   (`producao.consumir_ficha`) SEM creditar estoque — um gesto só, a
   contabilidade fecha (MP consumida, produto inexistente). A falta do
   plano do dia NÃO muda (ele vai reassar, ou a auditoria trata) e a
   pré-baixa de MP fica intocada (a falta segue reservada pra re-assar).
3. Relatório admin com CUSTO em R$ pela ficha (`/producao/perdas`).

Exclusão (admin, lançou errado): estorna EXATO pelo movimento gravado
(`perda_producao_estorno` do que de fato saiu — nunca a quantidade nominal,
regra do desperdício das lojas). Perda de FORNADA não tem estorno
automático: MP e fração de sub-receita consumidas não são reversíveis com
segurança (acumulador ConsumoSubFracao) — recusa com orientação.
"""
import logging

from app.extensions import db
from app.models import (
    EstoqueProducao,
    MovEstoqueProducao,
    PerdaProducao,
    Receita,
)
from app.utils import agora

logger = logging.getLogger(__name__)

MOTIVOS = {
    'queimou': 'Queimou no forno',
    'caiu': 'Caiu / contaminou',
    'erro_producao': 'Erro de produção (ponto, massa...)',
    'vencido': 'Venceu / estragou',
    'outro': 'Outro',
}

# Sanidade: acima disso é quase certeza de dedo errado (maior fornada real
# tem ~centenas de unidades). Recusa com mensagem clara em vez de debitar.
QTD_MAXIMA = 2000


def registrar(receita_id, quantidade, motivo, usuario_id, fornada=False,
              observacao=None):
    """Registra a perda e aplica o efeito de estoque. Commita.

    Retorna {'perda_id', 'baixado', 'falta', 'avisos': [str]}.
    Levanta ValueError com mensagem legível em entrada inválida (nada é
    gravado)."""
    from datetime import timedelta

    try:
        quantidade = int(quantidade or 0)
    except (TypeError, ValueError):
        raise ValueError('Quantidade inválida.') from None
    if quantidade <= 0:
        raise ValueError('Quantidade deve ser maior que zero.')
    if quantidade > QTD_MAXIMA:
        raise ValueError(f'Quantidade muito alta ({quantidade}) — confira o '
                         'número. Se for isso mesmo, lance em partes.')
    if motivo not in MOTIVOS:
        raise ValueError('Motivo inválido.')
    observacao = (observacao or '').strip() or None
    if motivo == 'outro' and not observacao:
        raise ValueError('Motivo "Outro" precisa da observação — escreva o '
                         'que aconteceu.')
    try:
        rec = db.session.get(Receita, int(receita_id or 0))
    except (TypeError, ValueError):
        raise ValueError('Receita inválida.') from None
    if rec is None:
        raise ValueError('Receita não encontrada.')
    if fornada and rec.arquivada_em is not None:
        # Item PRONTO de receita arquivada pode se perder (estoque físico
        # ainda escoa — mesma exceção da classe desperdício); consumir a
        # FICHA de receita morta não tem justificativa.
        raise ValueError('Receita arquivada não tem fornada — se há estoque '
                         'pronto dela se perdendo, lance SEM marcar '
                         '"fornada queimada".')

    # Guarda de duplo lançamento (padrão do checklist): o mesmo usuário com a
    # MESMA receita+quantidade em <30s é retry de rede/toque duplo, não uma
    # 2ª perda. Perda igual de verdade: espere meio minuto ou ajuste a qtd.
    recente = (PerdaProducao.query
               .filter(PerdaProducao.receita_id == rec.id,
                       PerdaProducao.quantidade == quantidade,
                       PerdaProducao.criado_por_id == usuario_id,
                       PerdaProducao.criado_em >= agora() - timedelta(
                           seconds=30))
               .first())
    if recente is not None:
        raise ValueError('Uma perda IGUAL a essa foi registrada há segundos '
                         '(#%d) — não repeti. Se foi outra perda de verdade, '
                         'aguarde meio minuto e lance de novo.' % recente.id)

    perda = PerdaProducao(
        receita_id=rec.id, quantidade=quantidade, motivo=motivo,
        observacao=observacao, fornada=bool(fornada),
        criado_por_id=usuario_id)
    db.session.add(perda)
    db.session.flush()

    avisos = []
    baixado = falta = 0
    if fornada:
        # Fornada queimada: consome a ficha (MP + subs) como o produzir,
        # SEM creditar estoque — o produto nunca existiu. Nenhum movimento
        # na linha da receita de propósito: mov sem efeito de saldo
        # quebraria a leitura por tipo do ledger; o registro é a PerdaProducao.
        from app.services.producao import consumir_ficha
        consumir_ficha(rec, quantidade, usuario_id,
                       referencia_mp='Fornada queimada %s (%d un) — perda #%d'
                       % (rec.nome, quantidade, perda.id))
    else:
        from app.services.estoque_congelados import saida_producao
        res = saida_producao(
            receita_id=rec.id, quantidade=quantidade, usuario_id=usuario_id,
            referencia='Perda #%d — %s' % (perda.id, MOTIVOS[motivo]),
            tipo='perda_producao')
        baixado, falta = res['baixado'], res['falta']
        if falta > 0:
            avisos.append(
                f'O estoque da indústria só tinha {baixado} de '
                f'{rec.nome} — a perda foi registrada inteira ({quantidade}) '
                f'e o estoque foi a zero, nunca negativo. Se a prateleira '
                'tinha mais, o saldo do sistema estava errado: vale conferir.')

    db.session.commit()
    logger.info('perda_producao #%d: %s x%d motivo=%s fornada=%s '
                'baixado=%d falta=%d', perda.id, rec.nome, quantidade,
                motivo, bool(fornada), baixado, falta)
    return {'perda_id': perda.id, 'baixado': baixado, 'falta': falta,
            'avisos': avisos}


def excluir(perda_id, usuario_id):
    """Exclui a perda ESTORNANDO exato o que saiu do estoque (admin —
    "lançou errado"). Commita. Retorna {'estornado': int}.

    Perda de FORNADA é recusada: a MP e a fração acumulada de sub-receita
    consumidas não têm estorno automático seguro — o acerto é manual
    (recebimento/ajuste de MP)."""
    perda = db.session.get(PerdaProducao, int(perda_id or 0))
    if perda is None:
        raise ValueError('Perda não encontrada (já excluída?).')
    if perda.fornada:
        raise ValueError(
            'Perda de FORNADA consumiu matéria-prima e sub-receitas da '
            'ficha — não há estorno automático. Se lançou errado, ajuste o '
            'estoque de MP na mão (recebimento/ajuste) e registre o motivo.')

    # Estorno EXATO: soma o que os movimentos 'perda_producao' desta perda
    # baixaram de verdade (a falta _sem_estoque nunca saiu — não volta).
    # O prefixo 'Perda #<id> — ' com delimitador não casa #1 com #12.
    ref_prefixo = 'Perda #%d — ' % perda.id
    movs = (db.session.query(MovEstoqueProducao)
            .join(EstoqueProducao,
                  MovEstoqueProducao.estoque_producao_id == EstoqueProducao.id)
            .filter(EstoqueProducao.receita_id == perda.receita_id,
                    MovEstoqueProducao.tipo == 'perda_producao',
                    MovEstoqueProducao.referencia.like(ref_prefixo + '%'))
            .all())
    estornado = 0
    for m in movs:
        ep = db.session.get(EstoqueProducao, m.estoque_producao_id)
        q = int(m.quantidade or 0)
        if ep is not None and q > 0:
            ep.quantidade = (ep.quantidade or 0) + q
            db.session.add(MovEstoqueProducao(
                estoque_producao_id=ep.id, tipo='perda_producao_estorno',
                quantidade=q,
                referencia='Estorno da perda #%d (excluída)' % perda.id,
                usuario_id=usuario_id))
            estornado += q
    db.session.delete(perda)
    db.session.commit()
    logger.info('perda_producao #%d excluída: %d un estornadas', perda_id,
                estornado)
    return {'estornado': estornado}


def listar(dias=30):
    """Perdas dos últimos `dias` com CUSTO pela ficha (relatório admin).

    Custo calculado 1x fora do loop (`calcular_custos_receitas`, chave por
    NOME — padrão da casa); receita sem custo calculável sai com custo 0 e
    flag `sem_custo`. Retorna {'perdas': [...], 'total_qtd', 'total_custo',
    'dias'}."""
    from datetime import timedelta

    from app.services.custos import calcular_custos_receitas

    corte = agora() - timedelta(days=max(1, min(int(dias or 30), 365)))
    rows = (PerdaProducao.query
            .filter(PerdaProducao.criado_em >= corte)
            .order_by(PerdaProducao.criado_em.desc())
            .limit(500)
            .all())
    custos_map = (calcular_custos_receitas() or {}).get('custos', {})
    perdas = []
    total_qtd = 0
    total_custo = 0.0
    for p in rows:
        nome = p.receita.nome if p.receita else f'#{p.receita_id}'
        custo_unit = float(custos_map.get(nome) or 0)
        custo_total = custo_unit * int(p.quantidade or 0)
        perdas.append({
            'id': p.id, 'receita': nome,
            'quantidade': int(p.quantidade or 0),
            'motivo': MOTIVOS.get(p.motivo, p.motivo),
            'observacao': p.observacao or '',
            'fornada': bool(p.fornada),
            'criado_em': p.criado_em,
            'quem': p.criado_por.nome if p.criado_por else '—',
            'custo_unit': custo_unit,
            'custo_total': custo_total,
            'sem_custo': custo_unit <= 0,
        })
        total_qtd += int(p.quantidade or 0)
        total_custo += custo_total
    return {'perdas': perdas, 'total_qtd': total_qtd,
            'total_custo': total_custo, 'dias': int(dias or 30)}
