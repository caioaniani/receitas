"""Compra única de um kit por data escolhida pelo cliente."""
import logging
import re
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy.exc import SQLAlchemyError

from app.extensions import db
from app.models import CompraKit, EntregaKit
from app.utils import agora

logger = logging.getLogger(__name__)
DIAS_AGENDA_KITS = 31


def grupo_do_pedido(pedido):
    pedido_id = getattr(pedido, 'id', None)
    if pedido_id is None:
        return None
    entrega = db.session.get(EntregaKit, pedido_id)
    return entrega.compra if entrega else None


def pedidos_do_grupo(compra):
    return [e.pedido for e in compra.entregas]


def reler_entregas(compra):
    """Relê marcadores após os locks dos pedidos; identity map pode ser anterior à espera."""
    return (EntregaKit.query.filter_by(compra_id=compra.id)
            .order_by(EntregaKit.ordem).populate_existing().all())


def principal_do_pedido(pedido):
    compra = grupo_do_pedido(pedido)
    return compra.pedido_principal if compra else pedido


def valor_cobranca(pedido):
    compra = grupo_do_pedido(pedido)
    return compra.valor_total if compra else pedido.valor_total


def _assinatura_itens(itens):
    return [(it.kind, it.receita_id, it.produto_id, it.nome,
             it.quantidade, it.preco_unitario, it.subtotal, bool(it.fatiado),
             sorted((c.produto_item_id, c.tipo, c.alvo_id, c.nome,
                     c.quantidade, c.preco_unitario) for c in it.componentes))
            for it in itens]


def _snapshot_composicao(itens):
    """Captura preço/composição ANTES da cotação de frete e valida todas as datas."""
    from app.models import PedidoOnlineItem
    from app.services.loja_checkout import _persistir_composicao_menu
    modelos = []
    for item in itens:
        modelo = PedidoOnlineItem(kind=item['kind'], receita_id=item['receita_id'],
                                   produto_id=item['produto_id'], nome=item['nome'],
                                   quantidade=item['qtd'], preco_unitario=item['preco'],
                                   subtotal=item['subtotal'], fatiado=item.get('fatiado'))
        if item.get('comp'):
            _persistir_composicao_menu(modelo, item['produto_id'], item['comp'])
        modelos.append(modelo)
    return _assinatura_itens(modelos)


def validar_agenda(agenda):
    if not isinstance(agenda, list) or not 1 <= len(agenda) <= DIAS_AGENDA_KITS:
        return [], ['Escolha entre 1 e 31 datas de entrega. Cada data corresponde a um kit.']
    datas = set()
    out = []
    for item in agenda:
        if not isinstance(item, dict):
            return [], ['Revise as datas e os horários das entregas.']
        raw = item.get('data')
        janela = item.get('janela')
        try:
            dia = date.fromisoformat(raw)
        except (ValueError, TypeError):
            return [], ['Informe uma data válida para cada entrega.']
        if dia.isoformat() != raw or not isinstance(janela, str) or not janela or len(janela) > 40:
            return [], ['Escolha o horário de cada entrega.']
        if dia in datas:
            return [], ['Escolha cada data uma única vez: cada dia corresponde a um kit.']
        datas.add(dia)
        out.append({'data': raw, 'janela': janela})
    return sorted(out, key=lambda item: item['data']), []


