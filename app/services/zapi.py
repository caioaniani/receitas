"""Cliente Z-API (WhatsApp) — envia mensagens de texto.

Cadastro/setup: https://z-api.io/
- Cria instancia, conecta WhatsApp via QR code, copia INSTANCE_ID + TOKEN
- Opcional: ativa Token de Seguranca (Client-Token) em Account Settings

Env vars: ZAPI_INSTANCE_ID, ZAPI_TOKEN, ZAPI_CLIENT_TOKEN (opcional).
"""
import logging
import threading
import time

import requests
from flask import current_app

logger = logging.getLogger(__name__)

BASE = 'https://api.z-api.io'

# ── Teto GLOBAL de envio/hora (anti-spam do WhatsApp/Meta) ──────────────────
# Ponto ÚNICO por onde TODO envio passa (alertas, vigias, digest, magic-link de
# motorista). O WhatsApp restringe número que dispara automático em volume; um
# vigia em loop ou bug podia inundar a linha e derrubá-la. Aqui um teto por hora
# barra o flood. In-memory POR WORKER (cap efetivo = teto × nº de workers gunicorn
# — aceitável, mesmo padrão dos tetos por-feature). Kill-switch: ZAPI_THROTTLE=0.
#
# GARANTIAS: (1) mensagem CRÍTICA (critico=True: Lalamove/pedido pago) NUNCA é
# segurada; (2) o que passar do teto NÃO some em silêncio — vira UM digest ao dono
# no próximo envio liberado (regra do dono: nada de alerta perdido calado).
_JANELA_SEG = 3600.0
_throttle_lock = threading.Lock()
_env_ts = []            # monotonic() de cada envio real na janela
_seg_previews = []      # amostra (bounded) das mensagens seguradas
_seg_n = 0              # contagem REAL de seguradas desde o último digest


def _teto_hora():
    try:
        return max(1, int(current_app.config.get('ZAPI_MAX_HORA', 30)))
    except (TypeError, ValueError):
        return 30


def _throttle_ativo():
    """Teto ligado? Default ON em prod, OFF sob TESTING (sem override explícito)
    — o estado é global de módulo e vazaria entre os ~2500 testes que compartilham
    o app. Os testes do teto setam ZAPI_THROTTLE='1' de propósito."""
    cfg = current_app.config
    v = cfg.get('ZAPI_THROTTLE')
    if v is None:
        return not cfg.get('TESTING')
    return str(v).strip().lower() not in ('0', 'false', 'no', '')


def _prune_locked(agora):
    limite = agora - _JANELA_SEG
    while _env_ts and _env_ts[0] < limite:
        _env_ts.pop(0)


def _montar_digest_locked():
    """Resumo do que foi segurado + zera o buffer. Chamar sob _throttle_lock."""
    global _seg_n
    n, amostra = _seg_n, list(_seg_previews[-5:])
    _seg_n = 0
    _seg_previews.clear()
    linhas = [f'⚠ {n} alerta(s) automático(s) foram SEGURADOS na última hora '
              'pra proteger o número contra o anti-spam do WhatsApp.']
    if amostra:
        linhas.append('Amostra do que ficou retido:')
        linhas += [f'• {p}' for p in amostra]
    linhas.append('Se isso repetir, algum vigia pode estar em loop — confira os '
                  'vigias e /admin/frete-sensores.')
    return '\n'.join(linhas)


def _throttle_decidir(mensagem, critico):
    """Sob o lock: decide se pode enviar agora e se há digest de seguradas a
    liberar antes. Retorna (pode_enviar, digest_ou_None). NÃO faz I/O."""
    global _seg_n
    agora = time.monotonic()
    with _throttle_lock:
        _prune_locked(agora)
        if critico or len(_env_ts) < _teto_hora():
            _env_ts.append(agora)
            return True, (_montar_digest_locked() if _seg_n else None)
        # Passou do teto e não é crítico: segura (vira digest depois).
        _seg_n += 1
        _seg_previews.append(' '.join(str(mensagem or '').split())[:60])
        del _seg_previews[:-8]
        return False, None


