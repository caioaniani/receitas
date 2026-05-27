"""Alerta WhatsApp: lojas que NAO lancaram desperdicio ate as 20:10 (BRT).

Roda 1x/dia via cron 20:10 (scheduler em America/Sao_Paulo). Lista as lojas
operacionais (ativas, exceto Industria) sem nenhum registro de Desperdicio no
dia e manda pro ZAPI_NUMERO_DESTINO. So envia se houver loja faltando (sem spam).
"""
import logging

from flask import current_app

from app.models import Desperdicio, Loja
from app.utils import hoje

logger = logging.getLogger(__name__)


def lojas_sem_desperdicio(dia=None):
    """Lojas operacionais sem nenhum Desperdicio lancado no dia.

    Operacional = ativa e != 'Industria' (mesmo filtro de _lojas_operacionais).
    """
    dia = dia or hoje()
    lojas = (Loja.query
             .filter(Loja.ativa.is_(True), Loja.nome != 'Industria')
             .order_by(Loja.nome).all())
    com_lancamento = {d.loja_id for d in
                      Desperdicio.query.filter_by(data=dia).all()}
    return [lj for lj in lojas if lj.id not in com_lancamento]


def enviar_alerta_desperdicio():
    """Job: se alguma loja nao lancou desperdicio hoje, avisa no WhatsApp
    (ZAPI_NUMERO_DESTINO) listando quais. Se todas lancaram, nao envia nada."""
    from app.services import zapi

    numero = (current_app.config.get('ZAPI_NUMERO_DESTINO') or '').strip()
    if not numero:
        logger.info('desperdicio_alerta: ZAPI_NUMERO_DESTINO nao configurado, pulando')
        return
    if not zapi.disponivel():
        logger.info('desperdicio_alerta: Z-API nao configurado, pulando')
        return

    faltam = lojas_sem_desperdicio()
    if not faltam:
        logger.info('desperdicio_alerta: todas as lojas lancaram, nada a enviar')
        return

    nomes = '\n'.join(f'• {lj.nome}' for lj in faltam)
    texto = ('⚠ *Desperdício não lançado*\n'
             'Estas lojas ainda não enviaram o desperdício de hoje (até 20:10):\n'
             + nomes)
    res = zapi.enviar_texto(numero, texto)
    if res.get('ok'):
        logger.info('desperdicio_alerta: enviado pra %s (%d loja[s] faltando)',
                    numero, len(faltam))
    else:
        logger.warning('desperdicio_alerta: falha ao enviar: %s', res.get('erro'))
