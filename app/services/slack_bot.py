"""Orchestrator do bot Slack — recebe eventos, mapeia usuario, chama copilot.

Fluxo de uma mensagem:
1. Webhook /slack/events recebe event_callback
2. Valida signing + idempotencia (SlackEventoProcessado)
3. Resolve SlackVinculo: slack_user_id → Usuario
4. Carrega/cria SlackConversa (multi-turn context via CopilotConversa)
5. Chama copilot_svc.interpretar
6. Decide:
   - tipo='conversa' ou 'erro' → posta texto
   - tipo=read tool          → posta texto do resultado
   - tipo=write tool         → cria SlackAcaoPendente + posta Block Kit com botoes

Botao Confirmar/Cancelar → /slack/interact:
7. Resolve token → SlackAcaoPendente
8. Confirmar → copilot_svc.executar + chat.update com sucesso/erro
9. Cancelar  → chat.update com mensagem de cancelado
"""
import json
import logging
import secrets
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from flask import current_app

from app.extensions import db
from app.utils import agora

logger = logging.getLogger(__name__)

# Pool dedicado pra processamento async dos eventos Slack.
# Slack exige ack <3s; chamada Haiku leva 2-5s. Resposta vai por
# chat.postMessage depois.
_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix='slack-bot')

# TTL pra acoes pendentes (botoes nas mensagens)
ACAO_TTL = timedelta(minutes=10)


def _resolver_usuario(slack_user_id):
    """Procura SlackVinculo ativo → retorna Usuario ou None."""
    from app.models import SlackVinculo, Usuario
    v = (SlackVinculo.query
         .filter_by(slack_user_id=slack_user_id, ativo=True)
         .first())
    if not v:
        return None
    return Usuario.query.get(v.usuario_id)


def _evento_visto(event_id):
    """True se ja processamos. False caso contrario, marcando como visto."""
    from app.models import SlackEventoProcessado
    if not event_id:
        return False
    if SlackEventoProcessado.query.get(event_id):
        return True
    try:
        db.session.add(SlackEventoProcessado(event_id=event_id))
        db.session.commit()
    except Exception:
        db.session.rollback()
        # race: outro worker pegou primeiro
        return True
    return False


def _conversa(slack_user_id, slack_channel_id):
    """Recupera/cria SlackConversa pra (user, channel)."""
    from app.models import SlackConversa
    sc = (SlackConversa.query
          .filter_by(slack_user_id=slack_user_id,
                     slack_channel_id=slack_channel_id)
          .first())
    if sc:
        return sc
    sc = SlackConversa(slack_user_id=slack_user_id,
                       slack_channel_id=slack_channel_id,
                       mensagens_json='[]')
    db.session.add(sc)
    db.session.commit()
    return sc


def _historico_da_conversa(sc):
    try:
        return json.loads(sc.mensagens_json or '[]')
    except (ValueError, TypeError):
        return []


def _salvar_historico(sc, historico):
    sc.mensagens_json = json.dumps(historico[-40:], ensure_ascii=False)
    db.session.add(sc)
    db.session.commit()


def _canal_permitido(channel_id, channel_type):
    """Retorna True se o bot deve responder neste canal.

    DM (channel_type='im') sempre OK. Canais publicos: so se id estiver
    em SLACK_CANAIS_PERMITIDOS (CSV).
    """
    if channel_type == 'im':
        return True
    permitidos = (current_app.config.get('SLACK_CANAIS_PERMITIDOS') or '').strip()
    if not permitidos:
        return False
    return channel_id in {c.strip() for c in permitidos.split(',') if c.strip()}