def disponivel():
    cfg = current_app.config
    return bool((cfg.get('ZAPI_INSTANCE_ID') or '').strip()
                and (cfg.get('ZAPI_TOKEN') or '').strip())


def status_instancia():
    """Consulta o status da instancia no Z-API (conectada ao WhatsApp?).
    Retorna {'ok': bool, 'conectado': bool, 'detalhe': str}."""
    cfg = current_app.config
    instance_id = (cfg.get('ZAPI_INSTANCE_ID') or '').strip()
    token = (cfg.get('ZAPI_TOKEN') or '').strip()
    client_token = (cfg.get('ZAPI_CLIENT_TOKEN') or '').strip()
    if not instance_id or not token:
        return {'ok': False, 'conectado': False, 'detalhe': 'Z-API nao configurado'}
    url = f'{BASE}/instances/{instance_id}/token/{token}/status'
    headers = {'Client-Token': client_token} if client_token else {}
    try:
        r = requests.get(url, headers=headers, timeout=10)
        if r.status_code not in (200, 201):
            return {'ok': False, 'conectado': False, 'detalhe': f'HTTP {r.status_code}'}
        data = r.json() if r.text else {}
        conectado = bool(data.get('connected'))
        detalhe = data.get('error') or ('conectado' if conectado else 'desconectado')
        return {'ok': True, 'conectado': conectado, 'detalhe': detalhe}
    except Exception as exc:  # noqa: BLE001
        logger.exception('zapi status falhou')
        return {'ok': False, 'conectado': False, 'detalhe': str(exc)}


def _normalizar_numero(numero):
    """Mantem so digitos. '+55 11 99999-9999' → '5511999999999'.

    Delega pra `app.utils.normalizar_telefone` (fonte canonica unica)."""
    from app.utils import normalizar_telefone
    return normalizar_telefone(numero)


def _whitelist_numeros():
    """Numeros permitidos (set). Vazio = recusa tudo (fail-closed).

    Inclui automaticamente telefones de motoristas ativos
    (`driver_magic.telefones_drivers_ativos()`) — admin nao precisa
    manter lista manual em paralelo com `/entregas/drivers`."""
    raw = (current_app.config.get('ZAPI_NUMEROS_PERMITIDOS') or '').strip()
    permitidos = {_normalizar_numero(n) for n in raw.split(',') if n.strip()}
    # Inclui o destino padrao do digest tambem (atalho)
    destino = _normalizar_numero(current_app.config.get('ZAPI_NUMERO_DESTINO') or '')
    if destino:
        permitidos.add(destino)
    # Numero do dono pro bot privado (so escreve pra ele mesmo).
    dono = _normalizar_numero(current_app.config.get('ZAPI_BOT_DONO_NUMERO') or '')
    if dono:
        permitidos.add(dono)
    # Inclui motoristas ativos (pra magic link diario funcionar sem
    # admin precisar adicionar manualmente cada telefone).
    try:
        from app.services.driver_magic import telefones_drivers_ativos
        permitidos |= telefones_drivers_ativos()
    except Exception:  # noqa: BLE001
        # Servico podia nao estar disponivel em alguns contextos
        # (ex: scripts standalone). Falha silenciosa nao trava o envio
        # pra numeros ja na whitelist manual.
        pass
    return {n for n in permitidos if n}


def _e_grupo(destino):
    """True se o destino e um ID de GRUPO do Z-API (sufixo '-group').

    Grupos NAO passam pela normalizacao de telefone (que removeria o
    sufixo e quebraria o ID) nem pela whitelist de numeros — tem
    whitelist propria (ZAPI_GRUPOS_PERMITIDOS + destinos configurados)."""
    return str(destino or '').strip().lower().endswith('-group')


