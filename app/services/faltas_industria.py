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
- Só faltas da janela entram na pergunta e na quitação: criadas a partir da
  meia-noite de `JANELA_DIAS` dias atrás (falta de sexta ainda vale na
  segunda inteira). Falta mais antiga quase sempre é produção que nunca foi
  lançada; quitá-la comeria produção nova. Ela continua aberta até uma
  contagem.
- Conferência/balanço da linha ENCERRA as faltas do item criadas ANTES de a
  contagem começar: a contagem já reflete o que saiu até ali.
- Massa para folhar fica fora da quitação (o saldo vive em gramas).
- A quitação nunca passa do número que a pessoa confirmou na tela nem do
  lançado agora; o servidor recalcula sob trava e pode quitar MENOS.
- Ordem das travas: linha do estoque, depois as faltas (quitação e estorno).

Nenhuma função aqui commita — o chamador controla a transação.
"""
import logging
from datetime import datetime, time, timedelta

from sqlalchemy.orm import joinedload

from app.extensions import db
from app.models import FaltaSaidaIndustria, MovEstoqueProducao, PedidoLoja, Receita
from app.utils import agora, hoje

logger = logging.getLogger(__name__)

JANELA_DIAS = 3
TIPO_QUITADA = 'saida_pedido_quitada'
REF_QUITACAO = 'falta quitada: produção lançada depois'
# Saída desfeita, pedido apagado ou item transferido para matéria-prima:
# a falta deixa de existir para a tela do QR e para o aviso do Enviar.
MOTIVOS_SEM_EXIBICAO = ('estorno', 'excluido', 'transferido')


def _inicio_janela():
    """Meia-noite de `JANELA_DIAS` dias atrás. Por data, não por 72 h
    corridas: a produção de sexta lançada na segunda ainda é perguntada, e a
    borda não muda entre o que a TV mostrou e o que o servidor recalcula."""
    return datetime.combine(hoje() - timedelta(days=JANELA_DIAS), time.min)


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
            .options(joinedload(FaltaSaidaIndustria.receita),
                     joinedload(FaltaSaidaIndustria.produto),
                     joinedload(FaltaSaidaIndustria.pedido)
                     .joinedload(PedidoLoja.loja))
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


def resumo_por_ref(grupos=None):
    """{ref: {'quantidade', 'nome', 'pedidos'}} das faltas QUITÁVEIS da
    janela — alimenta a pergunta da TV ('receita:<id>' / 'produto:<id>').
    `grupos` = resultado de `abertas_recentes()` já carregado (evita a
    segunda consulta na mesma tela)."""
    out = {}
    for g in (abertas_recentes() if grupos is None else grupos):
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
    alguém confirmou que a produção inclui o que já saiu. `maximo` é o MENOR
    entre o lançado agora e o número que a pessoa viu na pergunta — falta
    registrada depois da pergunta nunca é quitada sem confirmação. Nunca
    deixa saldo negativo. Devolve o total quitado. Não commita."""
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


def encerrar_por_contagem(itens, usuario_id, antes_de=None):
    """Encerra as faltas abertas dos itens contados criadas até `antes_de`
    (o início da contagem; None = agora), de qualquer idade: a
    conferência/balanço já reflete o que saiu até ali. Falta de uma saída
    posterior ao início da contagem continua aberta. `itens` = iterável de
    (receita_id, produto_id). Devolve quantas foram encerradas.

    Falta travada por uma quitação em andamento é PULADA (`skip_locked`):
    esperar por ela fecharia um ciclo de travas com a Produção extra de
    vários itens (a conferência já segura linhas de estoque). A quitação
    concorrente decide o saldo dela."""
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
    limite = antes_de or agora()
    rows = (_abertas().filter(db.or_(*filtros),
                              FaltaSaidaIndustria.criada_em <= limite)
            .populate_existing().with_for_update(skip_locked=True).all())
    for f in rows:
        _encerrar(f, 'contagem', usuario_id)
    return len(rows)


def travar_do_pedido(pedido_id):
    """Trava as faltas do pedido. O estorno chama depois de travar as linhas
    de estoque (mesma ordem da quitação) e antes de somar os movimentos."""
    return (FaltaSaidaIndustria.query
            .filter(FaltaSaidaIndustria.pedido_id == pedido_id)
            .order_by(FaltaSaidaIndustria.id)
            .populate_existing().with_for_update().all())


def encerrar_do_pedido(pedido_id, motivo, usuario_id=None):
    """Encerra as faltas de um pedido ('estorno' quando volta para
    separado; 'excluido' quando o pedido é apagado).

    As já encerradas (quitada/contagem) também passam a esse motivo, sem
    mudar quando nem por quem foram encerradas: o estorno devolve a
    quitação junto com a saída, e a tela de um novo envio não pode mostrar
    a falta de uma saída desfeita. A quantidade quitada fica como histórico."""
    rows = (FaltaSaidaIndustria.query
            .filter(FaltaSaidaIndustria.pedido_id == pedido_id)
            .order_by(FaltaSaidaIndustria.id)
            .populate_existing().with_for_update().all())
    n = 0
    for f in rows:
        if f.encerrada_em is None:
            _encerrar(f, motivo, usuario_id)
            n += 1
        elif f.motivo_encerramento not in MOTIVOS_SEM_EXIBICAO:
            f.motivo_encerramento = motivo
    return n


def soltar_do_pedido(pedido_id):
    """Antes de apagar o pedido: solta o vínculo das faltas dele (já
    encerradas com 'excluido'), pelo ORM para ficar no histórico."""
    rows = FaltaSaidaIndustria.query.filter_by(pedido_id=pedido_id).all()
    for f in rows:
        f.pedido_id = None
    return len(rows)


def reapontar_receita(origem_id, destino_id):
    """Fusão de receitas (`vinculos_transferir`): as faltas seguem o
    estoque, que se funde no destino. Pelo ORM para ficar no histórico."""
    rows = FaltaSaidaIndustria.query.filter_by(receita_id=origem_id).all()
    for f in rows:
        f.receita_id = destino_id
    return len(rows)


def encerrar_da_receita(receita_id, motivo, usuario_id=None):
    """Receita virou matéria-prima (`_transferir_para_mp`): matéria-prima
    não gera falta nem quitação, então as faltas abertas dela se encerram e
    nenhuma (aberta ou não) aparece mais nas telas do pedido."""
    rows = (FaltaSaidaIndustria.query
            .filter(FaltaSaidaIndustria.receita_id == receita_id)
            .populate_existing().with_for_update().all())
    n = 0
    for f in rows:
        if f.encerrada_em is None:
            _encerrar(f, motivo, usuario_id)
            n += 1
        elif f.motivo_encerramento not in MOTIVOS_SEM_EXIBICAO:
            f.motivo_encerramento = motivo
    return n


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
