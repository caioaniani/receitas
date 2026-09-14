"""Cobrança única de kits, com efeitos financeiros por entrega real."""
import logging

from app.extensions import db
from app.models import EntregaKit
from app.services.compra_kits import grupo_do_pedido, pedidos_do_grupo, reler_entregas
from app.utils import agora

logger = logging.getLogger(__name__)


def travar(pedido, *, todos=False):
    """Sempre compra → pedidos em ID crescente; compartilha a trava com expiração."""
    compra = grupo_do_pedido(pedido)
    if compra:
        db.session.refresh(compra, with_for_update=True)
        pedidos = sorted(pedidos_do_grupo(compra), key=lambda p: p.id)
        if todos:
            for entrega in pedidos:
                db.session.refresh(entrega, with_for_update=True)
        else:
            db.session.refresh(pedido, with_for_update=True)
        reler_entregas(compra)
        return compra, pedidos
    db.session.refresh(pedido, with_for_update=True)
    return None, [pedido]


def preparar_cobranca(pedido):
    """Normaliza o destinatário financeiro e valida todo o grupo sob trava."""
    compra, pedidos = travar(pedido, todos=True)
    principal = compra.pedido_principal if compra else pedido
    if ((compra and (compra.pago_em or compra.expira_em <= agora()))
            or any(p.status != 'aguardando_pagamento' or p.pago_em for p in pedidos)):
        return principal, False
    return principal, True


def marcar_pago(pedido, pagamento, *, enviar_confirmacao=True, usuario_id=None):
    """Persiste todas as entregas juntas, sem consumir estoque físico antecipado."""
    from app.services import loja_pagamento

    compra, pedidos = travar(pedido, todos=True)
    if not compra:
        raise ValueError('Pedido sem compra de kit')
    if pagamento:
        db.session.refresh(pagamento)
        if pagamento.status != 'estornado':
            pagamento.status = 'pago'
            pagamento.pago_em = pagamento.pago_em or agora()
    if compra.pago_em:
        if loja_pagamento._tem_pagamento_externo(compra.pedido_principal):
            logger.warning('Kit %s: gateway reportou pagamento após recebimento externo; '
                           'conferir possível duplicidade.', compra.id)
        return False
    # Cancelamento deliberado não pode ser revertido por um webhook atrasado.
    # QR expirado é diferente: o gateway ainda pode confirmar dinheiro recebido.
    if any(p.pago_em or p.divulgacao or p.status not in ('aguardando_pagamento', 'cancelado')
           or (p.status == 'cancelado' and p.motivo_cancelamento != 'pix_expirado')
           for p in pedidos):
        logger.error('Pagamento recebido para kit %s com entrega incompatível; '
                     'requer conferência do owner.', compra.id)
        return False
    from app.services.kits_capacidade import reservar_compra
    reservar_compra(compra, pagamento_recebido=True)
    from app.services.loja_fiscal import agendar
    instante = agora()
    for entrega in pedidos:
        entrega.status = 'pago'
        entrega.pago_em = instante
        entrega.cancelado_em = None
        entrega.motivo_cancelamento = None
        entrega.reserva_expira_em = None
        agendar(entrega, base=instante)
    compra.pago_em = instante
    if enviar_confirmacao:
        loja_pagamento._enviar_confirmacao(compra.pedido_principal)
    return True


def apos_confirmacao(pedido):
    """Uma confirmação; NF e Purchase por entrega, com seus valores individuais."""
    from app.services import loja_pagamento

    compra = grupo_do_pedido(pedido)
    principal = compra.pedido_principal if compra else pedido
    pedidos = pedidos_do_grupo(compra) if compra else [pedido]
    loja_pagamento._enviar_confirmacao(principal)
    for entrega in pedidos:
        # Mesmo transaction_id do acompanhamento individual no navegador;
        # somar os eventos representa exatamente o total pago pelo grupo.
        loja_pagamento._reportar_purchase(entrega)


def cancelar_entrega_paga(pedido):
    """Efeito LOCAL após refund confirmado. Não presume retorno físico de produtos."""
    from app.services.kits_capacidade import liberar_entregas

    registro = db.session.get(EntregaKit, pedido.id)
    if not registro:
        raise ValueError('Pedido sem entrega de kit')
    pedido.status = 'cancelado'
    pedido.motivo_cancelamento = 'reembolso'
    pedido.cancelado_em = agora()
    liberar_entregas([registro])


def reembolsar_entrega(pedido):
    """Serializa o reembolso com emissão/envio da NF desta entrega."""
    from app.services.tiny_nf import _trava_nf_kit
    with _trava_nf_kit(pedido.id) as adquirido:
        if not adquirido:
            return False, 'A nota fiscal desta entrega está sendo processada. Aguarde e tente novamente.'
        return _reembolsar_entrega(pedido)


