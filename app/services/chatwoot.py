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
    from app.services import instancia as _inst
    if not _inst.pode_falar_com_o_mundo('chatwoot'):
        return {'ok': False, 'suprimido_instancia': True, 'erro': 'instancia nao canonica'}
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


# ── Painel (atendimento humano via /entregas/painel-testes) ──
# Usa token de USUARIO de um agente dedicado no Chatwoot ("Painel"). Mensagens
# postadas com esse token aparecem como esse agente — distinguivel do bot
# (que usa CHATWOOT_BOT_TOKEN) e dos 12 atendentes. Setup: criar agente
# "Painel" no Chatwoot, gerar token (Profile Settings → Access Token), pôr em
# CHATWOOT_PAINEL_TOKEN no Railway.


def painel_disponivel():
    cfg = current_app.config
    return bool((cfg.get('CHATWOOT_URL') or '').strip()
                and (cfg.get('CHATWOOT_PAINEL_TOKEN') or '').strip()
                and (cfg.get('CHATWOOT_ACCOUNT_ID') or '').strip())


def _painel_headers():
    return {'api_access_token':
            (current_app.config.get('CHATWOOT_PAINEL_TOKEN') or '').strip(),
            'Content-Type': 'application/json'}


def enviar_mensagem_painel(conversation_id, content):
    """Posta uma mensagem na conversa como o agente 'Painel' (NAO o bot).

    Cuidado: a mensagem aparece como esse agente humano no Chatwoot, com
    nome+foto proprios. Erro = nao envia (cliente nao recebe resposta dupla)."""
    from app.services import instancia as _inst
    if not _inst.pode_falar_com_o_mundo('chatwoot'):
        return {'ok': False, 'suprimido_instancia': True, 'erro': 'instancia nao canonica'}
    if not painel_disponivel():
        return {'ok': False, 'erro': 'CHATWOOT_PAINEL_TOKEN nao configurado'}
    url = f'{_base()}/conversations/{conversation_id}/messages'
    try:
        r = requests.post(url, json={'content': content, 'message_type': 'outgoing'},
                          headers=_painel_headers(), timeout=10)
        if r.status_code not in (200, 201):
            logger.warning('chatwoot enviar_mensagem_painel %s: %s',
                           r.status_code, (r.text or '')[:200])
            return {'ok': False, 'erro': f'HTTP {r.status_code}'}
        return {'ok': True}
    except Exception as exc:  # noqa: BLE001
        logger.exception('chatwoot enviar_mensagem_painel falhou')
        return {'ok': False, 'erro': str(exc)}


def enviar_mensagem(conversation_id, content):
    """Posta uma resposta do bot numa conversa. Retorna {'ok': bool}.

    Guarda de INSTÂNCIA CANÔNICA (20/08/2026): o Chatwoot é EXTERNO e
    compartilhado — uma cópia de homologação com as mesmas envs enxerga as
    conversas REAIS e responderia ao cliente em dobro (caso Lissa, 19/08:
    contenção duplicada no Instagram). Ver app/services/instancia.py.
    """
    from app.services import instancia as _inst
    if not _inst.pode_falar_com_o_mundo('chatwoot'):
        return {'ok': False, 'suprimido_instancia': True,
                'erro': 'instancia nao canonica — envio suprimido'}
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


def definir_status(conversation_id, status, tentativas=3):
    """Muda o status da conversa. 'open' = passa pro humano (sai do bot);
    'pending' = devolve pro bot; 'resolved' = encerra.

    Retry (default 3x, backoff 0.5s/1s) porque ESTE caminho e critico pro
    handoff: uma falha transitoria aqui deixava a conversa presa no bot e o
    cliente esperando (caso 23/06/2026). Devolve {'ok': bool, 'erro': str} —
    o caller DEVE checar e NAO pode silenciar a falha."""
    from app.services import instancia as _inst
    if not _inst.pode_falar_com_o_mundo('chatwoot'):
        return {'ok': False, 'suprimido_instancia': True, 'erro': 'instancia nao canonica — status nao alterado'}
    if not bot_disponivel():
        return {'ok': False, 'erro': 'Chatwoot bot nao configurado'}
    url = f'{_base()}/conversations/{conversation_id}/toggle_status'
    ultimo_erro = '?'
    for tentativa in range(1, tentativas + 1):
        try:
            r = requests.post(url, json={'status': status},
                              headers=_bot_headers(), timeout=10)
            if r.status_code in (200, 201):
                return {'ok': True}
            ultimo_erro = f'HTTP {r.status_code}'
            logger.warning('chatwoot definir_status %s (tentativa %d/%d): %s',
                           r.status_code, tentativa, tentativas, r.text[:200])
        except Exception as exc:  # noqa: BLE001
            ultimo_erro = str(exc)
            logger.warning('chatwoot definir_status erro (tentativa %d/%d): %s',
                           tentativa, tentativas, exc)
        if tentativa < tentativas:
            import time as _t
            _t.sleep(0.5 * tentativa)
    logger.error('chatwoot definir_status FALHOU apos %d tentativas: %s',
                 tentativas, ultimo_erro)
    return {'ok': False, 'erro': ultimo_erro}


