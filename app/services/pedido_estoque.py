"""Baixa e estorno de estoque da INDÚSTRIA para pedidos loja→indústria.

Motor ÚNICO (03/07/2026 — auditoria das baixas): a rota web
(`/pedidos/<id>/enviar` + handshake QR) e o copilot (`mudar_status_pedido`)
usam ESTAS funções. Antes o copilot tinha uma cópia inline que divergia, e
AMBAS pulavam em silêncio o item sem linha de `EstoqueProducao` (`if ep:`) —
o pedido saía com baixa zero e nenhum rastro.

Regras canônicas:
- `baixar_industria_pedido`: para CADA item, decrementa `EstoqueProducao`
  criando a linha se não existir (`obter_linha_producao`, get-or-create) e
  grava `MovEstoqueProducao` com a quantidade REALMENTE baixada; o que faltou
  vira mov `saida_pedido_sem_estoque` (mesmo padrão do
  `venda_b2b_sem_estoque`: registra a falta sem deixar saldo negativo).
  Matéria-prima: baixa real com a falta anotada na referência
  (`MovimentacaoEstoque` só tem entrada/saida).
- `estornar_industria_pedido`: espelho EXATO da baixa — devolve o que os
  movimentos do pedido dizem que REALMENTE saiu (`saida_pedido` mais a
  quitação de falta `saida_pedido_quitada`, menos `estorno_saida_pedido`
  anteriores), não a quantidade nominal do item, e encerra as faltas
  abertas do pedido (`faltas_industria`, 05/10/2026).
  Assim baixa saturada em 0 não vira estoque fantasma no estorno, e
  reenviar depois de um estorno não corrompe a conta (o saldo líquido dos
  movimentos é sempre a verdade).

NÃO commitam nem mexem em status — o caller controla a transação.
"""
import logging

from sqlalchemy import func

from app.extensions import db
from app.models import (
    EstoqueProducao,
    MateriaPrima,
    MovEstoqueProducao,
    MovimentacaoEstoque,
)

logger = logging.getLogger(__name__)


def referencia_saida(pedido, ref_extra=None):
    """Referência canônica dos movimentos de saída do pedido. O ' →' logo
    depois do número é o que o estorno e a auditoria procuram (LIKE
    'Pedido #<id> →%'); a quitação de falta (`faltas_industria`) usa a mesma
    forma para ser devolvida junto no estorno."""
    ref = f'Pedido #{pedido.id} → {pedido.loja.nome}'
    if ref_extra:
        ref += f' ({ref_extra})'
    return ref


_ref_base = referencia_saida


def baixar_industria_pedido(pedido, usuario_id, ref_extra=None):
    """Baixa EstoqueProducao + MP de todos os itens do pedido.

    Retorna lista de faltas [{'item', 'pedido', 'baixado', 'faltou'}] (com
    `mp: True` nas de matéria-prima) — vazia quando tudo saiu com saldo. Falta NUNCA bloqueia o envio (o
    caminhão sai mesmo; a falta fica registrada pra acerto de inventário).
    """
    from app.services.estoque_congelados import obter_linha_producao

    ref = _ref_base(pedido, ref_extra)
    faltas = []
    for item in pedido.itens:
        qtd = item.quantidade or 0
        if qtd <= 0:
            continue
        if item.materia_prima_id:
            mp = db.session.get(MateriaPrima, item.materia_prima_id)
            if mp is None:
                continue
            disp = float(mp.estoque_atual or 0)
            baixa = min(float(qtd), disp)
            falta = float(qtd) - baixa
            mp.estoque_atual = disp - baixa
            ref_mp = ref + (f' — faltaram {falta:g}' if falta > 0 else '')
            db.session.add(MovimentacaoEstoque(
                materia_prima_id=mp.id, tipo='saida', quantidade=baixa,
                referencia=ref_mp, usuario_id=usuario_id))
            if falta > 0:
                # `mp`: matéria-prima não gera falta na TV do padeiro (só a
                # anotação na referência) — a mensagem do Enviar distingue.
                faltas.append({'item': mp.nome, 'pedido': float(qtd),
                               'baixado': baixa, 'faltou': falta, 'mp': True})
            continue
        if not (item.receita_id or item.produto_id):
            # Item solto (legado, só nome) — não há linha possível.
            logger.warning('baixar_industria_pedido: item #%s do pedido #%s '
                           'sem FK (receita/produto/MP) — sem baixa', item.id,
                           pedido.id)
            continue
        ep = obter_linha_producao(receita_id=item.receita_id,
                                  produto_id=item.produto_id,
                                  usuario_id=usuario_id)
        disp = int(ep.quantidade or 0)
        baixa = min(int(qtd), disp)
        falta = int(qtd) - baixa
        ep.quantidade = disp - baixa
        if baixa > 0:
            db.session.add(MovEstoqueProducao(
                estoque_producao_id=ep.id, tipo='saida_pedido',
                quantidade=baixa, referencia=ref, usuario_id=usuario_id))
        if falta > 0:
            db.session.add(MovEstoqueProducao(
                estoque_producao_id=ep.id, tipo='saida_pedido_sem_estoque',
                quantidade=falta, referencia=ref, usuario_id=usuario_id))
            # Fica registrada para a TV do padeiro mostrar e perguntar, ao
            # lançar a produção, se ela inclui o que já saiu (05/10/2026).
            from app.services import faltas_industria
            faltas_industria.registrar(pedido, ep, falta)
            faltas.append({'item': ep.nome_item, 'pedido': int(qtd),
                           'baixado': baixa, 'faltou': falta})
    return faltas


