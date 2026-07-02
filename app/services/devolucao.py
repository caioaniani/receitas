"""Devolução de sobras loja → indústria (duas pontas, uma transação).

Caso de negócio (dono, 02/07/2026): croissants tradicionais que sobram nas
lojas voltam pra indústria pra virar Croissant Almond. A devolução precisa
BAIXAR o estoque da loja E CREDITAR o congelado da indústria no mesmo ato —
antes só existia o mov 'devolucao' da loja (a indústria nunca ficava sabendo
e o MRP pedia massa fresca pro Almond, ignorando os retornos).

Destino do crédito: `Receita.retorno_receita_id` quando setado (ex: Croissant
Tradicional → "Croissant Tradicional — Retorno"), senão a própria receita.
O retorno é receita SEPARADA porque a indústria mantém 1 linha por receita
(uq_estoque_producao_receita) e o retornado (assado, de véspera) não pode se
misturar com o congelado cru que atende pedidos das lojas.

Um token `dev-<hex>` amarra os movimentos das duas pontas — é a chave do
estorno. Salvaguardas iguais ao resto do estoque: nunca negativa saldo;
falta vira movimento `*_sem_estoque` visível, não erro silencioso.
"""
import secrets

from app.extensions import db
from app.models import Loja, MovEstoqueLoja, MovEstoqueProducao, Produto, Receita
from app.services.estoque_congelados import obter_linha_producao
from app.services.estoque_helpers import baixar_loja_por_prioridade

TIPO_BAIXA_LOJA = 'devolucao_industria'
TIPO_BAIXA_LOJA_SEM_ESTOQUE = 'devolucao_industria_sem_estoque'
TIPO_BAIXA_LOJA_ESTORNO = 'devolucao_industria_estorno'
TIPO_CREDITO_INDUSTRIA = 'retorno_loja'
TIPO_CREDITO_INDUSTRIA_ESTORNO = 'retorno_loja_estorno'


def _destino_do_retorno(tipo, item_id):
    """(coluna, id, nome_origem, nome_destino) do crédito na indústria.

    Receita com `retorno_receita_id` credita a receita de retorno; sem a
    config, credita a própria. Produto não tem receita de retorno — credita
    o próprio produto."""
    if tipo == 'receita':
        rec = Receita.query.get(item_id)
        if rec is None:
            return None
        destino = rec.retorno_receita if rec.retorno_receita_id else rec
        return ('receita_id', destino.id, rec.nome, destino.nome)
    if tipo == 'produto':
        prod = Produto.query.get(item_id)
        if prod is None:
            return None
        return ('produto_id', prod.id, prod.nome, prod.nome)
    return None


