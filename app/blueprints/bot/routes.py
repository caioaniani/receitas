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
    data_raw = (request.args.get('data') or 'hoje').strip()
    data_str = data_raw.lower()
    hoje = date.today()
    target = None
    fallback_aplicado = False

    if data_str in ('hoje', 'today', ''):
        target = hoje
    elif data_str in ('ontem', 'yesterday'):
        target = hoje - timedelta(days=1)
    elif data_str in ('anteontem',):
        target = hoje - timedelta(days=2)
    else:
        # Tenta varios formatos comuns. Pega so a parte da data se vier
        # algo tipo '2026-05-07T17:42:36' ou '07/05/2026 17:42'.
        candidato = data_raw.split('T')[0].split(' ')[0].strip()
        for fmt in ('%Y-%m-%d', '%d/%m/%Y', '%d-%m-%Y', '%Y/%m/%d',
                    '%d/%m/%y', '%d.%m.%Y', '%d %m %Y'):
            try:
                target = datetime.strptime(candidato, fmt).date()
                break
            except ValueError:
                continue
        if target is None:
            # Ultimo recurso: assume hoje e marca aviso
            target = hoje
            fallback_aplicado = True

    # Bot prioriza resposta rapida (n8n/AI Agent tem timeout). 1 chamada Seru
    # apenas — pedidos cancelados/atualizados em dias posteriores podem nao
    # aparecer. Pra auditoria completa, usar /pdv/ no site.
    debug_paginas = []
    try:
        pedidos = seru.listar_pedidos_completo(target, target, expandir_dias_frente=0,
                                                debug=debug_paginas)
    except Exception as e:
        current_app.logger.exception('bot/faturamento: Seru falhou')
        return jsonify(ok=False, erro=f'falha ao buscar Seru: {type(e).__name__}'), 502
    current_app.logger.info('bot/faturamento %s: %s', target.isoformat(), debug_paginas)

    target_iso = target.isoformat()
    total = 0.0
    por_loja = {}
    qtd = 0
    for p in pedidos:
        if not isinstance(p, dict):
            continue
        if p.get('canceledAt'):
            continue
        # createdAt da Seru e UTC; convertemos pra BRT pra comparar com a data local
        if seru.data_local(p.get('createdAt')) != target:
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

    if fallback_aplicado:
        mensagem = (f'_(Não entendi a data "{data_raw}", mostrando hoje)_\n\n' + mensagem)

    resp = jsonify(
        ok=True,
        data=target_iso,
        data_recebida=data_raw,
        fallback_aplicado=fallback_aplicado,
        total=round(total, 2),
        qtd_pedidos=qtd,
        qtd_pedidos_brutos=len(pedidos),
        por_loja={k: round(v, 2) for k, v in por_loja.items()},
        debug_paginas=debug_paginas,
        mensagem=mensagem,
    )
    resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    resp.headers['Pragma'] = 'no-cache'
    return resp
