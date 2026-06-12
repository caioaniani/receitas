"""Cliente fino da API do Chatwoot.

O Chatwoot é o inbox omnichannel (self-hosted, Railway). Este módulo só
EMPURRA dados pra lá — hoje, opcionalmente, atributos do contato (ex:
"Cliente B2B: Zion", "Débito: R$ 120,00") pra o atendente ver sem abrir o
card iframe. O recebimento de mensagens é responsabilidade do Chatwoot, não
deste sistema.

Config: CHATWOOT_URL, CHATWOOT_API_TOKEN, CHATWOOT_ACCOUNT_ID.
Fonte canônica de normalização de telefone: app.utils.telefone_chave.
"""
import logging

import requests
from flask import current_app

logger = logging.getLogger(__name__)


def disponivel():
    cfg = current_app.config
    return bool((cfg.get('CHATWOOT_URL') or '').strip()
                and (cfg.get('CHATWOOT_API_TOKEN') or '').strip()
                and (cfg.get('CHATWOOT_ACCOUNT_ID') or '').strip())


def _base():
    cfg = current_app.config
    url = (cfg.get('CHATWOOT_URL') or '').strip().rstrip('/')
    acc = (cfg.get('CHATWOOT_ACCOUNT_ID') or '').strip()
    return f'{url}/api/v1/accounts/{acc}'


def _headers():
    return {'api_access_token': (current_app.config.get('CHATWOOT_API_TOKEN') or '').strip(),
            'Content-Type': 'application/json'}


def atualizar_atributos_contato(contact_id, atributos):
    """PUT custom_attributes num contato existente. Retorna {'ok': bool}.

    `atributos` é um dict (ex: {'cliente_b2b': 'Zion', 'debito': '120.00'}).
    Best-effort: erro de rede não deve quebrar o fluxo de quem chama.
    """
    if not disponivel():
        return {'ok': False, 'erro': 'Chatwoot não configurado'}
    url = f'{_base()}/contacts/{contact_id}'
    try:
        r = requests.put(url, json={'custom_attributes': atributos},
                         headers=_headers(), timeout=10)
        if r.status_code not in (200, 201):
            logger.warning('chatwoot update contato %s: %s', r.status_code, r.text[:200])
            return {'ok': False, 'erro': f'HTTP {r.status_code}'}
        return {'ok': True}
    except Exception as exc:  # noqa: BLE001
        logger.exception('chatwoot atualizar_atributos_contato falhou')
        return {'ok': False, 'erro': str(exc)}


# ── Agent Bot (atendimento automatico) ──
# Usa o token do Agent Bot (CHATWOOT_BOT_TOKEN), nao o de usuario, pra as
# mensagens aparecerem como do bot. Webhook em app/blueprints/crm/routes.py.


def bot_disponivel():
    cfg = current_app.config
    return bool((cfg.get('CHATWOOT_URL') or '').strip()
                and (cfg.get('CHATWOOT_BOT_TOKEN') or '').strip()
                and (cfg.get('CHATWOOT_ACCOUNT_ID') or '').strip())


def _bot_headers():
    return {'api_access_token': (current_app.config.get('CHATWOOT_BOT_TOKEN') or '').strip(),
            'Content-Type': 'application/json'}


def enviar_mensagem(conversation_id, content):
    """Posta uma resposta do bot numa conversa. Retorna {'ok': bool}."""
    if not bot_disponivel():
        return {'ok': False, 'erro': 'Chatwoot bot nao configurado'}
    url = f'{_base()}/conversations/{conversation_id}/messages'
    try:
        r = requests.post(url, json={'content': content, 'message_type': 'outgoing'},
                          headers=_bot_headers(), timeout=10)
        if r.status_code not in (200, 201):
            logger.warning('chatwoot enviar_mensagem %s: %s', r.status_code, r.text[:200])
            return {'ok': False, 'erro': f'HTTP {r.status_code}'}
        return {'ok': True}
    except Exception as exc:  # noqa: BLE001
        logger.exception('chatwoot enviar_mensagem falhou')
        return {'ok': False, 'erro': str(exc)}


def definir_status(conversation_id, status):
    """Muda o status da conversa. 'open' = passa pro humano (sai do bot);
    'pending' = devolve pro bot; 'resolved' = encerra."""
    if not bot_disponivel():
        return {'ok': False, 'erro': 'Chatwoot bot nao configurado'}
    url = f'{_base()}/conversations/{conversation_id}/toggle_status'
    try:
        r = requests.post(url, json={'status': status},
                          headers=_bot_headers(), timeout=10)
        if r.status_code not in (200, 201):
            logger.warning('chatwoot definir_status %s: %s', r.status_code, r.text[:200])
            return {'ok': False, 'erro': f'HTTP {r.status_code}'}
        return {'ok': True}
    except Exception as exc:  # noqa: BLE001
        logger.exception('chatwoot definir_status falhou')
        return {'ok': False, 'erro': str(exc)}