def devolver_industria(loja_id, itens, usuario_id, commit=True):
    """Devolve sobras da loja pra indústria — as DUAS pontas numa transação.

    itens: [{'tipo': 'receita'|'produto', 'id': int, 'qtd': int}]

    Por item: baixa o EstoqueLoja (limitado ao saldo — falta vira mov
    `devolucao_industria_sem_estoque`, não negativa) e credita a quantidade
    INTEIRA no EstoqueProducao do destino de retorno (o que chegou na
    indústria chegou, mesmo que o saldo da loja estivesse subcontado).

    `commit=False` deixa a transação pro chamador (rota que agrega outras
    escritas no mesmo POST). Retorna {'token', 'loja', 'itens', 'avisos'}.
    Levanta ValueError pra entrada inválida (loja/item inexistente, qtd <= 0).
    """
    loja = Loja.query.get(loja_id)
    if loja is None:
        raise ValueError(f'Loja id={loja_id} não encontrada.')
    if not itens:
        raise ValueError('Nenhum item pra devolver.')

    token = 'dev-' + secrets.token_hex(4)
    resumo = []
    avisos = []
    for it in itens:
        tipo = (it.get('tipo') or '').strip()
        try:
            item_id = int(it.get('id') or 0)
            qtd = int(it.get('qtd') or 0)
        except (TypeError, ValueError):
            raise ValueError(f'Item inválido: {it!r}') from None
        if qtd <= 0:
            raise ValueError(f'Quantidade deve ser positiva: {it!r}')
        destino = _destino_do_retorno(tipo, item_id)
        if destino is None:
            raise ValueError(f'Item não encontrado: tipo={tipo!r} id={item_id}')
        col_destino, destino_id, nome_origem, nome_destino = destino

        # Ponta 1 — LOJA: baixa limitada ao saldo (helper canônico registra
        # a falta como movimento visível em vez de negativar).
        filtro = {'loja_id': loja_id,
                  'receita_id': item_id if tipo == 'receita' else None,
                  'produto_id': item_id if tipo == 'produto' else None,
                  'materia_prima_id': None}
        r = baixar_loja_por_prioridade(
            filtro, qtd,
            tipo_mov=TIPO_BAIXA_LOJA,
            sem_estoque_tipo=TIPO_BAIXA_LOJA_SEM_ESTOQUE,
            referencia=f'Devolução p/ indústria {token}',
            usuario_id=usuario_id)
        if r['faltou']:
            avisos.append(
                f'{nome_origem}: saldo da loja tinha só {r["baixado"]} de '
                f'{qtd} — baixei o que havia; a indústria recebe os {qtd}.')

        # Ponta 2 — INDÚSTRIA: credita a quantidade inteira no destino.
        ep = obter_linha_producao(usuario_id=usuario_id,
                                  **{col_destino: destino_id})
        ep.quantidade = (ep.quantidade or 0) + qtd
        db.session.add(MovEstoqueProducao(
            estoque_producao_id=ep.id, tipo=TIPO_CREDITO_INDUSTRIA,
            quantidade=qtd,
            referencia=f'Retorno de {loja.nome} {token}',
            usuario_id=usuario_id))

        resumo.append({'tipo': tipo, 'id': item_id, 'qtd': qtd,
                       'nome': nome_origem, 'destino': nome_destino,
                       'baixado_loja': r['baixado'],
                       'faltou_loja': r['faltou']})

    if commit:
        db.session.commit()
    return {'token': token, 'loja': loja.nome, 'itens': resumo,
            'avisos': avisos}


def estornar_devolucao(token, usuario_id):
    """Reverte a devolução `token` nas duas pontas.

    Loja: re-credita exatamente o que foi BAIXADO (movs `devolucao_industria`
    do token). Indústria: re-baixa o que foi CREDITADO, limitado ao saldo
    atual — se a indústria já consumiu parte (ex: virou Almond), baixa o que
    houver e reporta a diferença no aviso. Idempotente por token: segunda
    chamada levanta ValueError. Commita no fim."""
    like = f'%{token}'
    ja = MovEstoqueLoja.query.filter(
        MovEstoqueLoja.tipo == TIPO_BAIXA_LOJA_ESTORNO,
        MovEstoqueLoja.referencia.like(like + '%')).first()
    if ja is not None:
        raise ValueError(f'Devolução {token} já estornada.')

    movs_loja = MovEstoqueLoja.query.filter(
        MovEstoqueLoja.tipo == TIPO_BAIXA_LOJA,
        MovEstoqueLoja.referencia.like(like)).all()
    movs_ind = MovEstoqueProducao.query.filter(
        MovEstoqueProducao.tipo == TIPO_CREDITO_INDUSTRIA,
        MovEstoqueProducao.referencia.like(like)).all()
    if not movs_loja and not movs_ind:
        raise ValueError(f'Devolução {token} não encontrada.')

    avisos = []
    for mov in movs_loja:
        el = mov.estoque
        el.quantidade = (el.quantidade or 0) + mov.quantidade
        db.session.add(MovEstoqueLoja(
            estoque_loja_id=el.id, tipo=TIPO_BAIXA_LOJA_ESTORNO,
            quantidade=mov.quantidade,
            referencia=f'Estorno da devolução {token}',
            usuario_id=usuario_id))

    for mov in movs_ind:
        ep = mov.estoque
        disp = int(ep.quantidade or 0)
        baixa = min(disp, mov.quantidade)
        ep.quantidade = disp - baixa
        if baixa < mov.quantidade:
            avisos.append(
                f'{ep.nome_item}: indústria já consumiu parte do retorno — '
                f'estornei {baixa} de {mov.quantidade}.')
        db.session.add(MovEstoqueProducao(
            estoque_producao_id=ep.id, tipo=TIPO_CREDITO_INDUSTRIA_ESTORNO,
            quantidade=baixa,
            referencia=f'Estorno da devolução {token}',
            usuario_id=usuario_id))

    db.session.commit()
    return {'token': token, 'avisos': avisos}


