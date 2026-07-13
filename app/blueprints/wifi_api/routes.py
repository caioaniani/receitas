"""API de autenticação do Wi-Fi das lojas — /api/wifi/* (13/07/2026).

O portal do Omada (OC200) roda no modo **RADIUS**: o cliente digita e-mail +
senha (a MESMA conta do site, `Cliente`) na tela do controlador, e o OC200
pergunta a um servidor RADIUS externo se a senha confere. O OC200 não fala
HTTP com a gente diretamente (e o Railway não expõe UDP), então a arquitetura
tem duas peças:

1. **Esta API** (roda no gestão.opao, onde vivem as contas): valida
   e-mail+senha contra `Cliente`. É o cérebro.
2. **A ponte RADIUS** (`wifi_radius/bridge.py`, roda num servidorzinho com
   UDP): recebe o Access-Request do OC200, decripta a senha (PAP) e chama
   ESTE endpoint. É só um tradutor de protocolo.

Por que a trava dura funciona SEM o problema de certificado do External
Portal: é o OC200 que SAI perguntando pra ponte (saída pela internet, que o
roteador da loja/CGNAT não bloqueia — igual ele já fala com a nuvem TP-Link).

Segurança (mesmo padrão do CLAUDE_API_TOKEN):
- `Authorization: Bearer <WIFI_RADIUS_TOKEN>` (a ponte manda; timing-safe).
- Sem WIFI_RADIUS_TOKEN no env → 503 (desligado).
- Rate limit: o endpoint é um ORÁCULO de senha — limitado por IP.
- Resposta NUNCA revela se o e-mail existe (mesma resposta pra "sem conta"
  e "senha errada") — anti-enumeração.
"""
import secrets as _secrets
from functools import wraps

from flask import current_app, jsonify, request

from app.blueprints.wifi_api import wifi_api_bp
from app.extensions import limiter


def _wifi_auth_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        token_cfg = (current_app.config.get('WIFI_RADIUS_TOKEN')
                     or '').strip()
        if not token_cfg:
            return jsonify(ok=False,
                           erro='WIFI_RADIUS_TOKEN nao configurado'), 503
        auth = request.headers.get('Authorization', '')
        recebido = (auth[7:].strip()
                    if auth.lower().startswith('bearer ') else '')
        if not recebido or not _secrets.compare_digest(recebido, token_cfg):
            return jsonify(ok=False, erro='token invalido'), 401
        return f(*args, **kwargs)
    return wrapper


@wifi_api_bp.route('/ping')
@_wifi_auth_required
def ping():
    """Sonda pra ponte confirmar conectividade + token. Não toca no banco."""
    return jsonify(ok=True, servico='wifi-radius'), 200


@wifi_api_bp.route('/radius-check', methods=['POST'])
@limiter.limit('30 per minute')
@_wifi_auth_required
def radius_check():
    """Valida e-mail+senha de um `Cliente`. Chamado pela ponte RADIUS.

    Aceita JSON {email, senha} OU form-encoded (a ponte manda JSON). A
    resposta é sempre 200 com {ok: bool} nos casos de credencial — o status
    de auth vai no corpo, não no HTTP (a ponte trata ok=false como
    Access-Reject). 401/503 ficam pra erro de token/config."""
    dados = request.get_json(silent=True) or request.form
    email = (dados.get('email') or '').strip().lower()
    senha = dados.get('senha') or ''
    if not email or not senha:
        return jsonify(ok=False, motivo='faltou email ou senha'), 200

    from app.models import Cliente
    cliente = Cliente.query.filter(
        Cliente.email == email).first()
    # Anti-enumeração: mesma resposta pra "sem conta", "guest sem senha",
    # "inativo" e "senha errada". Nunca dizemos qual foi.
    ok = bool(cliente and cliente.ativo and cliente.tem_conta
              and cliente.check_senha(senha))
    if ok:
        current_app.logger.info('wifi radius-check OK: %s', email)
    else:
        current_app.logger.info('wifi radius-check FALHOU: %s', email)
    return jsonify(ok=ok), 200