def processar_evento_mensagem(evento):
    """Processa um event de mensagem (DM ou @mention).

    Disparado async via ThreadPoolExecutor — o handler de /slack/events
    so dispara isso e responde 200 imediatamente.
    """
    from app.services import slack as slack_api
    from app.services import slack_blocks
    from app.services import copilot as copilot_svc

    slack_user_id = evento.get('user')
    channel = evento.get('channel')
    text = (evento.get('text') or '').strip()
    channel_type = evento.get('channel_type', '')
    thread_ts = evento.get('thread_ts')
    files = evento.get('files') or []

    if not slack_user_id or not channel:
        return
    if not text and not files:
        return
    if not text:
        text = '(imagem enviada)'

    if not _canal_permitido(channel, channel_type):
        return

    # Em @mention, remove o '<@U12345>' inicial
    bot_uid_marker = '<@'
    if text.startswith(bot_uid_marker):
        fim = text.find('>')
        if fim > 0:
            text = text[fim + 1:].strip()
    if not text:
        return

    user = _resolver_usuario(slack_user_id)
    if not user:
        slack_api.post_message(
            channel,
            text=('Voce nao esta autorizado a usar o bot. '
                  'Peca pro admin vincular seu Slack ao sistema em /slack/install.'),
            thread_ts=thread_ts,
        )
        return

    # Carrega contexto
    try:
        sc = _conversa(slack_user_id, channel)
    except Exception:
        logger.exception('slack_bot: falha ao carregar conversa')
        slack_api.post_message(channel,
                                text='Erro interno carregando contexto.',
                                thread_ts=thread_ts)
        return

    historico = _historico_da_conversa(sc)

    # Baixa imagens (se houver) e converte pra base64 pro Claude
    imagens = []
    for f in files:
        mime = (f.get('mimetype') or '').lower()
        if not mime.startswith('image/'):
            continue
        arq = slack_api.baixar_arquivo(f)
        if not arq:
            continue
        import base64
        b64 = base64.b64encode(arq['bytes']).decode('ascii')
        imagens.append({
            'mimetype': arq['mimetype'] or 'image/jpeg',
            'base64': b64,
        })

    # Chama o copilot
    try:
        resp = copilot_svc.interpretar(text, user, historico=historico,
                                        images=imagens or None)
    except Exception:
        logger.exception('slack_bot: copilot.interpretar falhou')
        slack_api.post_message(channel,
                                text='Erro processando seu pedido. Tente de novo.',
                                thread_ts=thread_ts)
        return

    # Atualiza historico
    historico.append({'role': 'user', 'content': text})
    historico.append({'role': 'assistant',
                       'content': resp.get('explicacao') or ''})
    try:
        _salvar_historico(sc, historico)
    except Exception:
        logger.exception('slack_bot: falha salvando historico')

    tipo = resp.get('tipo')
    explicacao = resp.get('explicacao') or ''

    if tipo == 'erro':
        slack_api.post_message(channel,
                                blocks=slack_blocks.build_texto(f':warning: {explicacao}'),
                                text=explicacao,
                                thread_ts=thread_ts)
        return

    if tipo == 'conversa':
        slack_api.post_message(channel,
                                blocks=slack_blocks.build_texto(explicacao),
                                text=explicacao[:200],
                                thread_ts=thread_ts)
        return

    # Tool call
    if not resp.get('requer_aprovacao'):
        # Read tool: ja executou
        resultado = resp.get('resultado') or {}
        texto = resultado.get('texto') or explicacao
        if resultado.get('erro'):
            texto = f':warning: {resultado["erro"]}'
        slack_api.post_message(channel,
                                blocks=slack_blocks.build_texto(texto),
                                text=texto[:200],
                                thread_ts=thread_ts)
        return

    # Write tool: cria SlackAcaoPendente + preview Block Kit.
    # Se tem imagens E a tool e anexar_foto_pedido, embute as imagens nos
    # params pra o executor ter acesso quando clicar Confirmar.
    params_acao = dict(resp.get('params') or {})
    if tipo == 'anexar_foto_pedido' and imagens:
        params_acao['imagens'] = imagens
        params_acao['_n_imagens'] = len(imagens)

    token = secrets.token_urlsafe(24)
    try:
        from app.models import SlackAcaoPendente
        acao = SlackAcaoPendente(
            token=token,
            slack_user_id=slack_user_id,
            slack_channel_id=channel,
            tipo_acao=tipo,
            params_json=json.dumps(params_acao, ensure_ascii=False, default=str),
            usuario_id=user.id,
        )
        db.session.add(acao)
        db.session.commit()
    except Exception:
        logger.exception('slack_bot: falha criando acao pendente')
        db.session.rollback()
        slack_api.post_message(channel,
                                text='Erro interno preparando acao.',
                                thread_ts=thread_ts)
        return

    # Usa params_acao (que tem _n_imagens pra preview da foto)
    blocks = slack_blocks.build_preview(tipo, params_acao,
                                         token=token,
                                         explicacao=explicacao)
    res = slack_api.post_message(channel, blocks=blocks,
                                  text=f'Confirme: {tipo}',
                                  thread_ts=thread_ts)
    # Salva ts pra chat.update apos clique
    if res.get('ok') and res.get('ts'):
        try:
            acao.slack_message_ts = res['ts']
            db.session.add(acao)
            db.session.commit()
        except Exception:
            db.session.rollback()


