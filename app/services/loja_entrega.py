"""Avanço de status de ENTREGA do pedido da loja própria (PedidoOnline).

Centraliza a transição `a_caminho` / `entregue`, disparada por 3 lugares:
- painel `/entregas` (clique "entregue")           → entregue
- chamar Lalamove (motorista a caminho)            → a_caminho
- webhook do Lalamove (corrida COMPLETED)          → entregue

Confirmação e baixa são transacionais; só o e-mail é best-effort:
1. avança `PedidoOnline.status` (sem regredir; idempotente em reentrega);
2. quando entregue, reflete no painel (`PainelPedidoStatus`) pra o card ir
   pra coluna de entregues;
3. dispara o e-mail transacional certo (a caminho leva o link de rastreio).

VNDA não tem `PedidoOnline` → a função é no-op silencioso (o webhook do
Lalamove chama pra todo mundo).
"""
import logging

from app.extensions import db

logger = logging.getLogger(__name__)

# Ordem do fluxo — usado pra NÃO regredir status (entregue não volta a caminho).
_ORDEM = {'pago': 1, 'em_preparo': 2, 'a_caminho': 3, 'entregue': 4}


def avancar_status_entrega(codigo, novo_status, rastreio_url=None):
    """Avança o PedidoOnline `codigo` pra `novo_status` ∈ {a_caminho, entregue}.
    `rastreio_url`: link de rastreio (Lalamove share_link) pro e-mail."""
    if novo_status not in ('a_caminho', 'entregue'):
        return
    # Entrega confirma que os itens já saíram. Falha técnica na baixa precisa
    # ser retentável, portanto não pode ser engolida pelo bloco de e-mail.
    if novo_status == 'entregue':
        from app.services.saida_producao_site import registrar_por_codigo
        registrar_por_codigo(codigo, 'entrega_confirmada')
    try:
        from app.models import PainelPedidoStatus, PedidoOnline
        from app.utils import hoje
        p = PedidoOnline.query.filter_by(codigo=codigo).first()
        if not p or p.status in ('cancelado', 'aguardando_pagamento'):
            return  # não existe (ex: VNDA) ou não faz sentido avançar
        if _ORDEM.get(novo_status, 0) <= _ORDEM.get(p.status, 0):
            return  # não regride (idempotente em reentrega de webhook)
        p.status = novo_status
        if novo_status == 'entregue':
            # Reflete no painel pra o card mover pra coluna de "entregues".
            s = PainelPedidoStatus.query.filter_by(pedido_code=codigo).first()
            if s:
                s.status = 'entregue'
            else:
                db.session.add(PainelPedidoStatus(
                    pedido_code=codigo, status='entregue', data_ref=hoje()))
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise
    try:
        from app.services import email as email_svc
        if email_svc.disponivel():
            if novo_status == 'a_caminho':
                email_svc.enviar_pedido_a_caminho(p, rastreio_url=rastreio_url)
            else:
                email_svc.enviar_pedido_entregue(p)
    except Exception:  # noqa: BLE001
        logger.exception('email de avancar_status_entrega %s -> %s falhou',
                         codigo, novo_status)