def _reembolsar_entrega(pedido):
    """Refund parcial da cobrança comum, restrito ao valor desta entrega.

    A API documenta idempotência de criação de pedidos, não do DELETE de
    cobrança. Por isso a intenção é persistida ANTES da rede: se a resposta
    for incerta ou o processo cair, outro clique não devolve dinheiro de novo.
    Confirmado é persistido antes dos efeitos locais para permitir recuperação
    de falha de banco sem repetir a chamada financeira.
    """
    from app.models.kits_cafe import ReembolsoKit
    from app.services import loja_pagamento, pagarme

    try:
        compra, pedidos = travar(pedido, todos=True)
        if not compra:
            return False, 'Pedido não pertence a uma compra de kit.'
        if loja_pagamento._tem_pagamento_externo(pedido):
            return False, ('Este pagamento foi recebido fora do site. O Pagar.me '
                           'não pode devolver esse valor; o estorno automático não está disponível.')
        if pedido.status == 'cancelado':
            return True, 'Esta entrega já estava cancelada.'
        if not compra.pago_em or not pedido.pago_em:
            return False, 'Esta entrega ainda não teve pagamento confirmado.'
        pago = next((p for p in compra.pedido_principal.pagamentos
                     if p.status == 'pago' and p.pagarme_charge_id), None)
        if not pago:
            return False, 'Compra sem cobrança paga no Pagar.me; confira o pagamento.'
        registro = db.session.get(ReembolsoKit, pedido.id)
        if registro and registro.status == 'solicitado':
            return False, ('Já existe uma solicitação de estorno desta entrega sem '
                           'conclusão confirmada. Confira no Pagar.me antes de qualquer novo estorno.')
        if not registro or registro.status == 'recusado':
            if not registro:
                registro = ReembolsoKit(pedido_id=pedido.id, valor=pedido.valor_total,
                                        pagarme_charge_id=pago.pagarme_charge_id)
                db.session.add(registro)
            registro.status = 'solicitado'
            registro.erro = None
            registro.criado_em = agora()
            db.session.commit()
            # A coleta vê o registro solicitado e aguarda a resolução. Um
            # segundo clique encontra o mesmo registro e nunca chama a API.
            resultado = pagarme.cancelar_charge(
                registro.pagarme_charge_id, valor_decimal=registro.valor, detalhar=True)
            compra, pedidos = travar(pedido, todos=True)
            db.session.refresh(registro, with_for_update=True)
            if not resultado.get('ok'):
                registro.erro = str(resultado.get('erro') or 'Resposta sem confirmação')[:2000]
                # Sem indicação explícita de recusa, assume que dinheiro pode
                # ter saído: não transforma timeout/erro desconhecido em retry.
                if resultado.get('incerto') is False:
                    registro.status = 'recusado'
                db.session.commit()
                return False, (f'Estorno desta entrega não confirmado: {registro.erro}. '
                               'As outras entregas continuam ativas.')
            registro.status = 'confirmado'
            registro.confirmado_em = agora()
            db.session.commit()
            compra, pedidos = travar(pedido, todos=True)
            db.session.refresh(registro, with_for_update=True)
        if registro.status != 'confirmado':
            return False, 'Estorno pendente de conferência.'
        # Outro worker pode concluir os efeitos locais entre o commit do
        # comprovante financeiro e a reaquisição das travas. Relê sob lock;
        # repetir a devolução do plano diminuiria a reserva de outro cliente.
        if pedido.status == 'cancelado':
            return True, 'Esta entrega já estava cancelada.'
        cancelar_entrega_paga(pedido)
        # A cobrança representa o mês. Cancelar a primeira entrega não pode
        # torná-la estornada enquanto há outras entregas pagas ainda ativas.
        if all(p.status == 'cancelado' for p in pedidos):
            pago.status = 'estornado'
        db.session.commit()
    except Exception:  # noqa: BLE001 — dinheiro não pode ser reenviado após erro
        db.session.rollback()
        logger.exception('Falha no reembolso da entrega de kit %s', pedido.id)
        return False, ('Não foi possível concluir o registro do estorno. Confira o pedido; '
                       'uma solicitação já enviada não será repetida automaticamente.')

    try:
        from app.services import email as email_svc
        if email_svc.disponivel():
            email_svc.enviar_reembolso_confirmado(pedido, valor=registro.valor, metodo=pago.metodo)
    except Exception:  # noqa: BLE001 — e-mail não desfaz dinheiro persistido
        logger.exception('Falha no comprovante de reembolso da entrega %s', pedido.id)
    return True, ('Esta entrega foi reembolsada e cancelada. As demais datas permanecem '
                  'como estavam. Confira a NF desta entrega no Tiny, se já foi emitida.')
