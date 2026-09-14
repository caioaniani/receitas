"""Confirmação integral de Pix/transferência recebida fora do site, owner-only."""
import logging
import re
from decimal import Decimal

from app.extensions import db
from app.models import PagamentoExternoOnline, PagamentoOnline, Usuario
from app.services import loja_pagamento

logger = logging.getLogger(__name__)


def pode_confirmar(pedido):
    """A expiração do QR não invalida dinheiro recebido diretamente na conta."""
    from app.services.compra_kits import grupo_do_pedido, pedidos_do_grupo
    compra = grupo_do_pedido(pedido)
    if compra:
        return not compra.pago_em and all(_pedido_permite(p) for p in pedidos_do_grupo(compra))
    return _pedido_permite(pedido)


def _pedido_permite(pedido):
    return bool(
        not pedido.divulgacao and not pedido.pago_em
        and (pedido.status == 'aguardando_pagamento'
             or (pedido.status == 'cancelado'
                 and pedido.motivo_cancelamento == 'pix_expirado'))
    )


def _valor_decimal(raw):
    # Mesmo formato BR do parse_float_br, preservando os centavos em Decimal.
    # Recusa expoentes, NaN, infinitos e mais de duas casas; não arredonda dinheiro.
    texto = str(raw or '').strip()
    if len(texto) > 24 or not re.fullmatch(
            r'(?:[0-9]+(?:[.,][0-9]{1,2})?|[0-9]{1,3}(?:\.[0-9]{3})+,[0-9]{1,2})', texto):
        raise ValueError('Valor inválido')
    if ',' in texto:
        texto = texto.replace('.', '').replace(',', '.')
    return Decimal(texto)


def confirmar_recebimento(pedido, *, usuario_id, referencia, valor_recebido, confirmado):
    """Grava pagamento, auditoria e baixa/reserva em uma única transação.

    O lock do pedido é compartilhado com webhook e expiração. O registro único
    protege contra duplo clique; e-mail/NF só saem após persistir o recebimento.
    Nenhuma chamada ao gateway cria, paga ou estorna uma cobrança nesta ação.
    """
    usuario = db.session.get(Usuario, usuario_id) if usuario_id else None
    if not usuario or not usuario.is_owner or usuario.somente_treino:
        return False, 'Somente o owner pode confirmar um pagamento recebido fora do site.'
    if confirmado is not True:
        return False, 'Confirme que conferiu o recebimento do dinheiro na conta.'
    referencia = str(referencia or '').strip()
    if not referencia or len(referencia) > 200:
        return False, 'Informe uma referência do recebimento com até 200 caracteres.'
    try:
        valor = _valor_decimal(valor_recebido)
    except ValueError:
        return False, 'Informe um valor válido, como 150,00.'

    try:
        from app.services.compra_kits import valor_cobranca
        from app.services.kits_pagamento import travar
        compra, _ = travar(pedido, todos=True)
        if compra:
            pedido = compra.pedido_principal
        if db.session.get(PagamentoExternoOnline, pedido.id):
            return True, 'O pagamento externo deste pedido já foi confirmado.'
        if not pode_confirmar(pedido):
            return False, 'Este pedido não permite confirmação de pagamento externo.'
        if any(p.status == 'pago' for p in pedido.pagamentos):
            return False, 'Este pedido já tem um pagamento confirmado. Confira o histórico.'
        if valor <= 0 or valor != valor_cobranca(pedido):
            return False, 'O valor recebido deve ser exatamente o total da compra, incluindo todas as entregas.'

        mudou = loja_pagamento._marcar_pago(
            pedido, None, enviar_confirmacao=False, usuario_id=usuario_id)
        if not mudou:
            return False, 'Este pedido já teve seu pagamento processado.'
        pedido.cancelado_em = None
        pedido.motivo_cancelamento = None
        pagamento = PagamentoOnline(
            pedido_id=pedido.id, metodo='externo', status='pago',
            valor=valor, pago_em=pedido.pago_em)
        db.session.add(pagamento)
        db.session.flush()
        db.session.add(PagamentoExternoOnline(
            pedido_id=pedido.id, pagamento_id=pagamento.id,
            usuario_id=usuario_id, confirmado_em=pedido.pago_em,
            referencia=referencia, valor=valor))
        db.session.commit()
    except Exception:  # noqa: BLE001 — falha não pode deixar baixa sem confirmação
        db.session.rollback()
        logger.exception('Falha ao confirmar pagamento externo do pedido %s', pedido.id)
        return False, 'Não foi possível salvar a confirmação. Confira o pedido e tente novamente.'

    from app.services.kits_pagamento import apos_confirmacao
    apos_confirmacao(pedido)
    return True, 'Pagamento recebido fora do site confirmado. Pedido liberado.'
