"""Faltas de saída da indústria: visíveis na hora e quitadas, com
confirmação humana, quando a produção que já saiu é lançada depois.

Caso que criou (05/10/2026, brioche): o sistema dizia 76 e o físico era 22.
Os 54 de diferença eram exatamente os brioches que saíram nos pedidos de
29/09 a 01/10 com o estoque da indústria ZERADO no sistema — o padeiro
lançou a produção daqueles dias só no fim da tarde seguinte. A saída do
pedido desconta só o saldo do momento e anota o resto como
`saida_pedido_sem_estoque` (`pedido_estoque.baixar_industria_pedido`,
neutro no saldo); a produção lançada depois entrou inteira.

Decisão do dono (opção A): sem saldo negativo e sem quitação automática.
A falta aparece na hora (tela do QR, botão Enviar e TV do padeiro) e, ao
lançar a produção na TV, a tela pergunta se ela inclui o que já saiu. Só a
confirmação grava o débito, um movimento `saida_pedido_quitada` por pedido
com a MESMA referência da saída ('Pedido #<id> → ...'), para o estorno do
pedido devolver a quitação junto com a saída normal.

Regras:
- Só faltas da janela (`JANELA_DIAS`) entram na pergunta e na quitação. Falta
  mais antiga quase sempre é produção que nunca foi lançada; quitá-la
  comeria produção nova. Ela continua aberta até uma contagem.
- Conferência/balanço da linha ENCERRA as faltas abertas do item: a contagem
  já reflete o que saiu.
- Massa para folhar fica fora da quitação (o saldo vive em gramas).
- O servidor recalcula tudo sob trava; o número da tela é só a pergunta.

Nenhuma função aqui commita — o chamador controla a transação.
"""
import logging
from datetime import timedelta

from app.extensions import db
from app.models import FaltaSaidaIndustria, MovEstoqueProducao, Receita
from app.utils import agora

logger = logging.getLogger(__name__)

JANELA_DIAS = 3
TIPO_QUITADA = 'saida_pedido_quitada'
REF_QUITACAO = 'falta quitada: produção lançada depois'
MOTIVOS_SEM_EXIBICAO = ('estorno', 'excluido')


def _inicio_janela():
    return agora() - timedelta(days=JANELA_DIAS)


def _abertas():
    return FaltaSaidaIndustria.query.filter(
        FaltaSaidaIndustria.encerrada_em.is_(None))


def _filtro_item(receita_id=None, produto_id=None):
    if receita_id:
        return FaltaSaidaIndustria.receita_id == receita_id
    return FaltaSaidaIndustria.produto_id == produto_id


def quitavel(receita):
    """Massa para folhar não entra: o saldo dela é em gramas e a saída do
    pedido mexe só na contagem inteira."""
    if receita is None:
        return True
    from app.services.estoque_massa import eh_massa_folhar
    return not eh_massa_folhar(receita)


def registrar(pedido, ep, quantidade):
    """Registra a falta de um item que saiu com o saldo do sistema abaixo do
    pedido. Chamado por `baixar_industria_pedido` junto do movimento
    `saida_pedido_sem_estoque`."""
    q = int(quantidade or 0)
    if q <= 0 or ep is None or not (ep.receita_id or ep.produto_id):
        return None
    falta = FaltaSaidaIndustria(
        pedido_id=pedido.id, receita_id=ep.receita_id,
        produto_id=None if ep.receita_id else ep.produto_id,
        quantidade=q, quitada=0)
    db.session.add(falta)
    return falta


def _encerrar(falta, motivo, usuario_id):
    falta.encerrada_em = agora()
    falta.motivo_encerramento = motivo
    falta.encerrada_por_id = usuario_id


def _descricao_pedido(f):
    p = f.pedido
    loja = p.loja.nome if (p is not None and p.loja) else 'pedido apagado'
    return {'pedido_id': f.pedido_id, 'loja': loja,
            'quando': f.criada_em.strftime('%d/%m %H:%M') if f.criada_em else '',
            'quantidade': f.aberta_qtd}


def abertas_recentes():
    """Faltas abertas da janela agrupadas por item, para a TV do padeiro."""
    rows = (_abertas()
            .filter(FaltaSaidaIndustria.criada_em >= _inicio_janela())
            .order_by(FaltaSaidaIndustria.criada_em, FaltaSaidaIndustria.id)
            .all())
    grupos = {}
    for f in rows:
        if f.aberta_qtd <= 0:
            continue
        g = grupos.get(f.ref)
        if g is None:
            g = grupos[f.ref] = {
                'ref': f.ref, 'nome': f.nome_item, 'quantidade': 0,
                'quitavel': quitavel(f.receita), 'pedidos': []}
        g['quantidade'] += f.aberta_qtd
        g['pedidos'].append(_descricao_pedido(f))
    return sorted(grupos.values(), key=lambda g: g['nome'].lower())