def _normalizar_grupo(destino):
    """Mantem digitos + sufixo '-group'. '1203 6302-group' → '12036302-group'."""
    s = str(destino or '').strip()
    base = s[:-len('-group')] if s.lower().endswith('-group') else s
    digitos = ''.join(ch for ch in base if ch.isdigit())
    return f'{digitos}-group' if digitos else ''


def _whitelist_grupos():
    """IDs de grupo permitidos. Inclui automaticamente os destinos de
    alerta configurados (mesmo atalho da whitelist de numeros)."""
    cfg = current_app.config
    raw = (cfg.get('ZAPI_GRUPOS_PERMITIDOS') or '').strip()
    permitidos = {_normalizar_grupo(g) for g in raw.split(',') if g.strip()}
    # Destinos de alerta que podem ser grupo entram sozinhos
    for chave in ('ZAPI_NUMERO_DESTINO', 'CHATBOT_VIGIA_NUMERO',
                  'ZAPI_BOT_DONO_NUMERO', 'CHATWOOT_VIGIA_INFRA_NUMERO'):
        v = (cfg.get(chave) or '').strip()
        if _e_grupo(v):
            permitidos.add(_normalizar_grupo(v))
    return {g for g in permitidos if g}


def enviar_texto(numero, mensagem, *, critico=False, _interno=False):
    """POST /send-text com texto simples. Retorna {'ok': bool, ...}.

    Aceita numero de telefone OU ID de grupo ('1203...-group'). Pra
    mandar alertas num grupo: criar o grupo no WhatsApp, adicionar o
    numero do bot, pegar o ID em /admin/zapi/grupos e configurar o
    destino (ex: CHATBOT_VIGIA_NUMERO=120363...-group).

    SEGURANCA: rejeita envio pra destino fora do whitelist
    (`ZAPI_NUMEROS_PERMITIDOS` pra fones, `ZAPI_GRUPOS_PERMITIDOS` +
    destinos configurados pra grupos). Fail-closed.

    `critico=True`: isenta do teto/hora global (Lalamove/pedido pago — nunca
    segura). `_interno`: uso interno (flush do digest de seguradas) — não
    reentra no teto. Msg segurada pelo teto retorna {'ok': False,
    'segurado': True}.
    """
    cfg = current_app.config
    instance_id = (cfg.get('ZAPI_INSTANCE_ID') or '').strip()
    token = (cfg.get('ZAPI_TOKEN') or '').strip()
    client_token = (cfg.get('ZAPI_CLIENT_TOKEN') or '').strip()

    if not instance_id or not token:
        return {'ok': False, 'erro': 'Z-API nao configurado (ZAPI_INSTANCE_ID/ZAPI_TOKEN)'}

    # INSTANCIA CANONICA (20/08/2026): copia de homologacao com as MESMAS
    # envs nao pode mandar WhatsApp — os crons de horario de parede fazem as
    # duas instancias dispararem no mesmo minuto e o dono recebe tudo em
    # dobro (ver app/services/instancia.py). Fail-open fora do Railway;
    # `critico` nunca e bloqueado.
    from app.services import instancia as _inst
    if not _inst.pode_falar_com_o_mundo('zapi', critico=critico):
        return {'ok': False, 'suprimido_instancia': True,
                'erro': 'instancia nao canonica — envio suprimido'}

    if _e_grupo(numero):
        destino = _normalizar_grupo(numero)
        if not destino:
            return {'ok': False, 'erro': 'id de grupo invalido'}
        grupos_ok = _whitelist_grupos()
        if destino not in grupos_ok:
            logger.error('zapi: grupo %s NAO esta no whitelist (%s '
                          'permitidos). RECUSADO.', destino, len(grupos_ok))
            return {'ok': False, 'erro': f'grupo {destino} fora do '
                                          'whitelist — recusado por seguranca'}
    else:
        destino = _normalizar_numero(numero)
        if not destino:
            return {'ok': False, 'erro': 'numero invalido'}
        permitidos = _whitelist_numeros()
        if not permitidos:
            logger.error('zapi: ZAPI_NUMEROS_PERMITIDOS vazio — recusa total. Numero pedido: %s', destino)
            return {'ok': False, 'erro': 'whitelist vazio — configure ZAPI_NUMEROS_PERMITIDOS'}
        if destino not in permitidos:
            logger.error('zapi: numero %s NAO esta no whitelist (%s permitidos). RECUSADO.',
                          destino, len(permitidos))
            return {'ok': False, 'erro': f'numero {destino} fora do whitelist — recusado por seguranca'}

    # Teto global anti-spam (depois do whitelist: destino recusado nao conta nem
    # segura). `_interno` = flush do digest, sempre passa e conta na janela.
    if _throttle_ativo():
        if _interno:
            with _throttle_lock:
                agora = time.monotonic()
                _prune_locked(agora)
                _env_ts.append(agora)
        else:
            permitido, digest = _throttle_decidir(mensagem, critico)
            if digest:
                dono = (cfg.get('ZAPI_NUMERO_DESTINO')
                        or cfg.get('ZAPI_BOT_DONO_NUMERO') or '').strip()
                if dono:
                    try:
                        enviar_texto(dono, digest, critico=True, _interno=True)
                    except Exception:  # noqa: BLE001
                        logger.exception('zapi: falha ao enviar digest de seguradas')
            if not permitido:
                logger.warning('zapi: teto/hora (%s) atingido — mensagem segurada '
                               '(vira digest ao dono)', _teto_hora())
                return {'ok': False, 'segurado': True,
                        'erro': 'teto/hora de envio atingido — mensagem segurada'}

    url = f'{BASE}/instances/{instance_id}/token/{token}/send-text'
    headers = {'Content-Type': 'application/json'}
    if client_token:
        headers['Client-Token'] = client_token

    try:
        r = requests.post(url, json={'phone': destino, 'message': mensagem or '',
                                      'linkPreview': True},
                          headers=headers, timeout=15)
        if r.status_code not in (200, 201):
            logger.warning('zapi send-text %s: %s', r.status_code, r.text[:200])
            return {'ok': False, 'erro': f'HTTP {r.status_code}: {r.text[:200]}'}
        return {'ok': True, 'response': r.json() if r.text else {}}
    except Exception as exc:  # noqa: BLE001
        logger.exception('zapi enviar_texto falhou')
        return {'ok': False, 'erro': str(exc)}


