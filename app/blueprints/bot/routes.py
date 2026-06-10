"""API pra integracao com bots externos (n8n / WhatsApp).

Auth: header `Authorization: Bearer <BOT_API_TOKEN>`. Sem session/login —
projetada pra ser chamada por workflows externos.

CSRF desabilitado pra esses endpoints (sao read-only e protegidos por token).
"""
import secrets as _secrets
from datetime import datetime, timedelta
from functools import wraps

from flask import current_app, jsonify, request

from app.blueprints.bot import bot_bp
from app.extensions import csrf, db
from app.services import seru
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
    BOT_MAX_DIAS_EXTRA = 7
    dias_extra = min(max(0, (hoje - target).days), BOT_MAX_DIAS_EXTRA) if target < hoje else 0
    try:
        pedidos = seru.listar_pedidos_completo(target, target, expandir_dias_frente=dias_extra)
    except Exception as e:
        current_app.logger.exception('bot/faturamento: Seru falhou')
        return jsonify(ok=False, erro=f'falha ao buscar Seru: {type(e).__name__}'), 502

    total_pdv = 0.0
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
        total_pdv += valor
        qtd += 1
        comp = p.get('company') or {}
        nome = (comp.get('name') if isinstance(comp, dict) else None) or '—'
        por_loja[nome] = por_loja.get(nome, 0) + valor

    # Site (VNDA) — best-effort: se a API do site cair, NAO derruba o endpoint;
    # devolve so o PDV com um aviso. Faturamento por data de venda (espelha o Seru).
    total_site = 0.0
    qtd_site = 0
    site_aviso = None
    try:
        from app.services import vnda_sync
        fat_site = vnda_sync.faturamento_por_dia(target, target)
        total_site = fat_site['total']
        qtd_site = fat_site['n_pedidos']
        if total_site > 0:
            loja_v = vnda_sync.loja_vnda()
            nome_site = (loja_v.nome if loja_v else 'Site') + ' (site)'
            por_loja[nome_site] = por_loja.get(nome_site, 0) + total_site
    except Exception as e:  # noqa: BLE001
        current_app.logger.warning('bot/faturamento: VNDA indisponivel: %s', e)
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


# ── Power (WhatsApp pessoal do dono) — copilot READ-ONLY ──
#
# Decisao de 10/06/2026: o Power ganha acesso ao copilot_svc inteiro, mas
# SO leitura — apenas_leitura=True remove todas as tools de write antes do
# Claude ver. Auth: BOT_API_TOKEN + whitelist BOT_ALLOWED_PHONES (igual ao
# faturamento). Multi-turn por telefone reusando ChatbotConversa.

@bot_bp.route('/api/bot/copilot', methods=['POST'])
@csrf.exempt
@_bot_auth_required
def copilot():
    """POST /api/bot/copilot

    Body JSON: {"mensagem": "..."} (telefone vai por ?telefone= como o resto).
    Resposta: {ok: bool, resposta: str, tipo: 'conversa'|'tool:<nome>'|'erro'}.
    """
    import json as _json

    from app.models import ChatbotConversa, Usuario
    from app.services import copilot as copilot_svc

    body = request.get_json(silent=True) or {}
    mensagem = (body.get('mensagem') or body.get('texto')
                or body.get('prompt') or '').strip()
    if not mensagem:
        return jsonify(ok=False, erro='mensagem obrigatoria'), 400

    telefone = _normalizar_telefone(request.args.get('telefone') or '')
    # Quem responde com o copilot precisa de Usuario pra filtrar tools pelo
    # papel. Whitelist do Power so tem o dono — usa o primeiro owner ativo.
    user = (Usuario.query
            .filter(Usuario.is_owner.is_(True))
            .order_by(Usuario.id.asc()).first())
    if not user:
        return jsonify(ok=False,
                       erro='nenhum usuario owner ativo no sistema'), 503

    # Memoria multi-turn por telefone (reusa ChatbotConversa, generica).
    conv_id = f'wpp-power-{telefone or "anon"}'
    conv = ChatbotConversa.query.filter_by(conv_id=conv_id).first()
    if not conv:
        conv = ChatbotConversa(conv_id=conv_id, mensagens_json='[]')
        db.session.add(conv)
    try:
        historico = _json.loads(conv.mensagens_json or '[]')
    except (ValueError, TypeError):
        historico = []
    historico = historico[-20:]   # cap

    res = copilot_svc.interpretar(mensagem, user, historico=historico,
                                  apenas_leitura=True)
    tipo = res.get('tipo') or 'conversa'
    explicacao = res.get('explicacao') or ''

    if tipo == 'erro':
        return jsonify(ok=False, tipo='erro', resposta=explicacao or
                       'copilot indisponivel'), 503

    # Read tool ja executou — concatena explicacao + texto formatado.
    resultado = res.get('resultado') or {}
    extra = ''
    if isinstance(resultado, dict):
        extra = resultado.get('texto') or resultado.get('mensagem') or ''
    resposta = '\n\n'.join(p for p in (explicacao, extra) if p).strip()
    if not resposta:
        resposta = 'OK.'

    # Persiste o turno na memoria (ultimas 40 mensagens — janela do interpret
    # ja limita a 20 mas guardamos um pouco mais pra ter folga).
    historico.append({'role': 'user', 'content': mensagem})
    historico.append({'role': 'assistant', 'content': resposta})
    conv.mensagens_json = _json.dumps(historico[-40:], ensure_ascii=False)
    db.session.commit()

    return jsonify(ok=True, tipo=f'tool:{tipo}' if tipo != 'conversa'
                   else 'conversa', resposta=resposta)


@bot_bp.route('/api/bot/copilot/reset', methods=['POST'])
@csrf.exempt
@_bot_auth_required
def copilot_reset():
    """Limpa a memoria de conversa do Power (?telefone=...)."""
    from app.models import ChatbotConversa
    telefone = _normalizar_telefone(request.args.get('telefone') or '')
    conv_id = f'wpp-power-{telefone or "anon"}'
    n = ChatbotConversa.query.filter_by(conv_id=conv_id).delete()
    db.session.commit()
    return jsonify(ok=True, apagada=bool(n))