def buscar_historico(conversation_id, limite=20):
    """Mensagens recentes da conversa, em ordem cronologica, mapeadas pra
    [{'role': 'user'|'assistant', 'content': str, 'imagens'?: [url]}] (pro
    Claude). Cliente = user (incoming), bot/atendente = assistant (outgoing).
    Ignora notas internas e eventos. Anexos de imagem do cliente entram em
    'imagens' (URLs do Chatwoot) — quem monta o prompt baixa via baixar_imagem.
    Mensagem so-imagem (sem texto) do cliente tambem entra.

    Auth: prefere o token de USUARIO (CHATWOOT_API_TOKEN) pra leitura —
    o token de bot tem permissao mais limitada. Caso real (12/06/2026):
    durante o incidente do Chatwoot o bot_token estava 401 e o historico
    voltava vazio; com fallback pro de usuario, a leitura segue. So o
    envio (enviar_mensagem) precisa do token de bot (pra aparecer como
    bot na conversa)."""
    if disponivel():
        headers = _headers()
    elif bot_disponivel():
        headers = _bot_headers()
    else:
        return []
    url = f'{_base()}/conversations/{conversation_id}/messages'
    try:
        r = requests.get(url, headers=headers, timeout=10)
        if r.status_code not in (200, 201):
            logger.warning('chatwoot buscar_historico %s: %s',
                           r.status_code, (r.text or '')[:200])
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
        # `created_at` (epoch UTC) entra como campo EXTRA — callers antigos
        # ignoram, a UI usa pra mostrar HH:MM em cada bolha. Safe pra todos.
        ts = m.get('created_at')
        try:
            ts = int(ts) if ts is not None else None
        except (TypeError, ValueError):
            ts = None
        if mt in ('incoming', 0):
            item = {'role': 'user', 'content': content, 'created_at': ts}
            if imagens:
                item['imagens'] = imagens
            hist.append(item)
        elif mt in ('outgoing', 1):
            if not content:
                continue  # imagem do bot/atendente nao precisa ir pro Claude
            hist.append({'role': 'assistant', 'content': content,
                         'created_at': ts})
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
    api_tok = (cfg.get('CHATWOOT_API_TOKEN') or '').strip()
    bot_tok = (cfg.get('CHATWOOT_BOT_TOKEN') or '').strip()
    out = {
        'url_configurada': url or '(vazia)',
        'account_id': (cfg.get('CHATWOOT_ACCOUNT_ID') or '').strip()
                      or '(vazio)',
        'api_token_configurado': bool(api_tok),
        'bot_token_configurado': bool(bot_tok),
        # Forense de copia-e-cola SEM expor o valor: tokens do Chatwoot
        # tem ~24 chars alfanumericos. len muito diferente = colou outra
        # coisa (ex: a Outgoing URL do bot, o ID, o secret). Caso real
        # 12/06/2026: bot_token seguia 401 apos o dono 'ja ter colado'.
        'api_token_len': len(api_tok),
        'bot_token_len': len(bot_tok),
        'bot_token_parece_url': bot_tok.lower().startswith('http'),
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
    # Sonda: POST toggle_status numa conversa IMPOSSIVEL (id 0). Token
    # valido → 404 (nao achou a conversa); token invalido → 401. NAO da
    # pra usar GET /conversations: token de Agent Bot nao tem permissao
    # de listar e devolve 401 mesmo VALIDO — falso alarme real de
    # 12/06/2026 (diag acusava bot 401 enquanto o bot postava respostas
    # normalmente; o dono re-colou token certo 2x atras do proprio rabo).
    if out['bot_token_configurado']:
        try:
            r = requests.post(f'{_base()}/conversations/0/toggle_status',
                              json={'status': 'open'},
                              headers=_bot_headers(), timeout=8)
            out['bot_token_http'] = r.status_code
            out['bot_token_ok'] = r.status_code != 401
        except Exception as exc:  # noqa: BLE001
            out['bot_token_http'] = None
            out['bot_token_ok'] = None
            out['bot_token_erro'] = f'{type(exc).__name__}: {str(exc)[:200]}'

    # 4. Saude dos CANAIS (inboxes): o payload de /inboxes traz
    # `reauthorization_required` quando o token Meta do canal morreu —
    # exatamente o "400 Session Invalid" do IG. Requer o token de
    # USUARIO (o de bot nao tem permissao pra listar inboxes).
    if out.get('api_token_http') == 200:
        try:
            r = requests.get(f'{_base()}/inboxes', headers=_headers(),
                             timeout=8)
            if r.status_code == 200:
                data = r.json() if r.text else {}
                payload = (data.get('payload')
                           if isinstance(data, dict) else data) or []
                out['inboxes'] = [
                    {'nome': ib.get('name'),
                     'canal': ib.get('channel_type'),
                     'precisa_reautorizar': bool(
                         ib.get('reauthorization_required'))}
                    for ib in payload if isinstance(ib, dict)]
        except Exception as exc:  # noqa: BLE001
            out['inboxes_erro'] = f'{type(exc).__name__}: {str(exc)[:200]}'

    # Conclusao automatica (ordem importa: do mais grave pro mais fino)
    statuses = [out.get('servidor_http'), out.get('api_token_http'),
                out.get('bot_token_http')]
    quebrados = [ib['nome'] for ib in out.get('inboxes', [])
                 if ib['precisa_reautorizar']]
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
    elif quebrados:
        out['conclusao'] = (
            'Canal(is) com token Meta morto, precisa REAUTORIZAR no '
            'Chatwoot (Settings → Inboxes → Reauthorize): %s. Pra nao '
            'repetir a cada 60 dias, conectar com token de System User '
            'do Business Manager (nao expira).' % ', '.join(quebrados))
    elif (out.get('api_token_http') == 401
          or out.get('bot_token_ok') is False):
        qual = []
        if out.get('api_token_http') == 401:
            qual.append('CHATWOOT_API_TOKEN (regerar em Profile Settings)')
        if out.get('bot_token_ok') is False:
            qual.append('CHATWOOT_BOT_TOKEN (copiar em /super_admin → '
                        'Agent Bots → Access Token)')
        out['conclusao'] = (
            'Servidor OK, mas token DESTE sistema pro Chatwoot esta '
            'invalido (401): %s. Atualizar o env no Railway da padaria.'
            % '; '.join(qual))
    elif out.get('servidor_http') == 200:
        out['conclusao'] = (
            'Servidor, tokens e inboxes OK pela API. Se atendente ainda '
            've "Falha ao enviar": olhar a janela de 24h da Meta '
            '(mensagem livre so ate 24h apos a ultima msg do cliente; '
            'fora disso exige template aprovado) e os logs do worker '
            '(Sidekiq) no Railway do Chatwoot.')

    # Flag de maquina pro vigia de infra (nao depende do texto da
    # conclusao). Token NAO configurado nao conta como doente — e estado
    # de configuracao, alertar a cada 15min viraria spam. Bot token:
    # so 401 e doente (404 da sonda = token valido).
    out['saudavel'] = (
        out.get('servidor_http') == 200
        and (out.get('servidor_latencia_ms') or 0) <= 5000
        and not any(s and s >= 500 for s in statuses)
        and out.get('api_token_http') != 401
        and out.get('bot_token_ok') is not False
        and not quebrados
    )
    return out


# Estado do vigia de infra persistido no banco (AppConfig key-value).
# Em memoria era perigoso: cada restart do worker / cada deploy resetava
# o estado e a proxima execucao do cron re-alertava o mesmo problema —
# foi exatamente o que aconteceu em 12/06/2026 (dono recebeu o mesmo
# aviso 2x em segundos durante uma janela de deploys). Persistir
# garante anti-spam mesmo entre processos e deploys.
_VIGIA_KEY_QUEBRADO = 'vigia_chatwoot_quebrado_desde'
_VIGIA_KEY_ULTIMO = 'vigia_chatwoot_ultimo_alerta_em'
_VIGIA_KEY_ASSIN = 'vigia_chatwoot_ultima_assinatura'
_VIGIA_REALERTA_MIN = 360   # re-alerta do MESMO problema a cada 6h


def _vigia_carregar():
    from datetime import datetime as _dt

    from app.models import AppConfig
    def _parse_dt(s):
        if not s:
            return None
        try:
            return _dt.fromisoformat(s)
        except ValueError:
            return None
    return {
        'quebrado_desde': _parse_dt(AppConfig.get(_VIGIA_KEY_QUEBRADO)),
        'ultimo_alerta_em': _parse_dt(AppConfig.get(_VIGIA_KEY_ULTIMO)),
        'ultima_assinatura': AppConfig.get(_VIGIA_KEY_ASSIN),
    }


def _vigia_gravar(est):
    from app.extensions import db
    from app.models import AppConfig
    def _fmt(v):
        return v.isoformat() if v else None
    AppConfig.set(_VIGIA_KEY_QUEBRADO, _fmt(est['quebrado_desde']))
    AppConfig.set(_VIGIA_KEY_ULTIMO, _fmt(est['ultimo_alerta_em']))
    AppConfig.set(_VIGIA_KEY_ASSIN, est['ultima_assinatura'])
    db.session.commit()


def vigiar_infra():
    """Roda o diagnostico e alerta o dono no WhatsApp (Z-API) quando o
    Chatwoot adoece. Estado persistido em AppConfig (anti-spam mesmo
    entre workers/deploys). Anti-spam: alerta na TRANSICAO
    saudavel→doente, re-alerta o mesmo problema a cada 6h, e avisa uma
    vez quando normalizar."""
    from app.services import zapi
    from app.utils import agora as _agora

    cfg = current_app.config
    if not (cfg.get('CHATWOOT_URL') or '').strip():
        return {'rodou': False, 'motivo': 'chatwoot nao configurado'}
    # Destino: CHATWOOT_VIGIA_INFRA_NUMERO (aceita ID de grupo
    # '...-group' — decisao do dono 12/06/2026: vigias vao pro grupo da
    # equipe, digests continuam no privado) com fallback pro numero
    # pessoal do dono.
    dono = ((cfg.get('CHATWOOT_VIGIA_INFRA_NUMERO') or '').strip()
            or (cfg.get('ZAPI_BOT_DONO_NUMERO') or '').strip())
    if not dono:
        return {'rodou': False, 'motivo': 'ZAPI_BOT_DONO_NUMERO vazio'}

    out = diagnostico()
    est = _vigia_carregar()
    agora_dt = _agora()

    if out.get('saudavel'):
        if est['quebrado_desde'] is not None:
            zapi.enviar_texto(dono, ('✅ Chatwoot normalizou — servidor, '
                                     'tokens e canais OK de novo.'))
            _vigia_gravar({'quebrado_desde': None,
                           'ultimo_alerta_em': None,
                           'ultima_assinatura': None})
            return {'rodou': True, 'enviado': True, 'tipo': 'recuperacao'}
        return {'rodou': True, 'enviado': False, 'tipo': 'saudavel'}

    assinatura = out.get('conclusao') or 'problema desconhecido'
    mudou = assinatura != est['ultima_assinatura']
    venceu = (est['ultimo_alerta_em'] is None
              or (agora_dt - est['ultimo_alerta_em']).total_seconds()
              >= _VIGIA_REALERTA_MIN * 60)
    if est['quebrado_desde'] is None:
        est['quebrado_desde'] = agora_dt
    if mudou or venceu:
        msg = ('⚠️ Chatwoot com problema (vigia automatico):\n\n'
               f'{assinatura}\n\n'
               'Detalhe: /admin/debug-chatwoot')
        zapi.enviar_texto(dono, msg)
        est['ultimo_alerta_em'] = agora_dt
        est['ultima_assinatura'] = assinatura
        _vigia_gravar(est)
        return {'rodou': True, 'enviado': True, 'tipo': 'alerta'}
    _vigia_gravar(est)
    return {'rodou': True, 'enviado': False, 'tipo': 'throttle'}


def erros_de_envio(conversation_id, limite=10):
    """Mensagens que FALHARAM numa conversa + o erro bruto que o canal
    (Meta) devolveu — o Chatwoot guarda em content_attributes.
    external_error. Responde 'por que a mensagem da atendente nao foi?'
    sem depender de alguem clicar no ⚠️ no app. Usa o token de USUARIO
    (CHATWOOT_API_TOKEN)."""
    if not disponivel():
        return {'ok': False, 'erro': 'Chatwoot nao configurado'}
    url = f'{_base()}/conversations/{conversation_id}/messages'
    try:
        r = requests.get(url, headers=_headers(), timeout=10)
        if r.status_code != 200:
            return {'ok': False, 'erro': f'HTTP {r.status_code}'}
        data = r.json() if r.text else {}
    except Exception as exc:  # noqa: BLE001
        return {'ok': False, 'erro': f'{type(exc).__name__}: {str(exc)[:200]}'}
    msgs = data.get('payload') if isinstance(data, dict) else data
    if not isinstance(msgs, list):
        return {'ok': False, 'erro': 'payload inesperado'}
    falhas = []
    for m in sorted(msgs, key=lambda x: x.get('created_at') or 0):
        if not isinstance(m, dict):
            continue
        ca = m.get('content_attributes') or {}
        if m.get('status') == 'failed' or ca.get('external_error'):
            falhas.append({
                'mensagem': (m.get('content') or '')[:80],
                'criada_em': m.get('created_at'),
                'status': m.get('status'),
                'erro_canal': ca.get('external_error') or '(sem detalhe)',
            })
    return {'ok': True, 'qtd_falhas': len(falhas),
            'falhas': falhas[-limite:]}


def listar_conversas_paradas(min_minutos=15, limite=50, status='pending'):
    """Conversas no `status` dado cujo `last_activity_at` foi ha mais de
    `min_minutos`. Usos:
    - status='pending' (default): detector de abandono + follow-up do bot
      (turno do bot, cliente sumiu).
    - status='open': detector de CLIENTE ESPERANDO HUMANO (12/06/2026,
      conv #198 — cliente mandou 'Olá' em conversa open e ninguem viu;
      bot ignora open por design, mas o dono precisa saber).

    Retorna lista de {'id', 'nome_contato', 'minutos_paradas'}. Lista vazia se
    o Chatwoot nao estiver configurado ou se a chamada falhar.

    Auth: token de USUARIO (com fallback pro de bot). Token de Agent Bot
    NAO pode listar conversas (401) — descoberto em 12/06/2026: este
    detector ficou cego em silencio o tempo todo em que so o bot token
    existia (o 401 virava lista vazia sem alarde)."""
    if disponivel():
        headers = _headers()
    elif bot_disponivel():
        headers = _bot_headers()
    else:
        return []
    url = f'{_base()}/conversations'
    try:
        r = requests.get(url, headers=headers,
                         params={'status': status, 'page': 1},
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
            # Telefone cru do contato (a vassoura canoniza) — sem ele o
            # `responder` da vassoura rodava sem autorizacao de pedido e a
            # busca por telefone nao funcionava nesse caminho (19/07/2026).
            'telefone': (sender.get('phone_number')
                         or sender.get('identifier') or ''),
            'minutos_paradas': int(minutos),
        })
    paradas.sort(key=lambda p: -p['minutos_paradas'])
    return paradas[:limite]