def resumo_por_ref():
    """{ref: {'quantidade', 'nome', 'pedidos'}} das faltas QUITÁVEIS da
    janela — alimenta a pergunta da TV ('receita:<id>' / 'produto:<id>')."""
    out = {}
    for g in abertas_recentes():
        if not g['quitavel']:
            continue
        out[g['ref']] = {
            'quantidade': g['quantidade'], 'nome': g['nome'],
            'pedidos': ', '.join(
                f"#{p['pedido_id']} ({p['loja']}, {p['quando']})"
                for p in g['pedidos']),
        }
    return out


def quitar(*, receita_id=None, produto_id=None, maximo, usuario_id):
    """Debita do estoque da indústria até `maximo` unidades das faltas ABERTAS
    da janela do item, das mais antigas para as mais novas, gravando um
    `saida_pedido_quitada` por pedido.

    Chamar DEPOIS do crédito da produção, na mesma transação e só quando
    alguém confirmou que a produção inclui o que já saiu. Nunca deixa saldo
    negativo. Devolve o total quitado. Não commita."""
    maximo = int(maximo or 0)
    if maximo <= 0 or bool(receita_id) == bool(produto_id):
        return 0
    if receita_id:
        receita = db.session.get(Receita, receita_id)
        if receita is None or not quitavel(receita):
            return 0
    from app.services.estoque_congelados import obter_linha_producao
    from app.services.pedido_estoque import referencia_saida

    ep = obter_linha_producao(receita_id=receita_id, produto_id=produto_id,
                              usuario_id=usuario_id)
    faltas = (_abertas()
              .filter(_filtro_item(receita_id, produto_id),
                      FaltaSaidaIndustria.criada_em >= _inicio_janela())
              .order_by(FaltaSaidaIndustria.criada_em, FaltaSaidaIndustria.id)
              .populate_existing().with_for_update().all())
    restante = maximo
    total = 0
    for f in faltas:
        disponivel = int(ep.quantidade or 0)
        if restante <= 0 or disponivel <= 0:
            break
        if f.pedido is None:
            # Pedido apagado: a falta deveria ter sido encerrada na exclusão.
            _encerrar(f, 'excluido', usuario_id)
            continue
        q = min(f.aberta_qtd, restante, disponivel)
        if q <= 0:
            continue
        ep.quantidade = disponivel - q
        db.session.add(MovEstoqueProducao(
            estoque_producao_id=ep.id, tipo=TIPO_QUITADA, quantidade=q,
            referencia=referencia_saida(f.pedido, REF_QUITACAO)[:200],
            usuario_id=usuario_id))
        f.quitada = int(f.quitada or 0) + q
        if f.quitada >= int(f.quantidade or 0):
            _encerrar(f, 'quitada', usuario_id)
        restante -= q
        total += q
    if total:
        logger.info('faltas_industria: quitadas %d un de %s', total,
                    f'receita {receita_id}' if receita_id else f'produto {produto_id}')
    return total


def encerrar_por_contagem(itens, usuario_id):
    """Encerra TODAS as faltas abertas dos itens contados (qualquer idade):
    a conferência/balanço já reflete o que saiu. `itens` = iterável de
    (receita_id, produto_id). Devolve quantas foram encerradas."""
    itens = list(itens)
    rec_ids = sorted({r for r, _p in itens if r})
    prod_ids = sorted({p for r, p in itens if p and not r})
    if not (rec_ids or prod_ids):
        return 0
    filtros = []
    if rec_ids:
        filtros.append(FaltaSaidaIndustria.receita_id.in_(rec_ids))
    if prod_ids:
        filtros.append(FaltaSaidaIndustria.produto_id.in_(prod_ids))
    rows = (_abertas().filter(db.or_(*filtros))
            .populate_existing().with_for_update().all())
    for f in rows:
        _encerrar(f, 'contagem', usuario_id)
    return len(rows)


def encerrar_do_pedido(pedido_id, motivo, usuario_id=None):
    """Encerra as faltas abertas de um pedido ('estorno' quando volta para
    separado; 'excluido' quando o pedido é apagado)."""
    rows = (_abertas().filter(FaltaSaidaIndustria.pedido_id == pedido_id)
            .populate_existing().with_for_update().all())
    for f in rows:
        _encerrar(f, motivo, usuario_id)
    return len(rows)


def do_pedido(pedido_id):
    """Faltas da saída ATUAL do pedido (sem as de uma saída estornada), para
    a tela de sucesso do QR e o aviso do botão Enviar."""
    rows = (FaltaSaidaIndustria.query
            .filter(FaltaSaidaIndustria.pedido_id == pedido_id)
            .order_by(FaltaSaidaIndustria.id).all())
    out = []
    for f in rows:
        if f.motivo_encerramento in MOTIVOS_SEM_EXIBICAO:
            continue
        out.append({'nome': f.nome_item, 'quantidade': int(f.quantidade or 0),
                    'quitada': int(f.quitada or 0), 'aberta': f.aberta_qtd,
                    'motivo': f.motivo_encerramento})
    return out
