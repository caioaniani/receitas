"""Portal Wi-Fi das lojas (11/07/2026) — rotas públicas em /loja/wifi.

Vive no blueprint da LOJA de propósito: o gate por host (app/__init__.py)
só libera /loja/* em opao.online, e a sessão de cliente criada no login
one-time precisa nascer no MESMO host do site. O gate LOJA_VISIVEL do
blueprint vale aqui também (before_request).

Fluxo: GET /loja/wifi (form; recebe os params do portal externo do Omada
quando o enforcement estiver ligado) → POST /cadastrar (cria a sessão,
mostra o botão do WhatsApp) → o cliente manda o código WIFI-XXXXXX → o
webhook do Chatwoot valida (crm/routes) → GET /status/<token> (polling da
tela) → GET /entrar/<token> (login one-time; o link chega pelo WhatsApp —
abre no navegador REAL, fora do mini-navegador do portal cativo, que não
compartilha cookies com o Safari/Chrome)."""
from flask import jsonify, redirect, render_template, request, url_for

from app.blueprints.loja import loja_bp
from app.extensions import limiter
from app.services import loja_auth, wifi_portal


def _params_omada():
    """Params que o portal externo do Omada anexa no redirect. No teste por
    link/QR (pré-enforcement) vêm vazios — tudo opcional."""
    a = request.args if request.method == 'GET' else request.form
    return {k: (a.get(k) or '').strip()
            for k in ('clientMac', 'apMac', 'ssidName', 'site',
                      'redirectUrl')}


@loja_bp.route('/wifi')
def wifi_portal_form():
    return render_template('loja/wifi_portal.html',
                           params=_params_omada(), erros=None, form={})


@loja_bp.route('/wifi/cadastrar', methods=['POST'])
@limiter.limit('10 per minute')
def wifi_cadastrar():
    dados, erros = wifi_portal.validar_form(request.form)
    if erros:
        return render_template('loja/wifi_portal.html',
                               params=_params_omada(), erros=erros,
                               form=request.form), 400
    sessao = wifi_portal.criar_sessao(dados, _params_omada())
    return redirect(url_for('loja.wifi_validar', token=sessao.token))


@loja_bp.route('/wifi/validar/<token>')
def wifi_validar(token):
    from app.models import WifiPortalSessao
    s = WifiPortalSessao.query.filter_by(token=token).first_or_404()
    return render_template(
        'loja/wifi_validar.html', sessao=s,
        link_whatsapp=wifi_portal.link_whatsapp(s),
        mensagem=wifi_portal.mensagem_whatsapp(s))


@loja_bp.route('/wifi/status/<token>')
@limiter.limit('60 per minute')
def wifi_status(token):
    """Polling da tela de validação (a cada 3 s)."""
    from app.models import WifiPortalSessao
    s = WifiPortalSessao.query.filter_by(token=token).first()
    if s is None:
        return jsonify({'ok': False, 'erro': 'sessao nao encontrada'}), 404
    return jsonify({
        'ok': True,
        'validado': s.validado_em is not None,
        'resultado': s.resultado,
        'wifi_autorizado': s.wifi_autorizado_em is not None,
    })


@loja_bp.route('/wifi/entrar/<token>')
def wifi_entrar(token):
    """Login one-time do link enviado por WhatsApp/e-mail. Single-use +
    expira em 30 min (wifi_portal.usar_login_token)."""
    cliente, _s = wifi_portal.usar_login_token(token)
    if cliente is None or not cliente.ativo:
        return render_template('loja/wifi_link_invalido.html'), 410
    loja_auth.login_cliente(cliente)
    return redirect(url_for('loja.home'))
