"""Alerta de lojas que NAO lancaram desperdicio: escalada Slack -> WhatsApp.

Fluxo (cron em America/Sao_Paulo):
- 20:10 / 20:15 / 20:20 / 20:25 BRT: posta lista de pendentes no canal
  Slack `SLACK_CANAL_COPILOT` (gerentes veem la e podem lancar antes do
  proximo tick — cada job re-consulta o banco, lojas que lancarem somem
  do proximo lembrete).
- 20:30 BRT: se ainda houver pendentes, escala via WhatsApp pro dono
  (ZAPI_NUMERO_DESTINO) — comportamento equivalente ao alerta unico
  anterior, so movido pra 20:30.

So envia se houver loja faltando (sem spam).
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


def mensagem_pendentes(lojas):
    """Monta o texto do alerta a partir da lista de lojas pendentes."""
    nomes = '\n'.join(f'• {lj.nome}' for lj in lojas)
    return ('⚠ *Desperdício não lançado*\n'
            'Estas lojas ainda não enviaram o desperdício de hoje:\n'
            + nomes)


def alertar_slack_pendentes(dia=None):
    """Posta no canal Slack `SLACK_CANAL_COPILOT` a lista de lojas que ainda
    nao lancaram desperdicio. So envia se houver pendentes; se o canal nao
    estiver configurado, loga warning e pula. Retorna dict com status."""
    from app.services import slack

    faltam = lojas_sem_desperdicio(dia)
    if not faltam:
        logger.info('desperdicio_alerta(slack): sem pendencias, nada a enviar')
        return {'enviado': False, 'motivo': 'sem_pendencias'}

    canal = (current_app.config.get('SLACK_CANAL_COPILOT') or '').strip()
    if not canal:
        logger.warning('desperdicio_alerta(slack): SLACK_CANAL_COPILOT nao '
                       'configurado, pulando (%d loja[s] pendente[s])', len(faltam))
        return {'enviado': False, 'motivo': 'sem_canal_configurado',
                'pendentes': len(faltam)}

    texto = mensagem_pendentes(faltam)
    res = slack.post_message(canal, texto)
    if res.get('ok'):
        logger.info('desperdicio_alerta(slack): enviado pro canal %s (%d loja[s])',
                    canal, len(faltam))
        return {'enviado': True, 'pendentes': len(faltam)}
    logger.warning('desperdicio_alerta(slack): falha ao enviar: %s', res.get('erro'))
    return {'enviado': False, 'motivo': 'erro_envio', 'erro': res.get('erro'),
            'pendentes': len(faltam)}


def enviar_alerta_desperdicio():
    """Job WhatsApp: se alguma loja nao lancou desperdicio hoje, avisa o dono
    (ZAPI_NUMERO_DESTINO). So envia se houver pendentes."""
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

    texto = mensagem_pendentes(faltam)
    res = zapi.enviar_texto(numero, texto)
    if res.get('ok'):
        logger.info('desperdicio_alerta: enviado pra %s (%d loja[s] faltando)',
                    numero, len(faltam))
    else:
        logger.warning('desperdicio_alerta: falha ao enviar: %s', res.get('erro'))
