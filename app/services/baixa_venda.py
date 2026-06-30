"""Motor UNICO de baixa de estoque de loja por VENDA (Seru + site + saida-lote).

Unifica o que estava triplicado em `seru_sync`, `loja_estoque_reserva` e
`estoque_loja_lote` (cada um re-implementava: resolver item -> aplicar fator ->
explodir cesta -> acumular fracao -> decrementar -> gerar movimento -> estornar).

Fluxo de `aplicar_venda`:
  1. Resolve a COMPOSICAO do item vendido (`cestas.composicao_de_venda`):
     cesta -> componentes; simples -> ele mesmo.
  2. Pra cada componente, consumo por unidade = `fator * qtd_no_componente`.
     - Inteiro (cafe normal, pao da cesta) -> baixa direta, sem acumulador.
     - Fracionario (cafe -> 0.2 cookie, sanduiche -> 0.2 sourdough) -> acumula
       em `DebitoEstoque` por (loja, ITEM FISICO) ate formar inteiro; rastreia a
       contribuicao em `DebitoEstoqueMov` pro estorno.
  3. Decrementa a linha canonica via `estoque_helpers.baixar_loja_por_prioridade`
     (que usa `obter_linha_loja` — respeita a trava de unicidade). Falta de
     saldo registra o movimento `*_sem_estoque`.

Estorno (`estornar_venda`), espelhando o estorno do Seru ja provado:
  - Fase 1: reverte os inteiros pela REFERENCIA do `MovEstoqueLoja` (exceto os
    marcados `(fracao)` — esses podem conter contribuicao de OUTROS pedidos, e
    sao revertidos so pela fase 2).
  - Fase 2: subtrai a contribuicao de cada `DebitoEstoqueMov` do acumulador; se
    o acumulador fica negativo, devolve inteiros ao estoque.

Strings de `MovEstoqueLoja.tipo` preservadas por canal (`venda_seru`/
`venda_site`/`saida_lote` + `_sem_estoque`/`_estorno`) — nao quebra historico
nem reconciliacao.
"""
from app.extensions import db
from app.models import DebitoEstoque, DebitoEstoqueMov, EstoqueLoja, MovEstoqueLoja
from app.services.cestas import composicao_de_venda
from app.services.estoque_helpers import baixar_loja_por_prioridade, obter_linha_loja
from app.utils import agora

_TOL = 1e-9
_TAG_FRACAO = '(fracao)'

# canal -> (tipo_baixa, tipo_sem_estoque, tipo_estorno)
_MOVS = {
    'seru': ('venda_seru', 'venda_seru_sem_estoque', 'venda_seru_estorno'),
    'site': ('venda_site', 'venda_site_sem_estoque', 'venda_site_estorno'),
    'lote': ('saida_lote', 'venda_loja_sem_estoque', 'saida_lote_estorno'),
}


def _movs(canal):
    return _MOVS.get(canal, ('venda', 'venda_sem_estoque', 'venda_estorno'))


def _item_filtro(col, item_id):
    """Filtro completo das 3 colunas de item (uma setada, duas None) — pra
    casar com a trava de unicidade de DebitoEstoque/EstoqueLoja."""
    base = {'receita_id': None, 'produto_id': None, 'materia_prima_id': None}
    base[col] = item_id
    return base


def _col_item(obj):
    """(coluna, id) do item fisico de um DebitoEstoqueMov/DebitoEstoque."""
    if obj.receita_id:
        return 'receita_id', obj.receita_id
    if obj.produto_id:
        return 'produto_id', obj.produto_id
    return 'materia_prima_id', obj.materia_prima_id


def _get_debito(loja_id, col, item_id):
    deb = DebitoEstoque.query.filter_by(
        loja_id=loja_id, **_item_filtro(col, item_id)).first()
    if deb is None:
        deb = DebitoEstoque(loja_id=loja_id, fracao_pendente=0.0,
                            **_item_filtro(col, item_id))
        db.session.add(deb)
        db.session.flush()
    return deb