def disparar_evento(evento):
    """Submete processamento async — slack_bp ack imediato (<3s)."""
    app = current_app._get_current_object()

    def _runner():
        with app.app_context():
            try:
                processar_evento_mensagem(evento)
            except Exception:
                logger.exception('slack_bot: erro processando evento')

    _executor.submit(_runner)


def processar_interacao_botao(action_id, token, slack_user_id, channel_id,
                                message_ts):
    """Clique em Confirmar/Cancelar. Chamado async via /slack/interact."""
    from app.models import SlackAcaoPendente
    from app.services import slack as slack_api
    from app.services import slack_blocks
    from app.services import copilot as copilot_svc

    acao = SlackAcaoPendente.query.filter_by(token=token).first()
    if not acao:
        slack_api.update_message(channel_id, message_ts,
                                  blocks=slack_blocks.build_expirado(),
                                  text='acao expirou')
        return

    # Quem clicou tem que ser quem pediu (impede outros usuarios do canal)
    if acao.slack_user_id != slack_user_id:
        slack_api.post_message(channel_id,
                                text='So quem pediu pode confirmar.',
                                thread_ts=message_ts)
        return

    if acao.executado_em or acao.cancelado_em:
        slack_api.update_message(channel_id, message_ts,
                                  blocks=slack_blocks.build_expirado(),
                                  text='ja processada')
        return

    if agora() - (acao.criado_em or agora()) > ACAO_TTL:
        acao.cancelado_em = agora()
        db.session.add(acao)
        db.session.commit()
        slack_api.update_message(channel_id, message_ts,
                                  blocks=slack_blocks.build_expirado(),
                                  text='expirou')
        return

    if action_id == 'copilot_cancelar':
        acao.cancelado_em = agora()
        db.session.add(acao)
        db.session.commit()
        slack_api.update_message(channel_id, message_ts,
                                  blocks=slack_blocks.build_cancelado(),
                                  text='cancelado')
        return

    if action_id == 'copilot_confirmar':
        from app.models import Usuario
        user = Usuario.query.get(acao.usuario_id)
        if not user:
            slack_api.update_message(channel_id, message_ts,
                                      blocks=slack_blocks.build_resultado(
                                          {'erro': 'usuario do vinculo nao encontrado'},
                                          ok=False),
                                      text='erro')
            return

        try:
            params = json.loads(acao.params_json or '{}')
        except (ValueError, TypeError):
            params = {}

        try:
            resultado = copilot_svc.executar(acao.tipo_acao, params, user)
        except Exception as exc:  # noqa: BLE001
            logger.exception('slack_bot: executar falhou')
            resultado = {'ok': False, 'erro': str(exc)}

        ok = bool(resultado.get('ok'))
        if ok:
            acao.executado_em = agora()
        else:
            acao.cancelado_em = agora()
        db.session.add(acao)
        db.session.commit()

        slack_api.update_message(channel_id, message_ts,
                                  blocks=slack_blocks.build_resultado(resultado, ok=ok),
                                  text='feito' if ok else 'erro')


