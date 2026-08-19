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

ANTI-DUPLICATA (19/08/2026, dono: "Continua duplicando"): num deploy, o
container VELHO e o NOVO ficam vivos ao mesmo tempo por alguns minutos e os
DOIS disparam o cron do minuto — o advisory lock do seru_cron so serializa
execucoes SIMULTANEAS; a segunda, segundos depois, pega o lock livre e
reenvia (caso real: push as 20:2x BRT, container novo bootou 20:26 e o dono
levou DUAS mensagens de sobras as 20:30 — com conteudos diferentes, porque
um gerente lancou entre os dois envios). Fix: claim persistente em AppConfig
COMMITADO ANTES do envio (`_claim_envio`) — WhatsApp do dono e 1x/DIA, cada
tick do Slack e 1x/MINUTO; envio que falha DEVOLVE o claim (o lembrete nao
se perde por Slack/Z-API fora). Janela residual aceita: kill entre o claim e
o envio perde 1 lembrete do dia (volta amanha) — aqui duplicar e pior que
perder, direcao OPOSTA a dos vigias de fraude.
"""
import logging

from flask import current_app

from app.models import Desperdicio, Loja
from app.utils import hoje

logger = logging.getLogger(__name__)

# Cap de itens NOMINAIS por loja na mensagem (alerta gigante vira ruido que
# ensina a ignorar — mesmo padrao do vigia de venda sem item).
_MAX_ITENS_POR_LOJA = 8


def lojas_sem_desperdicio(dia=None):
    """Lojas operacionais sem nenhum Desperdicio lancado no dia.

    Operacional = ativa e != 'Industria' (mesmo filtro de _lojas_operacionais)
    E que ABRE nesse dia (`Loja.funciona_em`). Loja fechada nao tem sobra pra
    lancar — cobra-la e ruido que ensina a ignorar o alerta. Decisao do dono
    27/07/2026: "Cantina nao precisa lancar sobras durante a semana pois so
    funciona de sabado e domingo". Loja SEM dias configurados continua sendo
    cobrada todo dia (fail-open — ver Loja.funciona_em).
    """
    dia = dia or hoje()
    lojas = (Loja.query
             .filter(Loja.ativa.is_(True), Loja.nome != 'Industria')
             .order_by(Loja.nome).all())
    com_lancamento = {d.loja_id for d in
                      Desperdicio.query.filter_by(data=dia).all()}
    return [lj for lj in lojas
            if lj.id not in com_lancamento and lj.funciona_em(dia)]


def itens_sem_sobra(dia=None):
    """Cobrança POR ITEM (01/08/2026, dono: "o pessoal não tem lançado sobra
    do croissant tradicional, precisamos atacar isso").

    Por que existe: o alerta por loja pergunta só "lançou ALGO hoje?" —
    lançar a sobra de UM item calava a cobrança de todos os outros. A
    conferência de 29-31/07 provou o custo: Pão Francês na Ribeiro com
    1.050 recebidos, 558 vendidos e ZERO sobra lançada em 14 dias (rombo de
    ~492 un que só apareceu na contagem física).

    Regra: receita com `cobra_sobra_diaria` (checkbox na ficha; seed = os
    itens que o dono ajustou na conferência) + saldo > 0 no EstoqueLoja da
    loja + NENHUM Desperdicio da receita naquela loja no dia. Só lojas
    operacionais que funcionam no dia (mesma régua de
    `lojas_sem_desperdicio`). Receita arquivada fica fora.

    Retorna [(loja, [(nome_receita, saldo), ...])], lojas por nome e itens
    por saldo desc. O saldo vai na mensagem de propósito: se a loja vendeu
    tudo e o sistema ainda mostra saldo, o gesto certo é conferir o
    estoque — a cobrança aponta divergência, não só esquecimento.
    """
    from sqlalchemy import func

    from app.extensions import db
    from app.models import EstoqueLoja, Receita

    dia = dia or hoje()
    rows = (db.session.query(Loja, Receita,
                             func.sum(EstoqueLoja.quantidade))
            .join(EstoqueLoja, EstoqueLoja.loja_id == Loja.id)
            .join(Receita, EstoqueLoja.receita_id == Receita.id)
            .filter(Loja.ativa.is_(True), Loja.nome != 'Industria',
                    Receita.cobra_sobra_diaria.is_(True),
                    Receita.arquivada_em.is_(None))
            .group_by(Loja.id, Receita.id)
            .having(func.sum(EstoqueLoja.quantidade) > 0)
            .all())
    if not rows:
        return []
    lancados = {(d.loja_id, d.receita_id)
                for d in Desperdicio.query.filter_by(data=dia).all()
                if d.receita_id}
    por_loja = {}
    for loja, rec, saldo in rows:
        if not loja.funciona_em(dia):
            continue
        if (loja.id, rec.id) in lancados:
            continue
        por_loja.setdefault(loja, []).append((rec.nome, int(saldo or 0)))
    out = []
    for loja in sorted(por_loja, key=lambda lj: lj.nome):
        itens = sorted(por_loja[loja], key=lambda x: (-x[1], x[0]))
        out.append((loja, itens))
    return out


def _itens_sem_sobra_safe(dia=None):
    """`itens_sem_sobra` best-effort pros SENDERS: a cobrança POR ITEM é
    adição de 01/08/2026 — se a query nova falhar por qualquer motivo, o
    alerta POR LOJA (que já funcionava antes) NÃO pode morrer junto. Erro
    fica visível no log (exception), nunca engolido em silêncio."""
    try:
        return itens_sem_sobra(dia)
    except Exception:  # noqa: BLE001 — alerta pré-existente não pode cair
        logger.exception('itens_sem_sobra falhou; alerta segue só por loja')
        return []


_KEY_CLAIM_SLACK = 'desperdicio_alerta_slack_tick'
_KEY_CLAIM_DONO = 'desperdicio_alerta_dono_dia'


def _claim_envio(chave, tick_id):
    """Claim persistente anti-duplicata (ver docstring do modulo): grava e
    COMMITA o marcador ANTES do envio — quem chegar depois com o MESMO
    tick_id pula. O advisory lock do cron cobre o caso SIMULTANEO; este
    claim cobre o SEQUENCIAL (container velho + novo no deploy).

    Retorna ('ok', valor_anterior) quando o claim e nosso, ('duplicata',
    None) quando outro processo ja enviou este tick, ('erro', None) quando
    nao deu pra gravar — sem claim duravel NAO se envia (duplicar e pior
    que atrasar um lembrete diario)."""
    from app.extensions import db
    from app.models import AppConfig
    try:
        atual = AppConfig.get(chave)
        if atual == tick_id:
            return 'duplicata', None
        AppConfig.set(chave, tick_id)
        db.session.commit()
        return 'ok', atual
    except Exception:  # noqa: BLE001 — claim indisponivel = nao envia
        logger.exception('desperdicio_alerta: claim %s falhou', chave)
        try:
            db.session.rollback()
        except Exception:  # noqa: BLE001
            pass
        return 'erro', None


def _devolver_claim(chave, anterior):
    """Devolve o claim quando o ENVIO falhou (Slack/Z-API fora) — o proximo
    tick/dia nao fica bloqueado por mensagem que nunca saiu. Best-effort:
    falha aqui so deixa o claim "gasto" (perde 1 lembrete, volta amanha)."""
    from app.extensions import db
    from app.models import AppConfig
    try:
        AppConfig.set(chave, anterior)
        db.session.commit()
    except Exception:  # noqa: BLE001
        logger.exception('desperdicio_alerta: devolver claim %s falhou', chave)
        try:
            db.session.rollback()
        except Exception:  # noqa: BLE001
            pass


def mensagem_pendentes(lojas, itens_por_loja=None):
    """Monta o texto do alerta: lojas sem NENHUM lançamento + itens
    cobrados nominalmente (`itens_sem_sobra`). Qualquer um dos dois pode
    vir vazio."""
    partes = ['⚠ *Sobras de hoje — pendências*']
    if lojas:
        nomes = '\n'.join(f'• {lj.nome}' for lj in lojas)
        partes.append('Lojas que ainda não lançaram NADA:\n' + nomes)
    for loja, itens in (itens_por_loja or []):
        visiveis = itens[:_MAX_ITENS_POR_LOJA]
        resto = len(itens) - len(visiveis)
        linha = ', '.join(f'{nome} ({saldo})' for nome, saldo in visiveis)
        if resto > 0:
            linha += f' e mais {resto}'
        partes.append(f'🥐 *{loja.nome}* — itens sem sobra lançada '
                      f'(lance a sobra ou confira o estoque):\n{linha}')
    return '\n'.join(partes)


def mensagem_resumo(lojas, itens_por_loja=None):
    """Versão COMPACTA pro WhatsApp do dono (14/08/2026: "muita informação,
    só fala se lançaram ou não"): UMA linha por loja — quem não lançou nada
    e quem tem N itens pendentes. Sem nome de item, sem saldo. O detalhe
    nominal por item continua no Slack (`mensagem_pendentes`), que é o
    canal onde o gerente age. Loja que não lançou NADA não repete na
    lista de itens (a linha "não lançou nada" já diz tudo)."""
    sem_nada = {lj.id for lj in lojas}
    linhas = ['⚠ *Sobras de hoje — pendências*']
    for lj in lojas:
        linhas.append(f'• {lj.nome} — não lançou nada')
    tem_parcial = False
    for loja, itens in (itens_por_loja or []):
        if loja.id in sem_nada:
            continue
        tem_parcial = True
        n = len(itens)
        plural = 'itens' if n != 1 else 'item'
        linhas.append(f'• {loja.nome} — lançou parcial, {n} {plural} '
                      'sem sobra')
    # O rodapé só quando existe detalhe nominal a mais no Slack — com só
    # "não lançou nada", o Slack não tem nada além desta mesma lista.
    if tem_parcial:
        linhas.append('_Detalhe por item no Slack._')
    return '\n'.join(linhas)


def alertar_slack_pendentes(dia=None):
    """Posta no canal Slack `SLACK_CANAL_COPILOT` a lista de lojas que ainda
    nao lancaram desperdicio. So envia se houver pendentes; se o canal nao
    estiver configurado, loga warning e pula. Retorna dict com status."""
    from app.services import slack

    faltam = lojas_sem_desperdicio(dia)
    itens = _itens_sem_sobra_safe(dia)
    if not faltam and not itens:
        logger.info('desperdicio_alerta(slack): sem pendencias, nada a enviar')
        return {'enviado': False, 'motivo': 'sem_pendencias'}

    n_itens = sum(len(its) for _, its in itens)
    canal = (current_app.config.get('SLACK_CANAL_COPILOT') or '').strip()
    if not canal:
        logger.warning('desperdicio_alerta(slack): SLACK_CANAL_COPILOT nao '
                       'configurado, pulando (%d loja[s], %d item[ns])',
                       len(faltam), n_itens)
        return {'enviado': False, 'motivo': 'sem_canal_configurado',
                'pendentes': len(faltam), 'pendentes_itens': n_itens}

    texto = mensagem_pendentes(faltam, itens)
    res = slack.post_message(canal, texto)
    if res.get('ok'):
        logger.info('desperdicio_alerta(slack): enviado pro canal %s '
                    '(%d loja[s], %d item[ns] nominais)',
                    canal, len(faltam), n_itens)
        return {'enviado': True, 'pendentes': len(faltam),
                'pendentes_itens': n_itens}
    logger.warning('desperdicio_alerta(slack): falha ao enviar: %s', res.get('erro'))
    return {'enviado': False, 'motivo': 'erro_envio', 'erro': res.get('erro'),
            'pendentes': len(faltam), 'pendentes_itens': n_itens}


def enviar_alerta_desperdicio():
    """Job WhatsApp: se alguma loja nao lancou desperdicio hoje, avisa o dono
    (ZAPI_NUMERO_DESTINO). So envia se houver pendentes.

    Mensagem COMPACTA (`mensagem_resumo`) desde 14/08/2026 — o dono pediu
    so "lançou ou não" no WhatsApp dele; a lista nominal de itens segue
    no Slack (`alertar_slack_pendentes`)."""
    from app.services import zapi

    numero = (current_app.config.get('ZAPI_NUMERO_DESTINO') or '').strip()
    if not numero:
        logger.info('desperdicio_alerta: ZAPI_NUMERO_DESTINO nao configurado, pulando')
        return
    if not zapi.disponivel():
        logger.info('desperdicio_alerta: Z-API nao configurado, pulando')
        return

    faltam = lojas_sem_desperdicio()
    itens = _itens_sem_sobra_safe()
    if not faltam and not itens:
        logger.info('desperdicio_alerta: todas as lojas lancaram, nada a enviar')
        return

    texto = mensagem_resumo(faltam, itens)
    res = zapi.enviar_texto(numero, texto)
    if res.get('ok'):
        logger.info('desperdicio_alerta: enviado pra %s (%d loja[s], '
                    '%d item[ns] nominais)', numero, len(faltam),
                    sum(len(its) for _, its in itens))
    else:
        logger.warning('desperdicio_alerta: falha ao enviar: %s', res.get('erro'))
