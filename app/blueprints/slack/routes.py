"""Endpoints do Slack bot.

- /slack/events    POST  webhook do Slack (Events API)
- /slack/interact  POST  callback de cliques em botoes (Interactivity)
- /slack/install   GET   tela admin pra mapear slack_user → Usuario
- /slack/vincular  POST  cria/edita SlackVinculo
"""
import json
import logging

from flask import request, jsonify, render_template, redirect, url_for, flash, current_app
from flask_login import login_required, current_user

from app.blueprints.slack import slack_bp
from app.decorators import admin_required
from app.extensions import db, csrf, limiter
from app.utils import agora
from app.models import SlackVinculo, Usuario, SlackAcaoPendente
from app.services import slack as slack_api
from app.services import slack_bot

logger = logging.getLogger(__name__)

# Slack chama com Content-Type que nao tem CSRF token nosso; isenta o blueprint
# da verificacao CSRF — autenticidade vai pela signing da Slack.
csrf.exempt(slack_bp)


@slack_bp.route('/events', methods=['POST'])
@limiter.exempt
def events():
    """Webhook do Slack Events API. Verifica signing, dispara processamento."""
    raw_body = request.get_data(as_text=True)

    if not slack_api.verify_signing(request.headers, raw_body):
        logger.warning('slack /events: signing invalido')
        return jsonify(error='invalid signing'), 401

    try:
        payload = json.loads(raw_body)
    except ValueError:
        return jsonify(error='invalid json'), 400

    # URL verification (handshake inicial do app)
    if payload.get('type') == 'url_verification':
        return jsonify(challenge=payload.get('challenge', ''))

    if payload.get('type') != 'event_callback':
        return ('', 200)

    event = payload.get('event') or {}
    event_id = payload.get('event_id')

    # Ignora msgs do proprio bot (bot_id presente) e edits/deletes
    if event.get('bot_id') or event.get('subtype') in ('bot_message', 'message_deleted', 'message_changed'):
        return ('', 200)

    # Idempotencia (pode retornar 200 sem processar de novo)
    if slack_bot._evento_visto(event_id):
        return ('', 200)

    tipo = event.get('type')
    if tipo not in ('message', 'app_mention'):
        logger.warning('slack /events: ignorado tipo=%s', tipo)
        return ('', 200)

    # Filtra: aceita DM (im), @mention, OU msg em canal whitelisted.
    canal = event.get('channel')
    canal_tipo = event.get('channel_type', '')
    logger.warning('slack /events: tipo=%s canal=%s canal_tipo=%s subtype=%s user=%s',
                    tipo, canal, canal_tipo, event.get('subtype'), event.get('user'))
    if tipo == 'message' and canal_tipo != 'im':
        if not slack_bot._canal_permitido(canal, canal_tipo):
            logger.warning('slack /events: canal %s NAO esta no whitelist (env=%r)',
                            canal, current_app.config.get('SLACK_CANAIS_PERMITIDOS'))
            return ('', 200)

    slack_bot.disparar_evento(event)
    return ('', 200)


@slack_bp.route('/interact', methods=['POST'])
@limiter.exempt
def interact():
    """Callback de cliques em botoes (block_actions)."""
    raw_body = request.get_data(as_text=True)

    if not slack_api.verify_signing(request.headers, raw_body):
        logger.warning('slack /interact: signing invalido')
        return jsonify(error='invalid signing'), 401

    payload_raw = request.form.get('payload', '')
    try:
        payload = json.loads(payload_raw)
    except ValueError:
        return jsonify(error='invalid payload'), 400

    if payload.get('type') != 'block_actions':
        return ('', 200)

    user = payload.get('user') or {}
    slack_user_id = user.get('id')
    channel = (payload.get('channel') or {}).get('id')
    message = payload.get('message') or {}
    message_ts = message.get('ts')

    actions = payload.get('actions') or []
    if not actions:
        return ('', 200)
    action = actions[0]
    action_id = action.get('action_id')
    token = action.get('value')

    if not slack_user_id or not channel or not message_ts or not token:
        return ('', 200)
    if action_id not in ('copilot_confirmar', 'copilot_cancelar'):
        return ('', 200)

    slack_bot.disparar_interacao_botao(action_id, token, slack_user_id,
                                        channel, message_ts)
    return ('', 200)


# ── Admin: gerenciar vinculacoes (slack_user_id ↔ Usuario) ───────────


@slack_bp.route('/install', methods=['GET'])
@login_required
@admin_required
def install():
    vinculos = (SlackVinculo.query
                .order_by(SlackVinculo.ativo.desc(), SlackVinculo.criado_em.desc())
                .all())
    usuarios = Usuario.query.order_by(Usuario.nome).all()
    cfg_ok = slack_api.disponivel()
    return render_template('slack/install.html',
                            vinculos=vinculos, usuarios=usuarios,
                            cfg_ok=cfg_ok)


@slack_bp.route('/vincular', methods=['POST'])
@login_required
@admin_required
def vincular():
    slack_user_id = (request.form.get('slack_user_id') or '').strip()
    workspace_id = (request.form.get('slack_workspace_id') or '').strip() or None
    try:
        usuario_id = int(request.form.get('usuario_id') or 0)
    except (TypeError, ValueError):
        usuario_id = 0
    if not slack_user_id or not usuario_id:
        flash('Preencha slack_user_id e usuario.', 'warning')
        return redirect(url_for('slack.install'))
    if not Usuario.query.get(usuario_id):
        flash('Usuario invalido.', 'warning')
        return redirect(url_for('slack.install'))

    existente = SlackVinculo.query.filter_by(slack_user_id=slack_user_id).first()
    if existente:
        existente.usuario_id = usuario_id
        existente.slack_workspace_id = workspace_id
        existente.ativo = True
    else:
        v = SlackVinculo(
            slack_user_id=slack_user_id,
            slack_workspace_id=workspace_id,
            usuario_id=usuario_id,
            criado_por_id=current_user.id,
        )
        db.session.add(v)
    db.session.commit()
    flash('Vinculo salvo.', 'success')
    return redirect(url_for('slack.install'))


@slack_bp.route('/desvincular/<int:vinc_id>', methods=['POST'])
@login_required
@admin_required
def desvincular(vinc_id):
    v = SlackVinculo.query.get_or_404(vinc_id)
    v.ativo = False
    db.session.add(v)
    db.session.commit()
    flash('Vinculo desativado.', 'success')
    return redirect(url_for('slack.install'))