def listar_grupos():
    """Lista os GRUPOS que o numero do bot participa, com id pronto pra
    colar no destino de alertas. Usa GET /chats do Z-API e filtra
    isGroup. Retorna {'ok': bool, 'grupos': [{'id', 'nome'}], ...}."""
    cfg = current_app.config
    instance_id = (cfg.get('ZAPI_INSTANCE_ID') or '').strip()
    token = (cfg.get('ZAPI_TOKEN') or '').strip()
    client_token = (cfg.get('ZAPI_CLIENT_TOKEN') or '').strip()
    if not instance_id or not token:
        return {'ok': False, 'erro': 'Z-API nao configurado'}
    url = f'{BASE}/instances/{instance_id}/token/{token}/chats'
    headers = {}
    if client_token:
        headers['Client-Token'] = client_token
    try:
        r = requests.get(url, params={'page': 1, 'pageSize': 100},
                         headers=headers, timeout=15)
        if r.status_code not in (200, 201):
            return {'ok': False, 'erro': f'HTTP {r.status_code}: {r.text[:200]}'}
        data = r.json() if r.text else []
    except Exception as exc:  # noqa: BLE001
        logger.exception('zapi listar_grupos falhou')
        return {'ok': False, 'erro': str(exc)}
    chats = data if isinstance(data, list) else (data.get('chats') or [])
    grupos = []
    for c in chats:
        if not isinstance(c, dict):
            continue
        cid = str(c.get('phone') or c.get('id') or '')
        if c.get('isGroup') or cid.lower().endswith('-group'):
            grupos.append({'id': cid,
                           'nome': c.get('name') or c.get('chatName') or ''})
    return {'ok': True, 'grupos': grupos, 'total': len(grupos)}
