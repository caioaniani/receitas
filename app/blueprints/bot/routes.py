"""API pra integracao com bots externos (n8n / WhatsApp).

Auth: header `Authorization: Bearer <BOT_API_TOKEN>`. Sem session/login —
projetada pra ser chamada por workflows externos.

CSRF desabilitado pra esses endpoints (sao read-only e protegidos por token).
"""
import secrets as _secrets
from datetime import date, datetime, timedelta
from functools import wraps

from flask import current_app, jsonify, request

from app.blueprints.bot import bot_bp
from app.extensions import csrf
from app.services import seru


def _normalizar_telefone(s):
    """Mantem so digitos. '+55 (11) 9 9999-9999' → '5511999999999'."""
    return ''.join(c for c in (s or '') if c.isdigit())


def _bot_auth_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        token_cfg = (current_app.config.get('BOT_API_TOKEN') or '').strip()
        if not token_cfg:
            return jsonify(ok=False, erro='BOT_API_TOKEN nao configurado no servidor'), 503
        # Token: Authorization: Bearer X OU ?token=X
        auth = request.headers.get('Authorization', '')
        token_recebido = ''
        if auth.lower().startswith('bearer '):
            token_recebido = auth[7:].strip()
        if not token_recebido:
            token_recebido = (request.args.get('token') or '').strip()
        if not token_recebido or not _secrets.compare_digest(token_recebido, token_cfg):
            return jsonify(ok=False, erro='token invalido'), 401

        # Whitelist de telefones (CSV em BOT_ALLOWED_PHONES). Se vazio, aceita
        # qualquer um (backwards compat). Telefone vem em ?telefone=X.
        allowed_csv = (current_app.config.get('BOT_ALLOWED_PHONES') or '').strip()
        if allowed_csv:
            telefone_recebido = _normalizar_telefone(request.args.get('telefone') or '')
            if not telefone_recebido:
                return jsonify(ok=False, erro='telefone obrigatorio (parametro telefone na query)'), 401
            permitidos = {_normalizar_telefone(t) for t in allowed_csv.split(',') if t.strip()}
            if telefone_recebido not in permitidos:
                current_app.logger.warning('bot: telefone nao autorizado: %s', telefone_recebido)
                return jsonify(ok=False, erro='telefone nao autorizado'), 403
        return f(*args, **kwargs)
    return wrapper


def _fmt_brl(valor):
    s = f'{valor:,.2f}'
    return s.replace(',', 'X').replace('.', ',').replace('X', '.')


@bot_bp.route('/api/bot/faturamento', methods=['GET'])
@csrf.exempt
@_bot_auth_required
def faturamento():
    """GET /api/bot/faturamento?data=YYYY-MM-DD

    Aliases aceitos: hoje, ontem.
    Devolve total por loja + total geral + mensagem pronta pra WhatsApp.
    """
    data_str = (request.args.get('data') or 'hoje').strip().lower()
    hoje = date.today()
    if data_str == 'hoje':
        target = hoje
    elif data_str == 'ontem':
        target = hoje - timedelta(days=1)
    else:
        try:
            target = datetime.strptime(data_str, '%Y-%m-%d').date()
        except ValueError:
            try:
                target = datetime.strptime(data_str, '%d/%m/%Y').date()
            except ValueError:
                return jsonify(ok=False, erro='data invalida (use YYYY-MM-DD ou DD/MM/AAAA, ou "hoje"/"ontem")'), 400

    # Mesma estrategia da aba PDV: expande updatedAt ate hoje pra capturar
    # vendas atualizadas depois da data, e filtra por createdAt local.
    dias_extra = max(0, (hoje - target).days) if target < hoje else 0
    try:
        pedidos = seru.listar_pedidos_completo(target, target, expandir_dias_frente=dias_extra)
    except Exception as e:
        current_app.logger.exception('bot/faturamento: Seru falhou')
        return jsonify(ok=False, erro=f'falha ao buscar Seru: {type(e).__name__}'), 502

    target_iso = target.isoformat()
    total = 0.0
    por_loja = {}
    qtd = 0
    for p in pedidos:
        if not isinstance(p, dict):
            continue
        if p.get('canceledAt'):
            continue
        if (p.get('createdAt') or '')[:10] != target_iso:
            continue
        valor = float(p.get('total') or 0)
        total += valor
        qtd += 1
        comp = p.get('company') or {}
        nome = (comp.get('name') if isinstance(comp, dict) else None) or '—'
        por_loja[nome] = por_loja.get(nome, 0) + valor

    # Mensagem WhatsApp (markdown leve, com *bold*)
    data_fmt = target.strftime('%d/%m/%Y')
    if not por_loja:
        mensagem = f'Faturamento de {data_fmt}: nenhuma venda registrada.'
    else:
        linhas = [f'*Faturamento de {data_fmt}*', '']
        for nome in sorted(por_loja, key=lambda k: por_loja[k], reverse=True):
            linhas.append(f'• {nome}: R$ {_fmt_brl(por_loja[nome])}')
        linhas.append('')
        linhas.append(f'*Total: R$ {_fmt_brl(total)}*')
        linhas.append(f'_{qtd} venda(s)_')
        mensagem = '\n'.join(linhas)

    return jsonify(
        ok=True,
        data=target_iso,
        total=round(total, 2),
        qtd_pedidos=qtd,
        por_loja={k: round(v, 2) for k, v in por_loja.items()},
        mensagem=mensagem,
    )
