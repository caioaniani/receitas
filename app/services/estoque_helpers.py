"""Helpers de EstoqueLoja: linha canonica por produto + baixa.

Regra de negocio: o `estado` (backup/assado) eh instrucao do PEDIDO (a industria
prepara fora do padrao sob demanda), NAO uma dimensao de estoque. A loja guarda
UMA linha por produto, sem estado. Por isso a baixa de venda (Seru/VNDA) e o
recebimento operam sempre na linha unica do produto.

`obter_linha_loja` centraliza o get-or-create dessa linha e consolida, de forma
idempotente, eventuais linhas legadas duplicadas (uma por estado, ou copias).
"""
from app.extensions import db
from app.models import EstoqueLoja, MovEstoqueLoja


def obter_linha_loja(loja_id, *, receita_id=None, produto_id=None,
                     materia_prima_id=None, usuario_id=None):
    """Retorna a UNICA linha de EstoqueLoja do produto (estado ignorado).

    Se houver linhas legadas duplicadas do mesmo (loja, item) — varias por
    estado, ou copias sem trava — consolida na canonica (menor id): soma a
    quantidade, registra `MovEstoqueLoja(tipo='consolidacao_estado')`, reatribui
    o historico de movimentos das extras (a relationship tem delete-orphan;
    deletar sem reatribuir apagaria o log) e remove as sobras. Cria a linha se
    nao existir. Idempotente (com 1 linha so retorna). NAO commita.
    """
    if receita_id is None and produto_id is None and materia_prima_id is None:
        raise ValueError('obter_linha_loja exige receita_id, produto_id ou '
                         'materia_prima_id (nao consolida linhas pendentes).')
    filtro = {'loja_id': loja_id, 'receita_id': receita_id,
              'produto_id': produto_id, 'materia_prima_id': materia_prima_id}
    linhas = EstoqueLoja.query.filter_by(**filtro).order_by(EstoqueLoja.id).all()
    if not linhas:
        nova = EstoqueLoja(**filtro, estado=None, quantidade=0)
        db.session.add(nova)
        db.session.flush()
        return nova

    canonica = linhas[0]
    for extra in linhas[1:]:
        qtd = extra.quantidade or 0
        if qtd:
            canonica.quantidade = (canonica.quantidade or 0) + qtd
            tag = f' [{extra.estado}]' if extra.estado else ''
            db.session.add(MovEstoqueLoja(
                estoque_loja_id=canonica.id, tipo='consolidacao_estado',
                quantidade=qtd,
                referencia=f'Consolidacao da linha #{extra.id}{tag} (+{qtd})',
                usuario_id=usuario_id))
        # Preserva o historico: reatribui os movimentos da extra pra canonica.
        for mov in list(extra.movimentacoes):
            mov.estoque = canonica
        db.session.delete(extra)

    if canonica.estado is not None:
        canonica.estado = None
    db.session.flush()
    return canonica


def consolidar_estoque_duplicado(usuario_id=None):
    """Consolida TODAS as linhas duplicadas em 1 por produto (estado ignorado),
    na loja (EstoqueLoja) e na producao (EstoqueProducao). Reusa
    `obter_linha_loja`/`obter_linha_producao` (somam, auditam, preservam
    historico, removem sobras). Idempotente. NAO commita. Usado pela rota de
    consolidacao e pela migracao que cria a trava de unicidade.

    Retorna (n_loja, n_prod): quantos itens tinham duplicata.
    """
    from collections import defaultdict

    from app.models import EstoqueProducao
    from app.services.estoque_congelados import obter_linha_producao

    col = {'receita': 'receita_id', 'produto': 'produto_id',
           'mp': 'materia_prima_id'}

    grupos = defaultdict(list)
    for el in EstoqueLoja.query.all():
        if el.pendente:
            continue
        if el.receita_id:
            grupos[(el.loja_id, 'receita', el.receita_id)].append(el)
        elif el.produto_id:
            grupos[(el.loja_id, 'produto', el.produto_id)].append(el)
        elif el.materia_prima_id:
            grupos[(el.loja_id, 'mp', el.materia_prima_id)].append(el)
    n_loja = 0
    for (loja_id, tipo, fk_id), linhas in grupos.items():
        if len(linhas) < 2:
            continue
        obter_linha_loja(loja_id, usuario_id=usuario_id, **{col[tipo]: fk_id})
        n_loja += 1

    grupos_p = defaultdict(list)
    for ep in EstoqueProducao.query.all():
        if ep.pendente:
            continue
        if ep.receita_id:
            grupos_p[('receita', ep.receita_id)].append(ep)
        elif ep.produto_id:
            grupos_p[('produto', ep.produto_id)].append(ep)
    n_prod = 0
    for (tipo, fk_id), linhas in grupos_p.items():
        if len(linhas) < 2:
            continue
        obter_linha_producao(usuario_id=usuario_id, **{col[tipo]: fk_id})
        n_prod += 1

    return n_loja, n_prod


def baixar_loja_por_prioridade(filtro_base, inteiros, *,
                                tipo_mov, referencia, sem_estoque_tipo,
                                usuario_id):
    """Baixa `inteiros` unidades da linha unica do produto (loja).

    Nome mantido por compatibilidade com os chamadores (`seru_sync`,
    `vnda_sync`); nao ha mais "prioridade" por estado — o estoque de loja eh
    por produto. Falta de saldo registra `sem_estoque_tipo` na propria linha.

    Args:
      filtro_base: dict com `loja_id` + (`receita_id` OU `produto_id` OU
        `materia_prima_id`). NAO inclui `estado`.
      inteiros: quantidade total a baixar (>= 1).
      tipo_mov / sem_estoque_tipo: tipos de `MovEstoqueLoja` pra baixa OK / falta.

    Retorna {baixado: int, faltou: int}.
    """
    if inteiros <= 0:
        return {'baixado': 0, 'faltou': 0}

    el = obter_linha_loja(usuario_id=usuario_id, **filtro_base)
    atual = el.quantidade or 0
    baixa = min(inteiros, atual)
    el.quantidade = atual - baixa
    if baixa > 0:
        db.session.add(MovEstoqueLoja(
            estoque_loja_id=el.id, tipo=tipo_mov, quantidade=baixa,
            referencia=referencia, usuario_id=usuario_id))

    faltou = inteiros - baixa
    if faltou > 0:
        db.session.add(MovEstoqueLoja(
            estoque_loja_id=el.id, tipo=sem_estoque_tipo, quantidade=faltou,
            referencia=f'{referencia} — sem estoque suficiente',
            usuario_id=usuario_id))

    return {'baixado': baixa, 'faltou': faltou}