def criar_compra(kit, form, agenda, *, checkout_token, base=None):
    """Reutiliza as validações do checkout para cada data sem commits parciais.

    O nonce vem da sessão assinada; a rota confere sua origem. A unicidade e
    o lock do kit impedem duas compras por um duplo clique do mesmo formulário.
    Os valores individuais são os únicos pedidos que entram no faturamento.
    """
    from app.services import kits_cafe, loja_checkout, loja_plano_dia

    if not re.fullmatch(r'[0-9a-f]{64}', str(checkout_token or '')):
        return None, ['Reabra o kit para iniciar uma nova compra.']
    agenda, erros = validar_agenda(agenda)
    if erros:
        return None, erros
    base = base or agora()
    try:
        db.session.refresh(kit, with_for_update=True)
        existente = CompraKit.query.filter_by(checkout_token=checkout_token).first()
        if existente:
            if existente.kit_id != kit.id:
                return None, ['Este formulário pertence a outro kit. Reabra a compra.']
            return existente, []
        if not kit.ativo:
            return None, ['Este kit não está disponível para compra.']
        itens, erros = kits_cafe.montar(kit)
        if erros or not itens:
            return None, erros or ['Este kit está indisponível.']
        snapshot = _snapshot_composicao(itens)
        raw = kits_cafe.itens_do_kit(kit)
        pedidos = []
        frete_validado = None
        for agendamento in agenda:
            dados = dict(form)
            dados.update(modo_entrega='agendada', data_entrega=agendamento['data'],
                         janela_entrega=agendamento['janela'])
            pedido, erros = loja_checkout.criar_pedido(
                dados, raw, base=base, commit=False,
                dias_agenda=DIAS_AGENDA_KITS, reservar_estoque=False,
                frete_validado=frete_validado, itens_estritos=True,
                dias_disponibilidade=DIAS_AGENDA_KITS)
            if erros:
                db.session.rollback()
                return None, [f"{agendamento['data']}: {erro}" for erro in erros]
            if _assinatura_itens(pedido.itens) != snapshot:
                db.session.rollback()
                return None, ['O preço ou a composição do kit mudou durante a compra. '
                              'Reabra o kit para conferir os itens e o total atualizados.']
            # Uma cotação por endereço. Frete agendado usa a tabela por distância,
            # não uma corrida contratada; cada entrega recebe o mesmo valor.
            frete_validado = (pedido.frete_valor, pedido.distancia_km,
                              pedido.endereco_entrega, None)
            for it in pedido.itens:
                saldo = loja_plano_dia.saldo(it.kind, it.receita_id or it.produto_id,
                                           pedido.data_entrega)
                if saldo is not None and saldo < it.quantidade:
                    db.session.rollback()
                    return None, [f'{agendamento["data"]}: quantidade de {it.nome} '
                                  'indisponível para essa data. Escolha outro dia.']
            pedidos.append(pedido)
        compra = CompraKit(
            kit_id=kit.id, kit_nome=kit.nome, pedido_principal_id=pedidos[0].id,
            subtotal=sum((p.subtotal for p in pedidos), Decimal('0.00')),
            frete_total=sum((p.frete_valor for p in pedidos), Decimal('0.00')),
            valor_total=sum((p.valor_total for p in pedidos), Decimal('0.00')),
            expira_em=base + timedelta(minutes=35), checkout_token=checkout_token)
        db.session.add(compra)
        db.session.flush()
        for ordem, pedido in enumerate(pedidos, 1):
            db.session.add(EntregaKit(compra_id=compra.id, pedido_id=pedido.id, ordem=ordem))
        db.session.flush()
        from app.services.kits_capacidade import reservar_compra
        ok, erros = reservar_compra(compra)
        if not ok:
            db.session.rollback()
            return None, erros
        db.session.commit()
        return compra, []
    except SQLAlchemyError:
        db.session.rollback()
        logger.exception('Falha ao salvar compra de kit %s', kit.id)
        return None, ['Não foi possível salvar a compra. Tente novamente. Nenhuma cobrança foi feita.']


def cancelar_pendentes(pedido):
    """Cancela uma compra ainda não paga por inteiro, sem alterar outras reservas."""
    compra = grupo_do_pedido(pedido)
    if not compra:
        return False, 'Pedido não pertence a uma compra de kits.'
    db.session.refresh(compra, with_for_update=True)
    pedidos = sorted(pedidos_do_grupo(compra), key=lambda p: p.id)
    for p in pedidos:
        db.session.refresh(p, with_for_update=True)
    reler_entregas(compra)
    if compra.pago_em or any(p.pago_em for p in pedidos):
        return False, 'A compra já foi paga. Use o reembolso da entrega desejada.'
    for p in pedidos:
        if p.status not in ('aguardando_pagamento', 'cancelado'):
            return False, 'Esta compra já avançou na operação. Confira as entregas.'
    from app.services.kits_capacidade import liberar_compra
    liberar_compra(compra)
    for p in pedidos:
        p.status = 'cancelado'
        p.motivo_cancelamento = 'cancelado_admin'
        p.cancelado_em = agora()
    db.session.commit()
    return True, 'Compra ainda não paga cancelada, incluindo todas as datas agendadas.'