def listar_conversas(status='open', limite=40):
    """Conversas no `status` (open/pending/resolved/all), com dados pra UI:
    contato, preview da ultima mensagem, status, canal, quando, nao-lidas.
    Ordena por atividade mais recente. Lista vazia se Chatwoot indisponivel
    ou erro (a UI trata como 'sem conversas', nunca quebra).

    Leitura: token de USUARIO (CHATWOOT_API_TOKEN), fallback pro de bot.
    LICAO DURA (CLAUDE.md): token de Agent Bot NAO lista conversas (401) — dai
    a preferencia pelo de usuario; so com o de bot a lista volta vazia em
    silencio."""
    if disponivel():
        headers = _headers()
    elif bot_disponivel():
        headers = _bot_headers()
    else:
        return []
    params = {'page': 1}
    if status and status != 'all':
        params['status'] = status
    url = f'{_base()}/conversations'
    try:
        r = requests.get(url, headers=headers, params=params, timeout=15)
        if r.status_code not in (200, 201):
            logger.warning('chatwoot listar_conversas %s: %s',
                           r.status_code, (r.text or '')[:200])
            return []
        data = r.json() if r.text else {}
    except Exception:  # noqa: BLE001
        logger.exception('chatwoot listar_conversas falhou')
        return []

    payload = (data.get('data') or {}).get('payload') if isinstance(data, dict) else None
    if not isinstance(payload, list):
        payload = data if isinstance(data, list) else []

    out = []
    for c in payload:
        if not isinstance(c, dict):
            continue
        meta = c.get('meta') or {}
        sender = meta.get('sender') or {}
        # Preview da ultima mensagem: last_non_activity_message, senao a ultima
        # do array `messages` que tenha texto (a API varia por versao).
        preview = ''
        ultima = c.get('last_non_activity_message')
        if isinstance(ultima, dict):
            preview = (ultima.get('content') or '').strip()
        if not preview:
            for m in reversed(c.get('messages') or []):
                if isinstance(m, dict) and (m.get('content') or '').strip():
                    preview = m['content'].strip()
                    break
        out.append({
            'id': c.get('id'),
            'contato': sender.get('name') or 'Sem nome',
            'status': c.get('status') or status,
            'canal': meta.get('channel') or '',
            'preview': preview[:90],
            'ultima_em': c.get('last_activity_at') or c.get('timestamp') or 0,
            'nao_lidas': c.get('unread_count') or 0,
        })
    out.sort(key=lambda d: d.get('ultima_em') or 0, reverse=True)
    return out[:limite]


