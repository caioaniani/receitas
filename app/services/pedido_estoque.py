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
  (`MovimentacaoEstoque` só tem entrada/saida). Produto-CESTA baixa os
  componentes (`composicao_do_item`, 13/10/2026), igual à venda.
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


def componentes_explodiveis(produto):
    """Componentes de um Produto-CESTA que o pedido loja→indústria movimenta
    no lugar da linha do próprio produto (dono, 13/10/2026: "o pote desconta
    na indústria o que ele leva por dentro").

    Mesma composição da VENDA (`cestas.componentes_de_cesta`, um nível — a
    loja vende Granola 50g baixando 50 g de "Produção - Granola"), então a
    indústria debita e a loja recebe na mesma língua em que a venda consome.

    Devolve `[(coluna, id, nome, fator_inteiro)]` ou None quando o produto
    segue pela linha dele (regime anterior):
    - não é cesta (produto simples);
    - algum componente sem vínculo (órfão) — explodir só parte sumiria com
      o resto em silêncio;
    - algum fator não inteiro ou <= 0 (ex.: "Molho Pesto 600ml" = 0,6 de
      "Molho pesto"): os saldos da indústria e da loja são inteiros e
      arredondar inventaria estoque. Corrigir o cadastro (componente na
      unidade de medida, ex. 600 g) faz o produto passar a explodir."""
    if produto is None or not produto.itens:
        return None
    from app.services.cestas import componentes_de_cesta
    comps = componentes_de_cesta(produto)
    if not comps or len(comps) != len(produto.itens):
        return None
    out = []
    for col, cid, nome, fator in comps:
        f = float(fator or 0)
        if f <= 0 or f != int(f):
            return None
        out.append((col, cid, nome, int(f)))
    return out


def composicao_do_item(item):
    """FONTE ÚNICA do que um item de pedido loja→indústria movimenta, por
    unidade pedida: `[(coluna, id, nome, fator)]`. Usada na saída da
    indústria, no recebimento da loja (web/QR e copilot) e na auditoria.
    `nome` None = o próprio item (a linha mostra o nome dele)."""
    if item.materia_prima_id:
        return [('materia_prima_id', item.materia_prima_id, None, 1)]
    if item.receita_id:
        return [('receita_id', item.receita_id, None, 1)]
    if item.produto_id:
        comps = componentes_explodiveis(item.produto)
        if comps:
            return comps
        return [('produto_id', item.produto_id, None, 1)]
    return []


def item_explodido(item, comps=None):
    """True quando o item é uma cesta desmontada nos componentes."""
    if not item.produto_id:
        return False
    comps = composicao_do_item(item) if comps is None else comps
    return comps != [('produto_id', item.produto_id, None, 1)]


def _ref_componente(ref, item, explodido, qtd):
    """Referência do movimento: a do pedido (o estorno e a auditoria buscam
    'Pedido #<id> →%') e, quando o item foi desmontado, de qual cesta veio."""
    if not explodido:
        return ref
    return (ref + f' · {item.nome_item} ×{int(qtd)}')[:200]


