"""Baixa física de compras sob encomenda ao sair para o cliente.

Não commita. Débito, snapshot e confirmação de saída pertencem à mesma
transação. Pagamento/reserva de loja e brindes continuam nos seus fluxos.
"""
import json
import logging
from decimal import Decimal

from sqlalchemy import text

from app.extensions import db
from app.models import (
    MateriaPrima,
    MovEstoqueProducao,
    MovimentacaoEstoque,
    PedidoOnline,
    PedidoOnlineItem,
    SaidaProducaoSite,
)

logger = logging.getLogger(__name__)


def itens_baixados(pedido_id):
    return {item_id for (item_id,) in db.session.query(SaidaProducaoSite.pedido_item_id)
            .filter_by(pedido_id=pedido_id).all()}


def ja_saiu(pedido_id):
    return SaidaProducaoSite.query.filter_by(pedido_id=pedido_id).first() is not None


def composicao_item(item, *, estrita=False):
    """Composição física por unidade: snapshot do menu, cadastro da cesta ou item simples."""
    from app.services.cestas import composicao_de_venda
    from app.services.loja_estoque_reserva import composicao_escolhida

    escolhida = composicao_escolhida(item)
    if estrita:
        # Não converter um menu com snapshot incompleto na pré-seleção atual.
        if item.componentes:
            if not escolhida or len(escolhida) != len(item.componentes):
                raise ValueError(f'{item.nome}: composição escolhida sem vínculo de estoque válido.')
        elif item.produto and item.produto.menu_configuravel:
            raise ValueError(f'{item.nome}: composição escolhida não foi registrada.')
        elif item.produto and item.produto.itens:
            from app.services.cestas import componentes_de_cesta
            componentes = componentes_de_cesta(item.produto)
            if len(componentes) != len(item.produto.itens):
                raise ValueError(f'{item.nome}: cesta com componentes sem vínculo de estoque.')
            return componentes
    return escolhida or composicao_de_venda(
        receita_id=item.receita_id, produto_id=item.produto_id)


def registrar_por_codigo(codigo, origem, usuario_id=None):
    pedido = PedidoOnline.query.filter_by(codigo=codigo).first()
    if pedido is None:
        return []  # código VNDA/externo
    return registrar(pedido, origem, usuario_id)


def registrar(pedido, origem, usuario_id=None):
    """Registra uma vez os itens sob encomenda. Retorna faltas para auditoria.

    Pedido entregue antigo não é reprocessado por um webhook repetido.
    O acerto manual do dia compartilha o lock e exclui os itens aqui gravados.
    """
    from app.services.acerto_despacho import _LOCK_ACERTO, _codigos_acertados
    from app.services.estoque_congelados import obter_linha_producao
    from app.services.loja_estoque_reserva import item_sob_encomenda

    # Ordem compartilhada com acerto manual: global -> pedido -> estoque.
    with db.session.no_autoflush:
        if db.engine.dialect.name == 'postgresql':
            db.session.execute(text('SELECT pg_advisory_xact_lock(:k)'), {'k': _LOCK_ACERTO})
        db.session.refresh(pedido, with_for_update=True)
    if pedido.status not in ('pago', 'em_preparo', 'a_caminho') or pedido.divulgacao:
        return []
    if pedido.data_entrega and pedido.codigo in _codigos_acertados(pedido.data_entrega):
        return []
    feitos = itens_baixados(pedido.id)
    planos = []
    # Recarrega também quantidades/snapshot que possam ter sido lidos antes
    # de aguardar a trava (ex.: redução administrativa concorrente).
    itens = (PedidoOnlineItem.query.filter_by(pedido_id=pedido.id)
             .populate_existing().order_by(PedidoOnlineItem.id).all())
    for item in itens:
        if item.id in feitos or not item_sob_encomenda(item) or (item.quantidade or 0) <= 0:
            continue
        componentes = composicao_item(item, estrita=True)
        if not componentes:
            raise ValueError(f'Pedido {pedido.codigo}: {item.nome} sem composição de estoque vinculada.')
        linhas = []
        for col, cid, nome, por_unidade in componentes:
            qtd = Decimal(str(item.quantidade)) * Decimal(str(por_unidade))
            if qtd <= 0:
                continue
            if col != 'materia_prima_id' and qtd != qtd.to_integral_value():
                raise ValueError(f'Pedido {pedido.codigo}: {nome} possui quantidade fracionária na indústria.')
            linhas.append((col, cid, nome, qtd))
        if not linhas:
            raise ValueError(f'Pedido {pedido.codigo}: {item.nome} sem componentes para baixar.')
        planos.append((item, linhas))

    faltas = []
    # Ordem única entre pedidos com vários componentes; saldo relido sob FOR UPDATE.
    # Produção registra primeiro EstoqueProducao e depois consome MP da ficha;
    # usar essa mesma ordem evita inverter as travas ao despachar uma cesta.
    linhas_estoque = {}
    chaves = {(col, cid) for _, linhas in planos for col, cid, _, _ in linhas}
    for col, cid in sorted(chaves, key=lambda k: (k[0] == 'materia_prima_id', *k)):
        if col == 'materia_prima_id':
            alvo = (MateriaPrima.query.filter_by(id=cid).with_for_update().populate_existing().one())
        else:
            alvo = obter_linha_producao(**{col: cid}, usuario_id=usuario_id)
        linhas_estoque[(col, cid)] = alvo

    for item, linhas in planos:
        snapshot = []
        for col, cid, nome, qtd in linhas:
            alvo = linhas_estoque[(col, cid)]
            ref = f'Site #{pedido.codigo} item #{item.id} — saída da produção ({origem})'
            if col == 'materia_prima_id':
                disp = max(Decimal('0'), Decimal(str(alvo.estoque_atual or 0)))
            else:
                disp = max(Decimal('0'), Decimal(int(alvo.quantidade or 0)))
            baixa = min(qtd, disp)
            falta = qtd - baixa
            if col == 'materia_prima_id':
                alvo.estoque_atual = float(disp - baixa)
                db.session.add(MovimentacaoEstoque(
                    materia_prima_id=cid, tipo='saida', quantidade=float(baixa),
                    referencia=ref + (f' — faltaram {falta}' if falta else ''), usuario_id=usuario_id))
            else:
                alvo.quantidade = int(disp - baixa)
                if baixa:
                    db.session.add(MovEstoqueProducao(
                        estoque_producao_id=alvo.id, tipo='saida_site_direto',
                        quantidade=int(baixa), referencia=ref, usuario_id=usuario_id))
                if falta:
                    db.session.add(MovEstoqueProducao(
                        estoque_producao_id=alvo.id, tipo='saida_site_direto_sem_estoque',
                        quantidade=int(falta), referencia=ref, usuario_id=usuario_id))
            detalhe = {'coluna': col, 'id': cid, 'nome': nome, 'quantidade': str(qtd),
                       'baixado': str(baixa), 'faltou': str(falta)}
            snapshot.append(detalhe)
            if falta:
                faltas.append(detalhe)
                logger.warning('Saída site %s: faltaram %s de %s no saldo da indústria; '
                               'divergência registrada, saldo não ficou negativo.',
                               pedido.codigo, falta, nome)
        db.session.add(SaidaProducaoSite(
            pedido_item_id=item.id, pedido_id=pedido.id, origem=origem,
            usuario_id=usuario_id, componentes_json=json.dumps(snapshot, ensure_ascii=False)))
    db.session.flush()
    return faltas