# ── Iniciar conversa no WhatsApp (botao "Chamar cliente", 11/07/2026) ──────
# Fora da janela de 24h a Meta so deixa a EMPRESA iniciar com TEMPLATE
# aprovado. Fluxo: acha/cria o contato pelo telefone -> reusa conversa aberta
# na inbox do WhatsApp OU cria uma nova + manda o template. Tudo com o token
# de USUARIO (CHATWOOT_API_TOKEN) — o de Agent Bot nem lista conversa (licao
# de 12/06). Best-effort e defensivo: a API varia por versao do Chatwoot,
# entao logamos o corpo cru no erro pra depurar rapido.

def _whatsapp_inbox_id():
    return (current_app.config.get('CHATWOOT_WHATSAPP_INBOX_ID') or '').strip()


def whatsapp_disponivel():
    """True se da pra iniciar conversa no WhatsApp: token de usuario + inbox
    + template configurados."""
    cfg = current_app.config
    return bool(disponivel()
                and _whatsapp_inbox_id()
                and (cfg.get('CHATWOOT_WHATSAPP_TEMPLATE') or '').strip())


def _e164(telefone):
    """Telefone armazenado -> E.164 pro WhatsApp (+55DDDNUMERO). Retorna None
    se nao der pra montar um numero confiavel (sem DDD)."""
    from app.utils import normalizar_telefone
    d = normalizar_telefone(telefone)
    if not d:
        return None
    if not d.startswith('55'):
        d = '55' + d
    # 55 + DDD(2) + numero(8 ou 9) = 12 ou 13 digitos. Menos que isso = sem DDD.
    if len(d) < 12:
        return None
    return '+' + d