def aplicar_venda(loja_id, *, receita_id=None, produto_id=None,
                  materia_prima_id=None, qtd, fator=1.0, canal,
                  referencia, pedido_ref, usuario_id=None, nome_venda=None):
    """Baixa o estoque de UMA venda (qtd unidades do item) na loja.

    Args:
      loja_id: loja que vende (de onde baixa).
      receita_id/produto_id/materia_prima_id: o item vendido (um deles).
      qtd: unidades vendidas (inteiro).
      fator: multiplicador escalar do canal (1 venda = fator unidades do alvo;
        cesta multi-item NAO usa — a composicao mora no Produto).
      canal: 'seru' | 'site' | 'lote' (define as strings de movimento).
      referencia: base textual do MovEstoqueLoja (ex: 'Seru #123'). Estavel —
        o estorno casa por ela.
      pedido_ref: chave do pedido por canal (ex: 'seru:123') — liga a fracao.
      nome_venda: nome do item vendido, so pra enriquecer a referencia da cesta.

    Retorna {baixado, faltou, acumulado, sem_alvo}.
    """
    comp = composicao_de_venda(receita_id=receita_id, produto_id=produto_id,
                               materia_prima_id=materia_prima_id)
    if not comp:
        return {'baixado': 0, 'faltou': 0, 'acumulado': False, 'sem_alvo': True}

    tipo_baixa, tipo_sem, _tipo_est = _movs(canal)
    fator = float(fator or 1.0)
    # eh_cesta = a composicao difere do proprio item vendido (1+ componentes).
    sold_col = ('receita_id' if receita_id else
                'produto_id' if produto_id else 'materia_prima_id')
    sold_id = receita_id or produto_id or materia_prima_id
    eh_cesta = not (len(comp) == 1 and comp[0][0] == sold_col
                    and comp[0][1] == sold_id)
    total_baixado = total_faltou = 0
    houve_acumulo = False

    for col, item_id, nome_comp, qpu in comp:
        por_unidade = fator * float(qpu)
        contrib = float(qtd) * por_unidade
        if contrib <= _TOL:
            continue
        ref = referencia
        if eh_cesta:
            ref = f'{referencia} [{nome_venda or "cesta"} -> cesta] {nome_comp}'

        if abs(por_unidade - round(por_unidade)) < _TOL:
            # Consumo inteiro por unidade -> baixa direta, sem acumulador.
            inteiros = int(round(contrib))
        else:
            # Fracionario -> acumula a fracao no item fisico.
            deb = _get_debito(loja_id, col, item_id)
            novo_total = (deb.fracao_pendente or 0.0) + contrib
            inteiros = int(novo_total + _TOL)
            deb.fracao_pendente = max(0.0, round(novo_total - inteiros, 6))
            db.session.add(DebitoEstoqueMov(
                loja_id=loja_id, canal=canal, pedido_ref=pedido_ref,
                fracao=contrib, **_item_filtro(col, item_id)))
            houve_acumulo = True
            ref = f'{ref} {_TAG_FRACAO}'

        if inteiros <= 0:
            continue
        res = baixar_loja_por_prioridade(
            filtro_base={'loja_id': loja_id, col: item_id},
            inteiros=inteiros, tipo_mov=tipo_baixa, referencia=ref,
            sem_estoque_tipo=tipo_sem, usuario_id=usuario_id)
        total_baixado += res['baixado']
        total_faltou += res['faltou']

    return {'baixado': total_baixado, 'faltou': total_faltou,
            'acumulado': houve_acumulo, 'sem_alvo': False}


def estornar_venda(canal, pedido_ref, referencia, *, usuario_id=None):
    """Reverte uma venda ja baixada. Idempotencia das FRACOES via
    `DebitoEstoqueMov.estornado_em`; a fase 1 (inteiros) deve ser chamada uma
    vez (o chamador garante, como o `SeruPedidoProcessado.estornado_em`).

    Args iguais aos de `aplicar_venda`: `referencia` eh a MESMA base usada na
    baixa (ex: 'Seru #123'); `pedido_ref` a mesma chave (ex: 'seru:123').

    Retorna {revertido_inteiros, revertido_fracoes}.
    """
    tipo_baixa, _tipo_sem, tipo_est = _movs(canal)

    # Fase 1: inteiros, pela referencia (exceto baixas marcadas (fracao)).
    candidatos = MovEstoqueLoja.query.filter(
        MovEstoqueLoja.tipo == tipo_baixa,
        db.or_(MovEstoqueLoja.referencia == referencia,
               MovEstoqueLoja.referencia.like(referencia + ' %')),
    ).all()
    revertido_int = 0
    for m in candidatos:
        ref_m = m.referencia or ''
        # Pula baixas FRACIONARIAS — revertidas so pela fase 2 (acumulador):
        # '(fracao)' = motor novo; '(fator' = baixa do Seru ANTES do cutover
        # (transicao; as contribuicoes viram DebitoEstoqueMov na migracao).
        if _TAG_FRACAO in ref_m or '(fator' in ref_m:
            continue
        el = EstoqueLoja.query.get(m.estoque_loja_id)
        if el is None:
            continue
        el.quantidade = (el.quantidade or 0) + m.quantidade
        db.session.add(MovEstoqueLoja(
            estoque_loja_id=el.id, tipo=tipo_est, quantidade=m.quantidade,
            referencia=f'Estorno {referencia}', usuario_id=usuario_id))
        revertido_int += 1

    # Fase 2: fracoes, via DebitoEstoqueMov (subtrai do acumulador; se passar do
    # zero, devolve inteiros). Espelha seru_sync._estornar_pedido.
    fracoes = DebitoEstoqueMov.query.filter_by(
        canal=canal, pedido_ref=pedido_ref, estornado_em=None).all()
    revertido_frac = 0
    for fm in fracoes:
        col, item_id = _col_item(fm)
        deb = DebitoEstoque.query.filter_by(
            loja_id=fm.loja_id, **_item_filtro(col, item_id)).first()
        if deb is None:
            fm.estornado_em = agora()
            continue
        novo = float(deb.fracao_pendente or 0.0) - float(fm.fracao)
        if novo < -_TOL:
            inteiros_devolver = int(-novo + 1.0 - _TOL)
            if inteiros_devolver > 0:
                el = obter_linha_loja(loja_id=fm.loja_id, usuario_id=usuario_id,
                                      **{col: item_id})
                el.quantidade = (el.quantidade or 0) + inteiros_devolver
                db.session.add(MovEstoqueLoja(
                    estoque_loja_id=el.id, tipo=tipo_est,
                    quantidade=inteiros_devolver,
                    referencia=f'Estorno {referencia} {_TAG_FRACAO} residual',
                    usuario_id=usuario_id))
                novo = novo + inteiros_devolver
        deb.fracao_pendente = max(0.0, round(novo, 6))
        fm.estornado_em = agora()
        revertido_frac += 1

    return {'revertido_inteiros': revertido_int,
            'revertido_fracoes': revertido_frac}
