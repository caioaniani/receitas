"""API legada pra integracao com bots externos via token.

HISTORICO: nasceu pro n8n (aposentado). O bot WhatsApp do dono NAO usa
esta API — ele vive em app/services/zapi_bot.py (webhook Z-API direto,
copilot read-only completo). Mantido apenas /api/bot/faturamento por
compatibilidade; se nada mais chamar, remover no futuro.

Auth: header `Authorization: Bearer <BOT_API_TOKEN>`. Sem session/login —
projetada pra ser chamada por workflows externos.

CSRF desabilitado pra esses endpoints (sao read-only e protegidos por token).
"""
import secrets as _secrets
from datetime import datetime, timedelta
from functools import wraps

from flask import current_app, jsonify, request

from app.blueprints.bot import bot_bp
from app.extensions import csrf
from app.utils import hoje as hoje_brt


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
    hoje = hoje_brt()
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

    # Mesma logica do PDV (expansao ate 7 dias) — chamadas paralelizadas
    # cabem no timeout do n8n. Captura pedidos sincronizados com atraso pela
    # OPAO PADARIA (sync em batch nos dias seguintes).
    # PDV/Seru: le do BANCO (VendaSeruDiaLoja), capturando o dia se faltar — nao
    # depende da API a cada request. Usa o TOTAL do pedido (inclui kit/box), a
    # mesma base de antes. Se a API estiver fora, cai pro ultimo snapshot.
    try:
        from app.services import vendas_diarias
        total_pdv, por_loja, qtd = vendas_diarias.faturamento_por_loja(target, target)
    except Exception as e:
        current_app.logger.exception('bot/faturamento: Seru falhou')
        return jsonify(ok=False, erro=f'falha ao buscar Seru: {type(e).__name__}'), 502

    # Site (loja propria / PedidoOnline) — best-effort: se a fonte do site
    # cair, NAO derruba o endpoint; devolve so o PDV com um aviso. Faturamento
    # por data de venda (pago_em), base subtotal sem frete (espelha o Seru).
    # VNDA foi APOSENTADO em 24/06/2026 — o site agora e o PedidoOnline; antes
    # este bloco consultava vnda_sync (fonte morta) e o site vinha sempre zerado.
    total_site = 0.0
    qtd_site = 0
    site_aviso = None
    try:
        from app.services import loja_online_vendas
        fat_site = loja_online_vendas.faturamento_por_dia(target, target)
        total_site = fat_site['total']
        qtd_site = fat_site['n_pedidos']
        if total_site > 0:
            por_loja['Site'] = por_loja.get('Site', 0) + total_site
    except Exception as e:  # noqa: BLE001
        current_app.logger.warning('bot/faturamento: site indisponivel: %s', e)
        site_aviso = 'site indisponível, total só PDV'

    total = total_pdv + total_site
    qtd_total = qtd + qtd_site

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
        linhas.append(f'_{qtd_total} venda(s)_')
        mensagem = '\n'.join(linhas)

    if site_aviso:
        mensagem = mensagem + f'\n_({site_aviso})_'
    if fallback_aplicado:
        mensagem = (f'_(Não entendi a data "{data_raw}", mostrando hoje)_\n\n' + mensagem)

    resp = jsonify(
        ok=True,
        data=target.isoformat(),
        total=round(total, 2),
        total_pdv=round(total_pdv, 2),
        total_site=round(total_site, 2),
        qtd_pedidos=qtd_total,
        por_loja={k: round(v, 2) for k, v in por_loja.items()},
        mensagem=mensagem,
    )
    resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    resp.headers['Pragma'] = 'no-cache'
    return resp