def listar_inboxes():
    """Diagnostico: [{id, nome, canal}] das inboxes do Chatwoot — pra achar o
    id da inbox do WhatsApp (CHATWOOT_WHATSAPP_INBOX_ID). Lista vazia em erro."""
    if not disponivel():
        return []
    try:
        r = requests.get(f'{_base()}/inboxes', headers=_headers(), timeout=15)
        if r.status_code not in (200, 201):
            logger.warning('chatwoot listar_inboxes %s: %s',
                           r.status_code, (r.text or '')[:200])
            return []
        data = r.json() if r.text else {}
    except Exception:  # noqa: BLE001
        logger.exception('chatwoot listar_inboxes falhou')
        return []
    payload = data.get('payload') if isinstance(data, dict) else None
    if not isinstance(payload, list):
        payload = data if isinstance(data, list) else []
    return [{'id': i.get('id'), 'nome': i.get('name'),
             'canal': i.get('channel_type') or i.get('channel') or ''}
            for i in payload if isinstance(i, dict)]


def _source_id_para_inbox(contato, inbox_id):
    """Extrai o source_id do contact_inbox da inbox alvo (chave que a criacao
    de conversa exige). None se o contato ainda nao tem vinculo com a inbox."""
    for ci in (contato.get('contact_inboxes') or []):
        inbox = ci.get('inbox') or {}
        if str(inbox.get('id') or ci.get('inbox_id') or '') == str(inbox_id):
            return ci.get('source_id')
    return None