def texto_faltas_saida(faltas):
    """Frase única do aviso de saída com estoque insuficiente (botão Enviar
    e copilot). Só promete a pergunta na TV do padeiro para receita/produto;
    matéria-prima fica só no registro do estoque de MP."""
    if not faltas:
        return ''
    quais = '; '.join(f"{f['item']}: pedido {f['pedido']:g}, baixado {f['baixado']:g}"
                      for f in faltas)
    txt = f'saiu com estoque insuficiente na indústria ({quais}).'
    if any(not f.get('mp') for f in faltas):
        txt += (' A falta aparece na tela do padeiro: se a produção já foi feita, '
                'ao lançá-la ele confirma se ela inclui o que já saiu.')
    if any(f.get('mp') for f in faltas):
        txt += (' Matéria-prima com falta não aparece na tela do padeiro: '
                'acerte pela contagem do estoque de matéria-prima.')
    return txt


def estornar_industria_pedido(pedido, usuario_id, motivo='voltar status'):
    """Devolve à indústria o que o pedido REALMENTE baixou (pelos movimentos),
    linha a linha. Registra `estorno_saida_pedido` — um novo envio depois do
    estorno soma movimentos novos e o líquido continua correto.

    Retorna o total de unidades devolvidas (EstoqueProducao)."""
    ref_like = f'Pedido #{pedido.id} →%'
    ref_estorno = f'Estorno pedido #{pedido.id} ({motivo})'

    # EstoqueProducao: líquido por linha = (saida_pedido + quitação de falta)
    # − estornos anteriores. A quitação (05/10/2026) é o débito ATRASADO da
    # mesma saída — o estorno do pedido a devolve junto.
    from app.services import faltas_industria

    # Travas ANTES de somar, na mesma ordem da quitação (linha do estoque,
    # depois as faltas): uma quitação concorrente ou termina antes — e a
    # soma abaixo já enxerga o movimento dela — ou espera este estorno e
    # encontra as faltas encerradas. Sem isso a quitação podia comitar entre
    # a soma e o encerramento e ficar fora da devolução.
    linhas_ids = [r[0] for r in db.session.query(
        MovEstoqueProducao.estoque_producao_id)
        .filter(MovEstoqueProducao.referencia.like(ref_like))
        .distinct().all() if r[0] is not None]
    linhas = {}
    if linhas_ids:
        linhas = {ep.id: ep for ep in (
            EstoqueProducao.query.filter(EstoqueProducao.id.in_(linhas_ids))
            .order_by(EstoqueProducao.id)
            .with_for_update().populate_existing().all())}
    faltas_industria.travar_do_pedido(pedido.id)

    saidas = dict(db.session.query(
        MovEstoqueProducao.estoque_producao_id,
        func.sum(MovEstoqueProducao.quantidade))
        .filter(MovEstoqueProducao.tipo.in_(
                    ('saida_pedido', faltas_industria.TIPO_QUITADA)),
                MovEstoqueProducao.referencia.like(ref_like))
        .group_by(MovEstoqueProducao.estoque_producao_id).all())
    # 'ajuste' entra por compat: era o tipo do estorno ANTES deste motor —
    # sem ele, pedido estornado no código antigo e re-estornado aqui
    # devolveria em dobro.
    estornos = dict(db.session.query(
        MovEstoqueProducao.estoque_producao_id,
        func.sum(MovEstoqueProducao.quantidade))
        .filter(MovEstoqueProducao.tipo.in_(('estorno_saida_pedido', 'ajuste')),
                MovEstoqueProducao.referencia.like(f'Estorno pedido #{pedido.id} %'))
        .group_by(MovEstoqueProducao.estoque_producao_id).all())
    devolvidas = 0
    for ep_id, total in saidas.items():
        liquido = int(total or 0) - int(estornos.get(ep_id, 0) or 0)
        if liquido <= 0:
            continue
        ep = linhas.get(ep_id) or db.session.get(EstoqueProducao, ep_id)
        if ep is None:
            continue
        ep.quantidade = (ep.quantidade or 0) + liquido
        db.session.add(MovEstoqueProducao(
            estoque_producao_id=ep_id, tipo='estorno_saida_pedido',
            quantidade=liquido, referencia=ref_estorno,
            usuario_id=usuario_id))
        devolvidas += liquido

    # MP: líquido = saidas do pedido − entradas de estorno anteriores.
    saidas_mp = dict(db.session.query(
        MovimentacaoEstoque.materia_prima_id,
        func.sum(MovimentacaoEstoque.quantidade))
        .filter(MovimentacaoEstoque.tipo == 'saida',
                MovimentacaoEstoque.referencia.like(ref_like))
        .group_by(MovimentacaoEstoque.materia_prima_id).all())
    estornos_mp = dict(db.session.query(
        MovimentacaoEstoque.materia_prima_id,
        func.sum(MovimentacaoEstoque.quantidade))
        .filter(MovimentacaoEstoque.tipo == 'entrada',
                MovimentacaoEstoque.referencia.like(f'Estorno pedido #{pedido.id} %'))
        .group_by(MovimentacaoEstoque.materia_prima_id).all())
    for mp_id, total in saidas_mp.items():
        liquido = float(total or 0) - float(estornos_mp.get(mp_id, 0) or 0)
        if liquido <= 0:
            continue
        mp = db.session.get(MateriaPrima, mp_id)
        if mp is None:
            continue
        mp.estoque_atual = (mp.estoque_atual or 0) + liquido
        db.session.add(MovimentacaoEstoque(
            materia_prima_id=mp_id, tipo='entrada', quantidade=liquido,
            referencia=ref_estorno, usuario_id=usuario_id))
    # A saída foi desfeita: as faltas abertas dela deixam de valer (um novo
    # envio registra as próprias) e nenhuma produção pode quitá-las depois.
    faltas_industria.encerrar_do_pedido(pedido.id, 'estorno', usuario_id)
    return devolvidas