def baixar_industria_pedido(pedido, usuario_id, ref_extra=None):
    """Baixa EstoqueProducao + MP de todos os itens do pedido.

    Cesta com composição inteira (`componentes_explodiveis`) baixa os
    COMPONENTES, não a linha do produto (13/10/2026).

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
        comps = composicao_do_item(item)
        if not comps:
            # Item solto (legado, só nome) — não há linha possível.
            logger.warning('baixar_industria_pedido: item #%s do pedido #%s '
                           'sem FK (receita/produto/MP) — sem baixa', item.id,
                           pedido.id)
            continue
        ref_item = _ref_componente(ref, item, item_explodido(item, comps), qtd)
        for col, cid, _nome, fator in comps:
            if col == 'materia_prima_id':
                _baixar_mp(cid, float(qtd) * fator, ref_item, usuario_id, faltas)
                continue
            q = int(qtd) * fator
            ep = obter_linha_producao(usuario_id=usuario_id, **{col: cid})
            disp = int(ep.quantidade or 0)
            baixa = min(q, disp)
            falta = q - baixa
            ep.quantidade = disp - baixa
            if baixa > 0:
                db.session.add(MovEstoqueProducao(
                    estoque_producao_id=ep.id, tipo='saida_pedido',
                    quantidade=baixa, referencia=ref_item, usuario_id=usuario_id))
            if falta > 0:
                db.session.add(MovEstoqueProducao(
                    estoque_producao_id=ep.id, tipo='saida_pedido_sem_estoque',
                    quantidade=falta, referencia=ref_item, usuario_id=usuario_id))
                # Fica registrada para a TV do padeiro mostrar e perguntar, ao
                # lançar a produção, se ela inclui o que já saiu (05/10/2026).
                from app.services import faltas_industria
                faltas_industria.registrar(pedido, ep, falta)
                faltas.append({'item': ep.nome_item, 'pedido': q,
                               'baixado': baixa, 'faltou': falta})
    return faltas


def _baixar_mp(mp_id, qtd, ref, usuario_id, faltas):
    mp = db.session.get(MateriaPrima, mp_id)
    if mp is None:
        return
    disp = float(mp.estoque_atual or 0)
    baixa = min(float(qtd), disp)
    falta = float(qtd) - baixa
    mp.estoque_atual = disp - baixa
    ref_mp = (ref + (f' — faltaram {falta:g}' if falta > 0 else ''))[:200]
    db.session.add(MovimentacaoEstoque(
        materia_prima_id=mp.id, tipo='saida', quantidade=baixa,
        referencia=ref_mp, usuario_id=usuario_id))
    if falta > 0:
        # `mp`: matéria-prima não gera falta na TV do padeiro (só a
        # anotação na referência) — a mensagem do Enviar distingue.
        faltas.append({'item': mp.nome, 'pedido': float(qtd),
                       'baixado': baixa, 'faltou': falta, 'mp': True})


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
    from app.services.estoque_congelados import travar_linhas_producao
    linhas = {ep.id: ep for ep in travar_linhas_producao(ids=linhas_ids)}
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


def creditar_loja_recebimento(pedido, item, qtd_rec, referencia, usuario_id):
    """Soma no estoque da LOJA o que chegou de UM item (recebimento web/QR e
    copilot). Cesta explodível entra nos COMPONENTES — a venda da loja baixa
    os componentes, então o pote recebido tem de estar lá (13/10/2026).
    Grava um `entrada_pedido` por linha creditada com a referência dada."""
    from app.models import MovEstoqueLoja
    from app.services.estoque_helpers import obter_linha_loja

    qtd_rec = int(qtd_rec or 0)
    if qtd_rec <= 0:
        return
    comps = composicao_do_item(item)
    explodido = item_explodido(item, comps)
    ref = (referencia + (f' · {item.nome_item} ×{qtd_rec}' if explodido else ''))[:200]
    for col, cid, _nome, fator in comps:
        q = qtd_rec * fator
        el = obter_linha_loja(pedido.loja_id, usuario_id=usuario_id, **{col: cid})
        el.quantidade = (el.quantidade or 0) + q
        db.session.add(MovEstoqueLoja(
            estoque_loja_id=el.id, tipo='entrada_pedido', quantidade=q,
            referencia=ref, usuario_id=usuario_id))


def estornar_loja_recebimento(pedido, usuario_id):
    """Desfaz no estoque da loja o que o recebimento do pedido REALMENTE
    creditou, pelos movimentos `entrada_pedido` dele (menos estornos
    anteriores), linha a linha — espelho de `estornar_industria_pedido`.
    Cobre a cesta desmontada nos componentes sem depender do cadastro atual.
    Pedido recebido sem nenhum movimento (legado) cai no estorno nominal do
    item, como sempre foi. Saldo da loja nunca fica negativo."""
    from app.models import EstoqueLoja, MovEstoqueLoja

    ref_estorno = f'Estorno pedido #{pedido.id} (voltar status)'
    entradas = dict(db.session.query(
        MovEstoqueLoja.estoque_loja_id, func.sum(MovEstoqueLoja.quantidade))
        .join(EstoqueLoja, EstoqueLoja.id == MovEstoqueLoja.estoque_loja_id)
        .filter(EstoqueLoja.loja_id == pedido.loja_id,
                MovEstoqueLoja.tipo == 'entrada_pedido',
                db.or_(MovEstoqueLoja.referencia == f'Pedido #{pedido.id}',
                       MovEstoqueLoja.referencia.like(f'Pedido #{pedido.id} %')))
        .group_by(MovEstoqueLoja.estoque_loja_id).all())
    if not entradas:
        for item in pedido.itens:
            qtd = item.quantidade_recebida or 0
            if qtd <= 0:
                continue
            el = EstoqueLoja.query.filter_by(
                loja_id=pedido.loja_id, receita_id=item.receita_id,
                produto_id=item.produto_id,
                materia_prima_id=item.materia_prima_id).first()
            if el:
                el.quantidade = max(0, (el.quantidade or 0) - qtd)
                db.session.add(MovEstoqueLoja(
                    estoque_loja_id=el.id, tipo='ajuste_negativo',
                    quantidade=qtd, referencia=ref_estorno,
                    usuario_id=usuario_id))
        return
    estornos = dict(db.session.query(
        MovEstoqueLoja.estoque_loja_id, func.sum(MovEstoqueLoja.quantidade))
        .filter(MovEstoqueLoja.tipo == 'ajuste_negativo',
                MovEstoqueLoja.referencia == ref_estorno)
        .group_by(MovEstoqueLoja.estoque_loja_id).all())
    for el_id in sorted(entradas):
        liquido = int(entradas[el_id] or 0) - int(estornos.get(el_id, 0) or 0)
        if liquido <= 0:
            continue
        el = db.session.get(EstoqueLoja, el_id)
        if el is None:
            continue
        el.quantidade = max(0, (el.quantidade or 0) - liquido)
        db.session.add(MovEstoqueLoja(
            estoque_loja_id=el_id, tipo='ajuste_negativo', quantidade=liquido,
            referencia=ref_estorno, usuario_id=usuario_id))