def _buscar_contato(telefone_e164):
    """Acha um contato pelo telefone (search). Retorna o dict cru do contato
    (com contact_inboxes) ou None."""
    try:
        r = requests.get(f'{_base()}/contacts/search',
                         headers=_headers(), params={'q': telefone_e164},
                         timeout=15)
        if r.status_code not in (200, 201):
            return None
        data = r.json() if r.text else {}
    except Exception:  # noqa: BLE001
        logger.exception('chatwoot _buscar_contato falhou')
        return None
    payload = (data.get('payload') if isinstance(data, dict) else None) or []
    from app.utils import telefone_chave
    alvo = telefone_chave(telefone_e164)
    for c in payload:
        if isinstance(c, dict) and telefone_chave(c.get('phone_number')) == alvo:
            return c
    return payload[0] if payload and isinstance(payload[0], dict) else None


def _criar_contato(telefone_e164, nome, inbox_id):
    """Cria contato ja vinculado a inbox do WhatsApp. Retorna o dict do
    contato (com contact_inboxes/source_id) ou None."""
    body = {'inbox_id': int(inbox_id), 'name': (nome or 'Cliente').strip(),
            'phone_number': telefone_e164}
    try:
        r = requests.post(f'{_base()}/contacts', json=body,
                          headers=_headers(), timeout=15)
        if r.status_code not in (200, 201):
            logger.warning('chatwoot _criar_contato %s: %s',
                           r.status_code, (r.text or '')[:300])
            return None
        data = r.json() if r.text else {}
    except Exception:  # noqa: BLE001
        logger.exception('chatwoot _criar_contato falhou')
        return None
    # Resposta varia: {payload: {contact: {...}}} ou {payload: {...}}.
    payload = data.get('payload') if isinstance(data, dict) else None
    if isinstance(payload, dict) and isinstance(payload.get('contact'), dict):
        return payload['contact']
    return payload if isinstance(payload, dict) else None


def _conversa_aberta_do_contato(contact_id, inbox_id):
    """conversation_id de uma conversa NAO resolvida do contato na inbox do
    WhatsApp (reusa em vez de duplicar). None se nao houver."""
    try:
        r = requests.get(f'{_base()}/contacts/{contact_id}/conversations',
                         headers=_headers(), timeout=15)
        if r.status_code not in (200, 201):
            return None
        data = r.json() if r.text else {}
    except Exception:  # noqa: BLE001
        logger.exception('chatwoot _conversa_aberta_do_contato falhou')
        return None
    payload = (data.get('payload') if isinstance(data, dict) else None) or []
    abertas = [c for c in payload if isinstance(c, dict)
               and str(c.get('inbox_id') or '') == str(inbox_id)
               and c.get('status') in ('open', 'pending')]
    abertas.sort(key=lambda c: c.get('last_activity_at') or 0, reverse=True)
    return abertas[0].get('id') if abertas else None