def disparar_interacao_botao(action_id, token, slack_user_id, channel_id,
                              message_ts):
    """Wrap async — ack imediato no /slack/interact."""
    app = current_app._get_current_object()

    def _runner():
        with app.app_context():
            try:
                processar_interacao_botao(action_id, token, slack_user_id,
                                          channel_id, message_ts)
            except Exception:
                logger.exception('slack_bot: erro processando interacao')

    _executor.submit(_runner)


def processar_interacao_lembrete(action_id, valor, slack_user_id, channel_id,
                                  message_ts):
    """Botoes do lembrete pedido amanha. value vem como 'loja_id:YYYY-MM-DD'."""
    from datetime import datetime
    from app.models import LembretePedidoOptOut, Loja, SlackVinculo
    from app.services import slack as slack_api

    try:
        loja_id_str, data_str = valor.split(':', 1)
        loja_id = int(loja_id_str)
        data = datetime.strptime(data_str, '%Y-%m-%d').date()
    except (ValueError, AttributeError):
        slack_api.update_message(channel_id, message_ts,
                                  text='Erro: token invalido.')
        return

    loja = Loja.query.get(loja_id)
    if not loja:
        slack_api.update_message(channel_id, message_ts,
                                  text='Loja nao encontrada.')
        return

    # Identifica quem clicou (pra audit)
    vinc = SlackVinculo.query.filter_by(slack_user_id=slack_user_id, ativo=True).first()
    usuario_id = vinc.usuario_id if vinc else None

    if action_id == 'lembrete_no_pedido':
        # Cria opt-out (ignora se ja existe)
        existente = LembretePedidoOptOut.query.filter_by(
            loja_id=loja_id, data_entrega=data).first()
        if not existente:
            db.session.add(LembretePedidoOptOut(
                loja_id=loja_id, data_entrega=data,
                marcado_por_slack_uid=slack_user_id,
                marcado_por_id=usuario_id,
            ))
            db.session.commit()
        slack_api.update_message(
            channel_id, message_ts,
            text=f'OK, sem pedido pra {loja.nome} em {data.strftime("%d/%m")}',
            blocks=[{'type': 'section',
                     'text': {'type': 'mrkdwn',
                              'text': (f':white_check_mark: *{loja.nome}* sem pedido '
                                        f'pra {data.strftime("%d/%m/%Y")} '
                                        f'(<@{slack_user_id}> marcou).')}}],
        )
        return

    if action_id == 'lembrete_fazer_pedido':
        # O botao tem 'url' (Slack ja abre o link). Aqui so atualizamos a msg
        # pra registrar que alguem clicou pra fazer o pedido.
        slack_api.update_message(
            channel_id, message_ts,
            text=f'{loja.nome}: pedido em andamento',
            blocks=[{'type': 'section',
                     'text': {'type': 'mrkdwn',
                              'text': (f':pencil2: <@{slack_user_id}> abriu o '
                                        f'formulario pra criar pedido da '
                                        f'*{loja.nome}* ({data.strftime("%d/%m")}).')}}],
        )
        return


def disparar_interacao_lembrete(action_id, valor, slack_user_id, channel_id,
                                 message_ts):
    app = current_app._get_current_object()

    def _runner():
        with app.app_context():
            try:
                processar_interacao_lembrete(action_id, valor, slack_user_id,
                                              channel_id, message_ts)
            except Exception:
                logger.exception('slack_bot: erro processando lembrete')

    _executor.submit(_runner)
