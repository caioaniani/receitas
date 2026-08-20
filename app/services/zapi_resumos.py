"""Digest diario de tarefas via WhatsApp (Z-API).

Disparado pelo APScheduler (07:00 BRT) e on-demand via copilot tool.
"""
import logging

from flask import current_app

from app.utils import hoje

logger = logging.getLogger(__name__)


def montar_digest_tarefas(user):
    """Monta o texto do digest pra o usuario `user`.

    Inclui tarefas com prazo <= hoje (ou seja: hoje + atrasadas), status
    diferente de feito/cancelado, visiveis ao user (owner ve todas; admins
    veem so empresa).
    """
    from app.models import Projeto, ProjetoArea, TarefaProjeto

    h = hoje()
    q = (TarefaProjeto.query
         .join(Projeto)
         .join(ProjetoArea)
         .filter(TarefaProjeto.prazo.isnot(None))
         .filter(TarefaProjeto.prazo <= h)
         .filter(~TarefaProjeto.status.in_(['feito', 'cancelado'])))

    # Owner ve tudo, outros (admin/gerente) so empresa
    is_owner = bool(getattr(user, 'is_owner', False))
    if not is_owner:
        q = q.filter(ProjetoArea.tipo == 'empresa')

    tarefas = q.order_by(TarefaProjeto.prazo, TarefaProjeto.ordem).all()

    if not tarefas:
        return '*Bom dia!*\n\nNenhuma tarefa com prazo pra hoje ou atrasada. :)'

    # Separa hoje vs atrasadas
    hoje_lst = [t for t in tarefas if t.prazo == h]
    atrasadas = [t for t in tarefas if t.prazo < h]

    linhas = [f'*Bom dia! Tarefas pra hoje ({h.strftime("%d/%m")}):*']

    if hoje_lst:
        linhas.append('')
        linhas.append(f'*HOJE ({len(hoje_lst)})*')
        for t in hoje_lst[:30]:
            proj_nome = t.projeto.nome if t.projeto else '?'
            linhas.append(f'• {t.nome} _({proj_nome})_')
        if len(hoje_lst) > 30:
            linhas.append(f'_...e mais {len(hoje_lst) - 30}_')

    if atrasadas:
        linhas.append('')
        linhas.append(f'*ATRASADAS ({len(atrasadas)})*')
        for t in atrasadas[:30]:
            proj_nome = t.projeto.nome if t.projeto else '?'
            dias_atras = (h - t.prazo).days
            linhas.append(f'• {t.nome} _({proj_nome}, {dias_atras}d atras)_')
        if len(atrasadas) > 30:
            linhas.append(f'_...e mais {len(atrasadas) - 30}_')

    return '\n'.join(linhas)


def _resolver_user_default():
    """Encontra o usuario owner pra mandar o digest (single-owner system)."""
    from app.models import Usuario
    return (Usuario.query.filter_by(is_owner=True).first()
            or Usuario.query.filter_by(papel='admin').first())


_KEY_CLAIM_DIGEST = 'digest_tarefas_dia'


def enviar_digest_tarefas(claim=True):
    """Job: monta + envia o digest pro `ZAPI_NUMERO_DESTINO`.

    `claim=True` (o cron das 07:00): no maximo 1 digest por DIA via
    `whatsapp.claim_envio` — dois schedulers vivos no mesmo minuto (deploy
    em overlap ou os 2 workers gunicorn) nao duplicam (caso real
    20/08/2026: dois "Bom dia!" as 07:00). O botao manual do /notificacoes
    passa claim=False (re-envio deliberado nunca e bloqueado). Envio falho
    devolve o claim."""
    from app.services import zapi
    from app.services.whatsapp import claim_envio, devolver_claim, notificar

    numero = (current_app.config.get('ZAPI_NUMERO_DESTINO') or '').strip()
    if not numero:
        logger.info('zapi_resumos: ZAPI_NUMERO_DESTINO nao configurado, pulando')
        return
    if not zapi.disponivel():
        logger.info('zapi_resumos: Z-API nao configurado, pulando')
        return

    user = _resolver_user_default()
    if not user:
        logger.warning('zapi_resumos: nenhum usuario owner/admin pra montar digest')
        return

    anterior = None
    if claim:
        dia_id = hoje().isoformat()
        status, anterior = claim_envio(_KEY_CLAIM_DIGEST, dia_id)
        if status != 'ok':
            logger.info('zapi_resumos: digest de %s ja enviado por outro '
                        'processo (%s) — suprimido', dia_id, status)
            return

    texto = montar_digest_tarefas(user)
    res = notificar(numero, texto, 'digest_tarefas')
    if res.get('ok'):
        logger.info('zapi_resumos: digest enviado pra %s', numero)
    else:
        if claim:
            devolver_claim(_KEY_CLAIM_DIGEST, anterior)
        logger.warning('zapi_resumos: falha ao enviar: %s', res.get('erro'))