def _criar_conversa(source_id, inbox_id, contact_id):
    """Cria conversa na inbox do WhatsApp. Retorna conversation_id ou None."""
    body = {'source_id': source_id, 'inbox_id': int(inbox_id),
            'contact_id': int(contact_id)}
    try:
        r = requests.post(f'{_base()}/conversations', json=body,
                          headers=_headers(), timeout=15)
        if r.status_code not in (200, 201):
            logger.warning('chatwoot _criar_conversa %s: %s',
                           r.status_code, (r.text or '')[:300])
            return None
        data = r.json() if r.text else {}
    except Exception:  # noqa: BLE001
        logger.exception('chatwoot _criar_conversa falhou')
        return None
    return data.get('id') if isinstance(data, dict) else None


def _render_corpo_template(params, corpo=None):
    """Texto do template com os {{N}} substituidos — vira o `content` da
    mensagem (o que aparece na THREAD; a Meta recebe o template aprovado).
    O proprio picker de template do Chatwoot manda content renderizado +
    template_params; sem content, versoes do Chatwoot mostram balao vazio.
    Corpo configuravel (CHATWOOT_WHATSAPP_TEMPLATE_CORPO) pra bater com o
    texto aprovado na Meta se o dono mudar o modelo; `corpo` explicito
    sobrepoe (ex.: template do motoboy)."""
    if corpo is None:
        corpo = (current_app.config.get('CHATWOOT_WHATSAPP_TEMPLATE_CORPO')
                 or '')
    corpo = corpo.strip()
    for i, v in enumerate(params or [], start=1):
        corpo = corpo.replace('{{%d}}' % i, str(v))
    return corpo


def enviar_template(conversation_id, nome_template, params, language,
                    corpo_template=None):
    """Manda uma mensagem de TEMPLATE aprovado (unico jeito de iniciar fora da
    janela de 24h). `params` = lista posicional (vira {{1}},{{2}}...). Retorna
    {'ok': bool, 'erro': str|None}."""
    from app.services import instancia as _inst
    if not _inst.pode_falar_com_o_mundo('chatwoot'):
        return {'ok': False, 'suprimido_instancia': True, 'erro': 'instancia nao canonica'}
    params = [str(p) for p in (params or [])]
    processed = {str(i + 1): v for i, v in enumerate(params)}
    corpo = {
        'message_type': 'outgoing',
        'content': _render_corpo_template(params, corpo=corpo_template),
        'template_params': {
            'name': nome_template,
            'category': 'utility',
            'language': language or 'pt_BR',
            'processed_params': processed,
        },
    }
    try:
        r = requests.post(f'{_base()}/conversations/{conversation_id}/messages',
                          json=corpo, headers=_headers(), timeout=15)
        if r.status_code not in (200, 201):
            erro = (r.text or '')[:300]
            logger.warning('chatwoot enviar_template %s: %s',
                           r.status_code, erro)
            return {'ok': False, 'erro': f'HTTP {r.status_code}: {erro}'}
        return {'ok': True, 'erro': None}
    except Exception as exc:  # noqa: BLE001
        logger.exception('chatwoot enviar_template falhou')
        return {'ok': False, 'erro': str(exc)}


def debug_envio_whatsapp(telefone):
    """Diagnóstico (owner): conversas de WhatsApp do cliente + erros de envio
    que a Meta gravou em cada uma (content_attributes.external_error).
    Responde "por que o template não chegou?" sem caçar conversation_id no
    Chatwoot. Caso clássico: template criado/editado HOJE ainda fora da
    lista sincronizada do Chatwoot (sync a cada ~3h) → 'Template not found'
    ou envio sem parâmetros recusado pela Meta."""
    if not disponivel():
        return {'ok': False, 'erro': 'Chatwoot nao configurado'}
    fone = _e164(telefone)
    if not fone:
        return {'ok': False, 'erro': f'Telefone invalido/sem DDD: {telefone!r}'}
    contato = _buscar_contato(fone)
    if not contato or not contato.get('id'):
        return {'ok': False, 'erro': 'Contato nao encontrado no Chatwoot'}
    inbox_id = _whatsapp_inbox_id()
    try:
        r = requests.get(f'{_base()}/contacts/{contato["id"]}/conversations',
                         headers=_headers(), timeout=15)
        data = r.json() if r.status_code in (200, 201) and r.text else {}
    except Exception:  # noqa: BLE001
        logger.exception('chatwoot debug_envio_whatsapp falhou')
        data = {}
    payload = (data.get('payload') if isinstance(data, dict) else None) or []
    convs = [c for c in payload if isinstance(c, dict)
             and (not inbox_id or str(c.get('inbox_id') or '') == str(inbox_id))]
    convs.sort(key=lambda c: c.get('last_activity_at') or 0, reverse=True)
    return {'ok': True, 'contact_id': contato.get('id'),
            'conversas': [{'conversation_id': c.get('id'),
                           'status': c.get('status'),
                           'erros': erros_de_envio(c.get('id'))}
                          for c in convs[:3]]}


