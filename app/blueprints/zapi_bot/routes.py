"""Webhook do Z-API pra o bot privado do dono.

Autenticacao: segredo na URL (?k=ZAPI_BOT_WEBHOOK_TOKEN). Z-API nao manda
header HMAC — o segredo na query e a defesa contra requisicao espuria.

Processamento async (thread). Responde 200 imediato, ja que o Z-API tem
timeout curto.
"""
import logging
import threading

from flask import current_app, jsonify, request

from app.blueprints.zapi_bot import zapi_bot_bp
from app.extensions import csrf

logger = logging.getLogger(__name__)


def _token_ok(provided):
    expected = (current_app.config.get('ZAPI_BOT_WEBHOOK_TOKEN') or '').strip()
    if not expected:
        return False
    # Timing-safe: == em Python sai cedo no 1o caractere diferente —
    # atacante pode medir o microtempo e adivinhar o token caractere a
    # caractere. compare_digest sempre demora o mesmo. Mesmo padrao do
    # crm/routes.py:168.
    import secrets as _s
    return _s.compare_digest(str(provided or ''), expected)


@zapi_bot_bp.route('/webhook', methods=['POST'])
@csrf.exempt
def webhook():
    """Webhook Z-API: 'ao receber mensagem'. So aceita do numero do dono."""
    if not _token_ok(request.args.get('k') or ''):
        return jsonify({'ok': False, 'erro': 'token invalido'}), 403

    payload = request.get_json(silent=True) or {}

    app = current_app._get_current_object()

    def _processar():
        with app.app_context():
            try:
                from app.services import zapi_bot
                zapi_bot.processar_payload(payload)
            except Exception:  # noqa: BLE001
                logger.exception('zapi_bot webhook: processamento falhou')

    threading.Thread(target=_processar, daemon=True).start()
    return jsonify({'ok': True}), 200
