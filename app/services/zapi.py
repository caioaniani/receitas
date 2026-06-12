"""Cliente Z-API (WhatsApp) — envia mensagens de texto.

Cadastro/setup: https://z-api.io/
- Cria instancia, conecta WhatsApp via QR code, copia INSTANCE_ID + TOKEN
- Opcional: ativa Token de Seguranca (Client-Token) em Account Settings

Env vars: ZAPI_INSTANCE_ID, ZAPI_TOKEN, ZAPI_CLIENT_TOKEN (opcional).
"""
import logging

import requests
from flask import current_app

logger = logging.getLogger(__name__)

BASE = 'https://api.z-api.io'


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
                  'ZAPI_BOT_DONO_NUMERO'):
        v = (cfg.get(chave) or '').strip()
        if _e_grupo(v):
            permitidos.add(_normalizar_grupo(v))
    return {g for g in permitidos if g}


def enviar_texto(numero, mensagem):
    """POST /send-text com texto simples. Retorna {'ok': bool, ...}.

    Aceita numero de telefone OU ID de grupo ('1203...-group'). Pra
    mandar alertas num grupo: criar o grupo no WhatsApp, adicionar o
    numero do bot, pegar o ID em /admin/zapi/grupos e configurar o
    destino (ex: CHATBOT_VIGIA_NUMERO=120363...-group).

    SEGURANCA: rejeita envio pra destino fora do whitelist
    (`ZAPI_NUMEROS_PERMITIDOS` pra fones, `ZAPI_GRUPOS_PERMITIDOS` +
    destinos configurados pra grupos). Fail-closed.
    """
    cfg = current_app.config
    instance_id = (cfg.get('ZAPI_INSTANCE_ID') or '').strip()
    token = (cfg.get('ZAPI_TOKEN') or '').strip()
    client_token = (cfg.get('ZAPI_CLIENT_TOKEN') or '').strip()

    if not instance_id or not token:
        return {'ok': False, 'erro': 'Z-API nao configurado (ZAPI_INSTANCE_ID/ZAPI_TOKEN)'}

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