def buscar_historico(conversation_id, limite=20):
    """Mensagens recentes da conversa, em ordem cronologica, mapeadas pra
    [{'role': 'user'|'assistant', 'content': str, 'imagens'?: [url]}] (pro
    Claude). Cliente = user (incoming), bot/atendente = assistant (outgoing).
    Ignora notas internas e eventos. Anexos de imagem do cliente entram em
    'imagens' (URLs do Chatwoot) — quem monta o prompt baixa via baixar_imagem.
    Mensagem so-imagem (sem texto) do cliente tambem entra."""
    if not bot_disponivel():
        return []
    url = f'{_base()}/conversations/{conversation_id}/messages'
    try:
        r = requests.get(url, headers=_bot_headers(), timeout=10)
        if r.status_code not in (200, 201):
            return []
        data = r.json() if r.text else {}
    except Exception:  # noqa: BLE001
        logger.exception('chatwoot buscar_historico falhou')
        return []

    msgs = data.get('payload') if isinstance(data, dict) else data
    if not isinstance(msgs, list):
        return []
    msgs = sorted(msgs, key=lambda m: m.get('created_at') or 0)

    hist = []
    for m in msgs:
        if m.get('private'):
            continue
        content = (m.get('content') or '').strip()
        mt = m.get('message_type')
        imagens = [a.get('data_url') for a in (m.get('attachments') or [])
                   if a.get('file_type') == 'image' and a.get('data_url')]
        if not content and not imagens:
            continue
        if mt in ('incoming', 0):
            item = {'role': 'user', 'content': content}
            if imagens:
                item['imagens'] = imagens
            hist.append(item)
        elif mt in ('outgoing', 1):
            if not content:
                continue  # imagem do bot/atendente nao precisa ir pro Claude
            hist.append({'role': 'assistant', 'content': content})
    return hist[-limite:]


def baixar_imagem(url):
    """Baixa um anexo de imagem do Chatwoot, comprime e devolve
    (media_type, base64), ou None se nao der (rede, formato nao suportado).

    So imagens — o Claude nao le audio/PDF por aqui. Sempre reencoda pra JPEG
    via comprimir_imagem (corrige rotacao de celular, limita o tamanho pra nao
    estourar o limite do Claude e mantem texto legivel em prints)."""
    if not url:
        return None
    if url.startswith('/'):
        base_url = (current_app.config.get('CHATWOOT_URL') or '').strip().rstrip('/')
        url = base_url + url
    try:
        r = requests.get(url, timeout=15)
        if r.status_code != 200 or not r.content:
            return None
    except Exception:  # noqa: BLE001
        logger.exception('chatwoot baixar_imagem falhou (download)')
        return None

    from app.utils import comprimir_imagem
    try:
        jpeg = comprimir_imagem(r.content, max_size=1568, quality=82)
    except ValueError:
        logger.warning('chatwoot baixar_imagem: formato nao suportado (%s)', url[:80])
        return None

    import base64
    return 'image/jpeg', base64.b64encode(jpeg).decode('ascii')


