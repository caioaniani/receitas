"""Aba de notificacoes WhatsApp: status da instancia Z-API, historico de
envios e automacoes (mensagens agendadas) configuraveis pelo admin."""
from flask import current_app, flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from app.blueprints.notificacoes import notificacoes_bp
from app.decorators import admin_required
from app.extensions import csrf, db
from app.models import AutomacaoWhatsapp, NotificacaoWhatsapp


def _destino_padrao():
    return (current_app.config.get('ZAPI_NUMERO_DESTINO') or '').strip()


def _parse_dias(form):
    """Dias marcados (0=seg..6=dom) -> CSV. Vazio = todos os dias."""
    dias = [d for d in ('0', '1', '2', '3', '4', '5', '6') if form.get(f'dia_{d}')]
    return ','.join(dias)


@notificacoes_bp.route('/')
@login_required
@admin_required
def index():
    from app.services import zapi
    status = zapi.status_instancia()
    automacoes = AutomacaoWhatsapp.query.order_by(AutomacaoWhatsapp.horario).all()
    envios = (NotificacaoWhatsapp.query
              .order_by(NotificacaoWhatsapp.criado_em.desc()).limit(50).all())
    return render_template('notificacoes/index.html', status=status,
                           automacoes=automacoes, envios=envios,
                           destino_padrao=_destino_padrao())


@notificacoes_bp.route('/status.json')
@login_required
@admin_required
def status_json():
    """Proxy seguro: o navegador nunca recebe token/ID na URL externa."""
    from app.services import zapi
    status = zapi.status_instancia()
    return jsonify(status), 200 if status.get('ok') else 503


@notificacoes_bp.route('/qr')
@login_required
@admin_required
def qr():
    from app.services import zapi
    resultado = zapi.obter_qr_code()
    return render_template('notificacoes/qr.html', resultado=resultado)


@notificacoes_bp.route('/reiniciar', methods=['POST'])
@login_required
@admin_required
def reiniciar():
    from app.services import zapi
    resultado = zapi.reiniciar_instancia()
    flash('Instância reiniciada. Aguarde alguns segundos e atualize o status.'
          if resultado.get('ok') else
          f"Falha ao reiniciar: {resultado.get('erro')}",
          'success' if resultado.get('ok') else 'danger')
    return redirect(url_for('notificacoes.index'))


@notificacoes_bp.route('/webhooks/assinar', methods=['POST'])
@login_required
@admin_required
def webhooks_assinar():
    from app.services import zapi_saude
    resultado = zapi_saude.garantir_assinatura(force=True)
    flash('Alertas de conexão ativados na Z-API.'
          if resultado.get('ok') else
          f"Falha ao ativar alertas: {resultado.get('erro')}",
          'success' if resultado.get('ok') else 'danger')
    return redirect(url_for('notificacoes.index'))


def _webhook_autorizado():
    from app.services import zapi_saude
    return zapi_saude.token_valido(request.args.get('k'))


@notificacoes_bp.route('/webhook/zapi/desconectado', methods=['POST'])
@csrf.exempt
def webhook_desconectado():
    if not _webhook_autorizado():
        return jsonify({'ok': False, 'erro': 'token invalido'}), 403
    from app.services import zapi_saude
    resultado = zapi_saude.registrar_desconexao(
        request.get_json(silent=True) or {})
    return jsonify(resultado), 200 if resultado.get('ok') else 400


@notificacoes_bp.route('/webhook/zapi/conectado', methods=['POST'])
@csrf.exempt
def webhook_conectado():
    if not _webhook_autorizado():
        return jsonify({'ok': False, 'erro': 'token invalido'}), 403
    from app.services import zapi_saude
    resultado = zapi_saude.registrar_conexao(
        request.get_json(silent=True) or {})
    return jsonify(resultado), 200 if resultado.get('ok') else 400


@notificacoes_bp.route('/automacoes', methods=['POST'])
@login_required
@admin_required
def automacao_criar():
    nome = (request.form.get('nome') or '').strip()
    horario = (request.form.get('horario') or '').strip()
    mensagem = (request.form.get('mensagem') or '').strip()
    if not (nome and horario and mensagem):
        flash('Preencha nome, horário e mensagem.', 'warning')
        return redirect(url_for('notificacoes.index'))
    db.session.add(AutomacaoWhatsapp(
        nome=nome, horario=horario, mensagem=mensagem,
        dias_semana=_parse_dias(request.form),
        destino=(request.form.get('destino') or '').strip() or None,
        ativo=True, criado_por_id=current_user.id))
    db.session.commit()
    flash('Automação criada.', 'success')
    return redirect(url_for('notificacoes.index'))


@notificacoes_bp.route('/automacoes/<int:id>', methods=['POST'])
@login_required
@admin_required
def automacao_acao(id):
    a = AutomacaoWhatsapp.query.get_or_404(id)
    acao = request.form.get('acao')
    if acao == 'excluir':
        db.session.delete(a)
        db.session.commit()
        flash('Automação excluída.', 'info')
    elif acao == 'toggle':
        a.ativo = not a.ativo
        db.session.commit()
        flash('Automação ' + ('ativada.' if a.ativo else 'pausada.'), 'success')
    elif acao == 'testar':
        from app.services.whatsapp import notificar
        destino = (a.destino or '').strip() or _destino_padrao()
        if not destino:
            flash('Sem número de destino (configure ZAPI_NUMERO_DESTINO).', 'warning')
        else:
            res = notificar(destino, a.mensagem, origem=f'automacao:{a.id} (teste)')
            flash('Teste enviado.' if res.get('ok') else f"Falhou: {res.get('erro')}",
                  'success' if res.get('ok') else 'danger')
    return redirect(url_for('notificacoes.index'))


@notificacoes_bp.route('/testar', methods=['POST'])
@login_required
@admin_required
def testar():
    from app.services.whatsapp import notificar
    numero = (request.form.get('numero') or '').strip() or _destino_padrao()
    msg = (request.form.get('mensagem') or '').strip() or 'Teste de notificação do sistema.'
    if not numero:
        flash('Informe um número (ou configure ZAPI_NUMERO_DESTINO).', 'warning')
        return redirect(url_for('notificacoes.index'))
    res = notificar(numero, msg, origem='manual')
    flash('Mensagem enviada.' if res.get('ok') else f"Falhou: {res.get('erro')}",
          'success' if res.get('ok') else 'danger')
    return redirect(url_for('notificacoes.index'))


@notificacoes_bp.route('/disparar/<tipo>', methods=['POST'])
@login_required
@admin_required
def disparar(tipo):
    """Dispara manualmente um digest do sistema (tarefas/anomalias)."""
    try:
        if tipo == 'tarefas':
            from app.services import zapi_resumos
            # claim=False: re-envio DELIBERADO do admin nunca e bloqueado
            # pelo anti-duplicata diario do cron das 07:00.
            zapi_resumos.enviar_digest_tarefas(claim=False)
        elif tipo == 'anomalias':
            from app.services import anomalias
            anomalias.enviar_digest_whatsapp()
        else:
            flash('Tipo inválido.', 'warning')
            return redirect(url_for('notificacoes.index'))
        flash('Disparo executado — confira o histórico abaixo.', 'success')
    except Exception as exc:  # noqa: BLE001
        flash(f'Falhou: {exc}', 'danger')
    return redirect(url_for('notificacoes.index'))