def iniciar_conversa_whatsapp(telefone, nome, params,
                              template_nome=None, template_corpo=None):
    """Abre (ou reusa) uma conversa de WhatsApp com o cliente e SEMPRE manda
    o template aprovado. `params` = valores posicionais do template (ex:
    [nome, codigo_pedido]). `template_nome`/`template_corpo` sobrepõem o
    template padrão (ex.: template do motoboy Lalamove, 14/07/2026) —
    None = CHATWOOT_WHATSAPP_TEMPLATE/_CORPO de sempre.

    SEMPRE template, mesmo em conversa reusada (fix 11/07/2026): conversa
    "aberta" no Chatwoot NAO significa janela de 24h aberta na Meta — a
    conversa fica open/pending por dias, e um texto livre fora da janela
    morre em silencio. O template garante a entrega em qualquer caso (dentro
    da janela, utilidade nao custa nada; fora, custa centavos). Era o
    combinado do dono: "o botao JA chama com a mensagem de template".

    Retorna {'ok': bool, 'conversation_id': int|None, 'nova': bool,
             'erro': str|None}. Nunca levanta — o painel trata o erro."""
    # Gate fino (nao o whatsapp_disponivel() inteiro): template PADRAO so e
    # exigido quando nao veio override — senao "so template do motoboy
    # configurado" falharia com mensagem enganosa (achado do revisor).
    if not (disponivel() and _whatsapp_inbox_id()):
        return {'ok': False, 'conversation_id': None, 'nova': False,
                'erro': ('WhatsApp nao configurado (inbox). '
                         'Defina CHATWOOT_WHATSAPP_INBOX_ID.')}
    if not (template_nome
            or (current_app.config.get('CHATWOOT_WHATSAPP_TEMPLATE')
                or '').strip()):
        return {'ok': False, 'conversation_id': None, 'nova': False,
                'erro': ('Template do WhatsApp nao configurado. '
                         'Defina CHATWOOT_WHATSAPP_TEMPLATE.')}
    inbox_id = _whatsapp_inbox_id()
    fone = _e164(telefone)
    if not fone:
        return {'ok': False, 'conversation_id': None, 'nova': False,
                'erro': f'Telefone invalido/sem DDD: {telefone!r}'}

    contato = _buscar_contato(fone) or _criar_contato(fone, nome, inbox_id)
    if not contato or not contato.get('id'):
        return {'ok': False, 'conversation_id': None, 'nova': False,
                'erro': 'Nao consegui achar/criar o contato no Chatwoot.'}
    contact_id = contato['id']

    # Reusa conversa aberta (nao duplica thread); sem nenhuma, cria uma.
    conv_id = _conversa_aberta_do_contato(contact_id, inbox_id)
    nova = conv_id is None
    if nova:
        source_id = _source_id_para_inbox(contato, inbox_id)
        if not source_id:
            # Contato existia mas sem vinculo com a inbox do WhatsApp — cria
            # o vinculo recriando o contato na inbox (idempotente).
            recriado = _criar_contato(fone, nome, inbox_id)
            source_id = _source_id_para_inbox(recriado or {}, inbox_id)
        if not source_id:
            return {'ok': False, 'conversation_id': None, 'nova': False,
                    'erro': 'Contato sem vinculo com a inbox do WhatsApp '
                            '(source_id ausente).'}
        conv_id = _criar_conversa(source_id, inbox_id, contact_id)
        if not conv_id:
            return {'ok': False, 'conversation_id': None, 'nova': False,
                    'erro': 'Nao consegui criar a conversa no Chatwoot.'}

    cfg = current_app.config
    res = enviar_template(
        conv_id,
        (template_nome or cfg.get('CHATWOOT_WHATSAPP_TEMPLATE') or '').strip(),
        params, (cfg.get('CHATWOOT_WHATSAPP_TEMPLATE_LANG') or 'pt_BR').strip(),
        corpo_template=template_corpo)
    if not res['ok']:
        # A conversa existe, mas o template falhou. Devolve o conv_id (o
        # atendente ve a conversa) + o erro cru pra corrigir o template.
        return {'ok': False, 'conversation_id': conv_id, 'nova': nova,
                'erro': res['erro']}
    return {'ok': True, 'conversation_id': conv_id, 'nova': nova, 'erro': None}