# ── Retirada de sobras (esteira em 2 tempos, movida por QR) ──────────────────
#
# A retirada separa as duas pontas NO TEMPO: a baixa da loja acontece na
# COLETA (motorista escaneou o QR na loja) e o crédito da indústria no
# RECEBIMENTO (QR escaneado na indústria). Mesmos tipos de movimento do fluxo
# manual (`devolucao_industria`/`retorno_loja`) com token `ret-<id>` — o
# Movimento do Dia, relatórios e estorno enxergam a mesma família.

def baixar_loja_retirada(retirada, usuario_id=None):
    """PONTA 1 (coleta): baixa o EstoqueLoja dos itens da retirada — limitado
    ao saldo; falta vira mov `devolucao_industria_sem_estoque` visível.
    NÃO commita (o handshake controla a transação). Retorna avisos."""
    avisos = []
    for it in retirada.itens:
        filtro = {'loja_id': retirada.loja_id,
                  'receita_id': it.receita_id,
                  'produto_id': it.produto_id,
                  'materia_prima_id': None}
        r = baixar_loja_por_prioridade(
            filtro, int(it.quantidade or 0),
            tipo_mov=TIPO_BAIXA_LOJA,
            sem_estoque_tipo=TIPO_BAIXA_LOJA_SEM_ESTOQUE,
            referencia=f'Retirada de sobras {retirada.token_mov}',
            usuario_id=usuario_id)
        if r['faltou']:
            avisos.append(
                f'{it.nome_item}: saldo da loja tinha só {r["baixado"]} de '
                f'{it.quantidade} — baixei o que havia.')
    return avisos


def creditar_industria_retirada(retirada, usuario_id=None):
    """PONTA 2 (recebimento): credita o EstoqueProducao no destino de retorno
    de cada item (usa `quantidade_recebida` quando a indústria conferiu com
    divergência; senão a declarada). NÃO commita. Retorna resumo por item."""
    resumo = []
    for it in retirada.itens:
        qtd = int(it.quantidade_recebida
                  if it.quantidade_recebida is not None else it.quantidade)
        if qtd <= 0:
            continue
        tipo = 'receita' if it.receita_id else 'produto'
        destino = _destino_do_retorno(tipo, it.receita_id or it.produto_id)
        if destino is None:
            resumo.append({'nome': it.nome_item, 'qtd': qtd,
                           'erro': 'item sem cadastro'})
            continue
        col_destino, destino_id, nome_origem, nome_destino = destino
        ep = obter_linha_producao(usuario_id=usuario_id,
                                  **{col_destino: destino_id})
        ep.quantidade = (ep.quantidade or 0) + qtd
        db.session.add(MovEstoqueProducao(
            estoque_producao_id=ep.id, tipo=TIPO_CREDITO_INDUSTRIA,
            quantidade=qtd,
            referencia=(f'Retorno de {retirada.loja.nome} '
                        f'{retirada.token_mov}'),
            usuario_id=usuario_id))
        resumo.append({'nome': nome_origem, 'qtd': qtd,
                       'destino': nome_destino})
    return resumo
