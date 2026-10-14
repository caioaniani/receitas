"""Aplicação de status de corrida Lalamove — fonte única do webhook e da
reconciliação (14/10/2026).

Caso que a criou: corridas ficaram paradas aqui em "procurando entregador"
enquanto na Lalamove já estavam a caminho/entregues (43190CD6 com DOIS
motoboys, 2F18BF49 e C64E9373 parados desde 13/10) — o aviso (webhook) da
Lalamove não chegou, e o card do painel mentia. O webhook segue sendo o
caminho principal; `reconciliar` é a rede de segurança que pergunta à
Lalamove o status das corridas vivas e aplica a MESMA regra.
"""
import logging
from datetime import timedelta

from app.extensions import db
from app.models import LalamoveEntrega
from app.utils import agora

logger = logging.getLogger(__name__)

# Corrida viva há menos disso sem aviso não é consultada (o webhook costuma
# chegar em segundos); passou disso, a conferência pergunta à Lalamove.
RECONCILIA_IDADE_MIN = 10
RECONCILIA_JANELA_DIAS = 3
RECONCILIA_LIMITE = 40


def aplicar_status(e, status, dados=None):
    """Aplica `status` (ORDER_STATUS_CHANGED da Lalamove) à corrida `e`, com
    os mesmos efeitos de sempre do webhook: coleta/entrega registram a saída
    da produção (falha recusada vira log — o fato físico já aconteceu),
    COMPLETED marca o pedido entregue e encerrada sem entrega alerta.
    Commita. Nunca levanta por regra de negócio."""
    from app.services import lalamove_alerta
    from app.services.loja_entrega import avancar_status_entrega
    from app.services.saida_producao_site import registrar_por_codigo

    status = (status or '').upper()
    if not status:
        return
    anterior = e.status
    if status in ('PICKED_UP', 'COMPLETED'):
        try:
            with db.session.begin_nested():
                registrar_por_codigo(e.pedido_code, 'lalamove_' + status.lower())
        except ValueError as exc:
            # O motoboy JÁ coletou (fato físico): registra o status e a equipe
            # confere a saída bloqueada (estorno de kit, composição sem
            # vínculo). Recusar só faria a Lalamove reenviar para sempre.
            logger.error('lalamove: saída do pedido %s não registrada (%s): %s',
                         e.pedido_code, status, exc)
    e.status = status
    e.atualizado_em = agora()
    db.session.commit()
    # ENTREGUE = corrida realmente concluída. Só no COMPLETED — nunca por
    # retirada/alocação (bug 19/06/2026).
    if status == 'COMPLETED':
        try:
            avancar_status_entrega(e.pedido_code, 'entregue')
        except ValueError as exc:
            # Caso CE639E7C (12/10/2026): saída recusada virava 500 a cada
            # reentrega com o pedido preso em "a caminho".
            db.session.rollback()
            logger.error('lalamove: pedido %s concluído mas NÃO marcado entregue '
                         '(saída recusada): %s', e.pedido_code, exc)
    # Encerrada SEM entrega: alerta ALTA + WhatsApp + conversa pra equipe.
    if status in lalamove_alerta.STATUS_SEM_ENTREGA:
        lalamove_alerta.tratar_encerramento(e, status, anterior, dados or {})


def reconciliar(limite=RECONCILIA_LIMITE):
    """Pergunta à Lalamove (GET /v3/orders/{id}) o status das corridas vivas
    aqui (procurando / a caminho / coletada) sem aviso há
    RECONCILIA_IDADE_MIN minutos e aplica o que mudou. Best-effort por
    corrida: falha na consulta mantém o gravado. Devolve as contagens."""
    from app.services import lalamove as lala_svc

    stats = {'consultadas': 0, 'corrigidas': 0, 'falhas': 0, 'mudancas': []}
    if not lala_svc.disponivel():
        stats['erro'] = 'Lalamove sem credenciais'
        return stats
    agora_ = agora()
    corte = agora_ - timedelta(minutes=RECONCILIA_IDADE_MIN)
    desde = agora_ - timedelta(days=RECONCILIA_JANELA_DIAS)
    vivas = (LalamoveEntrega.query
             .filter(LalamoveEntrega.order_id.isnot(None),
                     LalamoveEntrega.status.in_(lala_svc.STATUS_LALAMOVE_ATIVOS),
                     LalamoveEntrega.criado_em >= desde)
             .order_by(LalamoveEntrega.criado_em.asc())
             .all())
    for e in vivas:
        if stats['consultadas'] >= limite:
            break
        ultimo = e.atualizado_em or e.criado_em
        if ultimo is not None and ultimo > corte:
            continue
        stats['consultadas'] += 1
        try:
            r = lala_svc.detalhes(e.order_id)
        except Exception as exc:  # noqa: BLE001 — rede: fica o gravado
            logger.warning('lalamove reconcilia: consulta de %s falhou (%s)',
                           e.order_id, type(exc).__name__)
            stats['falhas'] += 1
            continue
        if not r.get('ok'):
            stats['falhas'] += 1
            continue
        novo = (r.get('status') or '').upper()
        if r.get('share_link') and not e.share_link:
            e.share_link = r['share_link']
        if not novo or novo == e.status:
            continue
        logger.warning('lalamove reconcilia: corrida %s do pedido %s estava %s aqui e '
                       'está %s na Lalamove (aviso não chegou) — aplicado',
                       e.order_id, e.pedido_code, e.status, novo)
        stats['mudancas'].append({'pedido': e.pedido_code, 'order_id': e.order_id,
                                  'de': e.status, 'para': novo})
        try:
            aplicar_status(e, novo, {'origem': 'reconciliacao'})
            stats['corrigidas'] += 1
        except Exception:  # noqa: BLE001 — uma corrida não derruba as outras
            db.session.rollback()
            logger.exception('lalamove reconcilia: falhou ao aplicar %s em %s',
                             novo, e.order_id)
            stats['falhas'] += 1
    try:
        db.session.commit()   # share_link preenchido sem mudança de status
    except Exception:  # noqa: BLE001
        db.session.rollback()
    return stats