def diagnostico():
    """Bateria de checagens pro /admin/debug-chatwoot (owner-only).

    Roda DO SERVIDOR de prod — que alcanca o Chatwoot mesmo quando o
    navegador/app do atendente mascara o erro real. Distingue 3 familias
    de problema sem precisar adivinhar:
      1. hospedagem fora/doente (Railway do Chatwoot);
      2. token DESTE sistema pro Chatwoot invalido (401);
      3. servidor e tokens OK → problema e nos CANAIS Meta (tokens de
         WhatsApp/IG guardados DENTRO do Chatwoot — reconectar inboxes).
    Nao vaza tokens: so status HTTP e booleans."""
    import time as _time
    cfg = current_app.config
    url = (cfg.get('CHATWOOT_URL') or '').strip().rstrip('/')
    out = {
        'url_configurada': url or '(vazia)',
        'account_id': (cfg.get('CHATWOOT_ACCOUNT_ID') or '').strip()
                      or '(vazio)',
        'api_token_configurado': bool(
            (cfg.get('CHATWOOT_API_TOKEN') or '').strip()),
        'bot_token_configurado': bool(
            (cfg.get('CHATWOOT_BOT_TOKEN') or '').strip()),
    }
    if not url:
        out['conclusao'] = 'CHATWOOT_URL vazia no env — configure no Railway.'
        return out

    # 1. Servidor vivo? GET /api e endpoint publico de versao (sem auth).
    t0 = _time.monotonic()
    try:
        r = requests.get(f'{url}/api', timeout=8)
        out['servidor_http'] = r.status_code
        out['servidor_latencia_ms'] = int((_time.monotonic() - t0) * 1000)
        try:
            out['servidor_versao'] = (r.json() or {}).get('version')
        except ValueError:
            out['servidor_corpo'] = (r.text or '')[:120]
    except Exception as exc:  # noqa: BLE001
        out['servidor_http'] = None
        out['servidor_erro'] = f'{type(exc).__name__}: {str(exc)[:200]}'
        out['conclusao'] = (
            'Servidor Chatwoot NAO respondeu — problema na hospedagem '
            '(projeto Railway do Chatwoot): conferir se web/worker estao '
            'verdes, logs de crash, e disco/memoria de Postgres e Redis.')
        return out

    # 2. Token de usuario (o que empurra atributos de contato) valido?
    if out['api_token_configurado']:
        try:
            r = requests.get(f'{_base()}/contacts', params={'page': 1},
                             headers=_headers(), timeout=8)
            out['api_token_http'] = r.status_code
        except Exception as exc:  # noqa: BLE001
            out['api_token_http'] = None
            out['api_token_erro'] = f'{type(exc).__name__}: {str(exc)[:200]}'

    # 3. Token do Agent Bot (o que responde conversas) valido?
    if out['bot_token_configurado']:
        try:
            r = requests.get(f'{_base()}/conversations',
                             params={'status': 'pending', 'page': 1},
                             headers=_bot_headers(), timeout=8)
            out['bot_token_http'] = r.status_code
        except Exception as exc:  # noqa: BLE001
            out['bot_token_http'] = None
            out['bot_token_erro'] = f'{type(exc).__name__}: {str(exc)[:200]}'

    # Conclusao automatica (ordem importa: do mais grave pro mais fino)
    statuses = [out.get('servidor_http'), out.get('api_token_http'),
                out.get('bot_token_http')]
    if any(s and s >= 500 for s in statuses):
        out['conclusao'] = (
            'Chatwoot respondeu com erro 5xx — servidor doente. Ver logs '
            'no Railway do Chatwoot; suspeitos comuns: Sidekiq parado, '
            'Redis/Postgres cheios, migracao pendente apos update. '
            'Explica os "unexpected error" do app dos atendentes.')
    elif (out.get('servidor_latencia_ms') or 0) > 5000:
        out['conclusao'] = (
            'Chatwoot respondeu mas MUITO lento (%sms) — servidor sob '
            'stress (memoria/CPU no Railway do Chatwoot). Explica '
            '"unexpected error" e app travado.'
            % out['servidor_latencia_ms'])
    elif any(s == 401 for s in statuses[1:]):
        out['conclusao'] = (
            'Servidor OK, mas o token DESTE sistema pro Chatwoot esta '
            'invalido (401) — regerar em Profile Settings (API) ou '
            'Agent Bots (bot) e atualizar o env no Railway da padaria.')
    elif out.get('servidor_http') == 200:
        out['conclusao'] = (
            'Servidor Chatwoot OK e tokens OK. O problema esta nos '
            'CANAIS (Meta): "400 Session Invalid" no IG + "Falha ao '
            'enviar" no WhatsApp = token Meta expirado/revogado DENTRO '
            'do Chatwoot. Reconectar em Settings → Inboxes (Reauthorize '
            'no Instagram e no WhatsApp). Pra nao repetir a cada 60 '
            'dias: usar token de System User do Business Manager (nao '
            'expira).')
    return out


def listar_conversas_paradas(min_minutos=15, limite=50):
    """Conversas em status `pending` (turno do bot) cujo `last_activity_at` foi
    ha mais de `min_minutos`. Usado pelo job de detecao de abandono.

    Retorna lista de {'id', 'nome_contato', 'minutos_paradas'}. Lista vazia se
    o Chatwoot nao estiver configurado ou se a chamada falhar."""
    if not bot_disponivel():
        return []
    url = f'{_base()}/conversations'
    try:
        r = requests.get(url, headers=_bot_headers(),
                         params={'status': 'pending', 'page': 1},
                         timeout=15)
        if r.status_code not in (200, 201):
            logger.warning('chatwoot listar_conversas_paradas %s: %s',
                           r.status_code, r.text[:200])
            return []
        data = r.json() if r.text else {}
    except Exception:  # noqa: BLE001
        logger.exception('chatwoot listar_conversas_paradas falhou')
        return []

    payload = (data.get('data') or {}).get('payload') if isinstance(data, dict) else None
    if not isinstance(payload, list):
        payload = data if isinstance(data, list) else []

    import time as _time
    agora_epoch = _time.time()
    paradas = []
    for c in payload:
        if not isinstance(c, dict):
            continue
        ult = c.get('last_activity_at') or c.get('updated_at') or c.get('created_at')
        if not ult:
            continue
        try:
            ult_epoch = float(ult)
        except (TypeError, ValueError):
            continue
        minutos = (agora_epoch - ult_epoch) / 60.0
        if minutos < min_minutos:
            continue
        meta = c.get('meta') or {}
        sender = meta.get('sender') or {}
        paradas.append({
            'id': c.get('id'),
            'nome_contato': sender.get('name') or '',
            'minutos_paradas': int(minutos),
        })
    paradas.sort(key=lambda p: -p['minutos_paradas'])
    return paradas[:limite]
