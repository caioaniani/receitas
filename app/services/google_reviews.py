"""Avaliacoes do Google (Business Profile) no gestao — 12/07/2026.

Pedido do dono: "conectar os comentarios do Google no gestao.opao" (ver +
responder + alerta de review nova). 3 locations: Ribeiro do Vale (Brooklin),
Anesio Pinto Rosa (Itaim), Nebraska (1851 Coffee).

DEPENDENCIAS EXTERNAS (a integracao fica DORMENTE ate elas existirem, mesmo
padrao do Seru/Chatwoot/Sicredi):
- Acesso APROVADO a Business Profile API no Google Cloud (formulario; sem isso
  a API responde 403). https://developers.google.com/my-business/content/prereqs
- OAuth do dono uma vez (rota /admin/avaliacoes-google/conectar) — gera o
  refresh_token que guardamos em AppConfig.

Garantias:
- Kill-switch `GOOGLE_REVIEWS=0`.
- GRACEFUL: sem credencial/token, toda funcao retorna vazio/no-op (nunca
  levanta) — a tela funciona e so mostra "nao conectado".
- Chamadas HTTP com timeout + try/except (padrao google_maps.py): erro vira
  log + retorno vazio, nunca quebra a tela nem o cron.

Fontes (endpoints Google):
- OAuth:      https://accounts.google.com/o/oauth2/v2/auth  +  oauth2.googleapis.com/token
- Accounts:   mybusinessaccountmanagement.googleapis.com/v1/accounts
- Locations:  mybusinessbusinessinformation.googleapis.com/v1/{account}/locations
- Reviews/reply (v4): mybusiness.googleapis.com/v4/{account}/{location}/reviews[/{id}/reply]
"""
import logging
from datetime import datetime, timedelta
from urllib.parse import urlencode

import requests

from app.extensions import db
from app.utils import agora

logger = logging.getLogger(__name__)

# Enum de estrelas do Google -> inteiro 1..5.
_ESTRELAS = {'ONE': 1, 'TWO': 2, 'THREE': 3, 'FOUR': 4, 'FIVE': 5}

_SCOPE = 'https://www.googleapis.com/auth/business.manage'
_AUTH_URL = 'https://accounts.google.com/o/oauth2/v2/auth'
_TOKEN_URL = 'https://oauth2.googleapis.com/token'
_ACCOUNTS_URL = 'https://mybusinessaccountmanagement.googleapis.com/v1/accounts'
_INFO_BASE = 'https://mybusinessbusinessinformation.googleapis.com/v1'
_V4_BASE = 'https://mybusiness.googleapis.com/v4'

# Chaves de estado em AppConfig (sobrevivem a deploy/multi-worker).
_KEY_TOKEN = 'google_oauth_token'        # JSON {access_token, refresh_token, expiry, scope}
_KEY_PRIMED = 'google_reviews_primed'    # '1' apos o 1o sync (evita alertar historico)

_TIMEOUT = 15


# ── Config / kill-switch ─────────────────────────────────────────────

def _cfg(chave, default=''):
    from flask import current_app
    return (current_app.config.get(chave) or default)


def _ativo():
    from flask import current_app
    return str(current_app.config.get('GOOGLE_REVIEWS', '1')).strip().lower() \
        not in ('0', 'false', 'no', '')


def _client_creds():
    return (_cfg('GOOGLE_OAUTH_CLIENT_ID').strip(),
            _cfg('GOOGLE_OAUTH_CLIENT_SECRET').strip())


def _token_state():
    """Le o dict de tokens do AppConfig (ou {} se nao conectado)."""
    import json

    from app.models import AppConfig
    raw = AppConfig.get(_KEY_TOKEN)
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return {}


def _salvar_token_state(estado):
    import json

    from app.models import AppConfig
    AppConfig.set(_KEY_TOKEN, json.dumps(estado))
    db.session.commit()


def conectado():
    """True se ja fizemos o OAuth (temos refresh_token guardado)."""
    return bool(_token_state().get('refresh_token'))


def disponivel():
    """True se da pra operar: kill-switch on + client creds + OAuth feito."""
    if not _ativo():
        return False
    cid, secret = _client_creds()
    return bool(cid and secret and conectado())


# ── OAuth ────────────────────────────────────────────────────────────

def url_autorizacao(redirect_uri, state):
    """URL de consentimento do Google (o dono clica e autoriza). `access_type=
    offline` + `prompt=consent` garante o refresh_token na primeira vez."""
    cid, _ = _client_creds()
    if not cid:
        return None
    params = {
        'client_id': cid,
        'redirect_uri': redirect_uri,
        'response_type': 'code',
        'scope': _SCOPE,
        'access_type': 'offline',
        'prompt': 'consent',
        'state': state,
    }
    return f'{_AUTH_URL}?{urlencode(params)}'


def trocar_codigo(code, redirect_uri):
    """Troca o `code` do callback por tokens e guarda em AppConfig.
    Retorna (ok, msg)."""
    cid, secret = _client_creds()
    if not (cid and secret):
        return False, 'GOOGLE_OAUTH_CLIENT_ID/SECRET nao configurados.'
    try:
        r = requests.post(_TOKEN_URL, data={
            'code': code, 'client_id': cid, 'client_secret': secret,
            'redirect_uri': redirect_uri, 'grant_type': 'authorization_code',
        }, timeout=_TIMEOUT)
        if r.status_code != 200:
            logger.warning('google_reviews: troca de codigo http %s: %s',
                           r.status_code, r.text[:300])
            return False, f'Google recusou a autorizacao (HTTP {r.status_code}).'
        d = r.json()
        refresh = d.get('refresh_token')
        if not refresh:
            # Sem refresh_token o acesso morre em 1h — forcamos consent acima,
            # entao isso so acontece se o dono ja tinha autorizado antes.
            return False, ('Google nao devolveu refresh_token. Remova o acesso '
                           'do app na conta Google e conecte de novo.')
        estado = {
            'access_token': d.get('access_token'),
            'refresh_token': refresh,
            'scope': d.get('scope'),
            'expiry': (agora() + timedelta(
                seconds=int(d.get('expires_in') or 3600))).isoformat(),
        }
        _salvar_token_state(estado)
        return True, 'Conta Google conectada com sucesso.'
    except (requests.RequestException, ValueError) as e:
        logger.warning('google_reviews: erro na troca de codigo: %s', e)
        return False, 'Falha de rede ao falar com o Google.'


def desconectar():
    """Apaga o token guardado (o dono pode reconectar depois)."""
    _salvar_token_state({})


def _token_acesso():
    """Access token valido, renovando via refresh_token se expirou. None se
    nao conectado ou o refresh falhou."""
    estado = _token_state()
    refresh = estado.get('refresh_token')
    if not refresh:
        return None
    # Ainda valido (com folga de 60s)?
    exp = estado.get('expiry')
    if estado.get('access_token') and exp:
        try:
            if datetime.fromisoformat(exp) - agora() > timedelta(seconds=60):
                return estado['access_token']
        except (ValueError, TypeError):
            pass
    cid, secret = _client_creds()
    if not (cid and secret):
        return None
    try:
        r = requests.post(_TOKEN_URL, data={
            'refresh_token': refresh, 'client_id': cid,
            'client_secret': secret, 'grant_type': 'refresh_token',
        }, timeout=_TIMEOUT)
        if r.status_code != 200:
            logger.warning('google_reviews: refresh http %s: %s',
                           r.status_code, r.text[:200])
            return None
        d = r.json()
        estado['access_token'] = d.get('access_token')
        estado['expiry'] = (agora() + timedelta(
            seconds=int(d.get('expires_in') or 3600))).isoformat()
        _salvar_token_state(estado)
        return estado['access_token']
    except (requests.RequestException, ValueError) as e:
        logger.warning('google_reviews: erro no refresh: %s', e)
        return None


# ── Cliente HTTP autenticado ─────────────────────────────────────────

def _get(url, params=None):
    """GET autenticado. Retorna dict do JSON ou None (best-effort)."""
    token = _token_acesso()
    if not token:
        return None
    try:
        r = requests.get(url, params=params or {},
                         headers={'Authorization': f'Bearer {token}'},
                         timeout=_TIMEOUT)
        if r.status_code != 200:
            logger.warning('google_reviews GET %s http %s: %s',
                           url, r.status_code, r.text[:200])
            return None
        return r.json()
    except (requests.RequestException, ValueError) as e:
        logger.warning('google_reviews GET %s erro: %s', url, e)
        return None


# ── Descoberta de locations ──────────────────────────────────────────

def _listar_accounts():
    """resourceNames das contas ('accounts/123'). Lista."""
    contas = []
    d = _get(_ACCOUNTS_URL) or {}
    for a in d.get('accounts') or []:
        nome = a.get('name')
        if nome:
            contas.append(nome)
    return contas


def _sincronizar_locations():
    """Descobre as locations de cada conta e faz upsert em
    GoogleReviewLocation. Retorna a lista de resourceNames completos
    ('accounts/123/locations/456'). Auto-fuzzy pra Loja fica pro admin."""
    from app.models import GoogleReviewLocation
    from app.utils import resolver_loja_por_nome
    nomes_completos = []
    for conta in _listar_accounts():
        d = _get(f'{_INFO_BASE}/{conta}/locations',
                 params={'readMask': 'name,title', 'pageSize': 100}) or {}
        for loc in d.get('locations') or []:
            curto = loc.get('name')          # 'locations/456'
            if not curto:
                continue
            completo = f'{conta}/{curto}' if not curto.startswith('accounts/') \
                else curto
            titulo = loc.get('title') or ''
            nomes_completos.append(completo)
            existente = GoogleReviewLocation.query.filter_by(
                location_name=completo).first()
            if existente:
                if titulo and existente.apelido != titulo:
                    existente.apelido = titulo
                continue
            # Nova location — tenta vincular a Loja por nome (admin confirma).
            loja = resolver_loja_por_nome(titulo) if titulo else None
            db.session.add(GoogleReviewLocation(
                location_name=completo, apelido=titulo,
                loja_id=loja.id if loja else None))
    db.session.commit()
    return nomes_completos


# ── Datas ────────────────────────────────────────────────────────────

def _parse_dt(valor):
    """RFC3339 do Google (UTC, sufixo Z) -> datetime BRT naive (UTC-3, sem
    horario de verao desde 2019). None se nao parsear."""
    if not valor:
        return None
    from app.utils import para_brt
    try:
        dt = datetime.fromisoformat(valor.replace('Z', '+00:00'))
    except (ValueError, TypeError):
        return None
    # Google devolve UTC (aware); para_brt centraliza a conversao pra BRT naive.
    return para_brt(dt)


# ── Sincronizacao de reviews ─────────────────────────────────────────

def _upsert_review(loc_completo, r):
    """Insere/atualiza uma review. Retorna a instancia se foi INSERT novo
    (pra alertar), senao None."""
    from app.models import GoogleReview
    rid = r.get('reviewId') or r.get('name')
    if not rid:
        return None
    reviewer = r.get('reviewer') or {}
    reply = r.get('reviewReply') or {}
    nota = _ESTRELAS.get(r.get('starRating'))
    existente = GoogleReview.query.filter_by(review_id=rid).first()
    novo = existente is None
    rev = existente or GoogleReview(review_id=rid)
    rev.location_name = loc_completo
    rev.autor = (reviewer.get('displayName') or 'Anonimo')[:200]
    rev.autor_foto = (reviewer.get('profilePhotoUrl') or '')[:500] or None
    rev.nota = nota
    rev.comentario = r.get('comment')
    rev.criado_em_google = _parse_dt(r.get('createTime'))
    rev.atualizado_em_google = _parse_dt(r.get('updateTime'))
    # Resposta que veio do Google (pode ter sido respondida por fora do gestao).
    if reply.get('comment'):
        rev.resposta_texto = reply.get('comment')
        rev.resposta_em = _parse_dt(reply.get('updateTime')) or rev.resposta_em
    rev.sincronizado_em = agora()
    if novo:
        db.session.add(rev)
    return rev if novo else None


def sincronizar():
    """Puxa as reviews de todas as locations e faz upsert (idempotente por
    review_id). Retorna a lista de reviews NOVAS (recem-inseridas). No-op
    gracioso se nao conectado."""
    if not disponivel():
        return []
    novas = []
    for loc in _sincronizar_locations():
        pagina = None
        for _ in range(20):          # teto de paginas (anti-loop)
            params = {'pageSize': 50}
            if pagina:
                params['pageToken'] = pagina
            d = _get(f'{_V4_BASE}/{loc}/reviews', params=params)
            if d is None:
                break
            for r in d.get('reviews') or []:
                nova = _upsert_review(loc, r)
                if nova is not None:
                    novas.append(nova)
            db.session.commit()
            pagina = d.get('nextPageToken')
            if not pagina:
                break
    return novas


def sincronizar_e_alertar():
    """Chamado pelo cron. Sincroniza e, se ja passou do 1o sync (primed),
    alerta o dono no WhatsApp sobre reviews novas (prioriza nota baixa). O 1o
    sync so IMPORTA (nao inunda o WhatsApp com o historico inteiro)."""
    if not disponivel():
        return {'rodou': False, 'motivo': 'nao conectado'}
    novas = sincronizar()
    from app.models import AppConfig, GoogleReviewLocation
    # "primed" so pode ser marcado quando a API REALMENTE respondeu — sinal:
    # ao menos uma location descoberta. Sem isso, um run durante a janela de
    # 403 (OAuth feito, mas acesso a Business Profile API ainda nao aprovado)
    # marcaria primed com o banco VAZIO; ai o 1o import real, depois da
    # aprovacao, dispararia UM WhatsApp com o historico INTEIRO — justo o que o
    # primed existe pra evitar (achado da revisao 12/07/2026).
    api_respondeu = GoogleReviewLocation.query.count() > 0
    primed = AppConfig.get(_KEY_PRIMED) == '1'
    if not primed:
        if api_respondeu:
            AppConfig.set(_KEY_PRIMED, '1')
            db.session.commit()
            return {'rodou': True, 'novas': len(novas), 'alertou': False,
                    'motivo': 'primeiro sync (historico importado sem alertar)'}
        return {'rodou': True, 'novas': len(novas), 'alertou': False,
                'motivo': 'API ainda nao respondeu — nao primado (aguardando '
                          'aprovacao do Google?)'}
    enviou = _alertar_novas(novas) if novas else False
    return {'rodou': True, 'novas': len(novas), 'alertou': enviou}


# ── Alerta WhatsApp ──────────────────────────────────────────────────

def _numero_dono():
    return (_cfg('GOOGLE_REVIEWS_NUMERO').strip()
            or _cfg('ZAPI_BOT_DONO_NUMERO').strip())


def _texto_alerta(novas):
    """Mensagem do alerta (pura — testavel). Prioriza notas baixas."""
    baixas = [r for r in novas if (r.nota or 5) <= 3]
    linhas = [f'⭐ {len(novas)} avaliacao(oes) nova(s) no Google']
    destaque = sorted(novas, key=lambda r: (r.nota or 5))[:4]
    for r in destaque:
        estrelas = '★' * (r.nota or 0) + '☆' * (5 - (r.nota or 0))
        txt = (r.comentario or '').strip().replace('\n', ' ')
        if len(txt) > 120:
            txt = txt[:117] + '…'
        autor = (r.autor or 'Anonimo').strip()
        linhas.append(f'{estrelas} {autor}: {txt or "(sem texto)"}')
    if baixas:
        linhas.append(f'⚠ {len(baixas)} com nota 1-3 — responder com prioridade.')
    linhas.append('Responder em /admin/avaliacoes-google')
    return '\n'.join(linhas)


def _alertar_novas(novas):
    """Manda UM WhatsApp ao dono resumindo as reviews novas. Best-effort."""
    try:
        numero = _numero_dono()
        if not numero:
            logger.info('google_reviews: sem numero de destino, pulando alerta')
            return False
        from app.services import zapi
        zapi.enviar_texto(numero, _texto_alerta(novas))
        return True
    except Exception:  # noqa: BLE001 — alerta nunca pode quebrar o sync
        logger.exception('google_reviews: falha ao alertar reviews novas')
        return False


# ── Responder ────────────────────────────────────────────────────────

def responder(review_pk, texto, user_id=None):
    """Responde uma review via API + espelha local. Retorna (ok, msg)."""
    from app.models import GoogleReview
    texto = (texto or '').strip()
    if not texto:
        return False, 'A resposta esta vazia.'
    rev = GoogleReview.query.get(review_pk)
    if not rev:
        return False, 'Avaliacao nao encontrada.'
    if not disponivel():
        return False, 'Conta Google nao conectada.'
    token = _token_acesso()
    if not token:
        return False, 'Nao consegui autenticar no Google (reconecte a conta).'
    try:
        r = requests.put(
            f'{_V4_BASE}/{rev.location_name}/reviews/{rev.review_id}/reply',
            headers={'Authorization': f'Bearer {token}'},
            json={'comment': texto}, timeout=_TIMEOUT)
        if r.status_code not in (200, 201):
            logger.warning('google_reviews: reply http %s: %s',
                           r.status_code, r.text[:200])
            return False, f'Google recusou a resposta (HTTP {r.status_code}).'
    except (requests.RequestException, ValueError) as e:
        logger.warning('google_reviews: erro ao responder: %s', e)
        return False, 'Falha de rede ao enviar a resposta.'
    rev.resposta_texto = texto
    rev.resposta_em = agora()
    rev.respondida_por_id = user_id
    db.session.commit()
    return True, 'Resposta publicada no Google.'


# ── Rascunho de resposta com IA (Sonnet) ─────────────────────────────

def rascunho_resposta(review_pk):
    """Sugere um rascunho de resposta (nao publica). Sonnet, custo em UsoIA.
    Retorna (texto|None, msg)."""
    import os

    from app.models import GoogleReview
    rev = GoogleReview.query.get(review_pk)
    if not rev:
        return None, 'Avaliacao nao encontrada.'
    api_key = os.environ.get('ANTHROPIC_API_KEY')
    if not api_key:
        return None, 'IA indisponivel (sem ANTHROPIC_API_KEY).'
    modelo = os.environ.get('GOOGLE_REVIEWS_IA_MODELO', 'claude-sonnet-4-6')
    estrelas = rev.nota or 0
    prompt = (
        'Voce e o dono de uma padaria artesanal (Opao) respondendo a uma '
        'avaliacao no Google. Escreva UMA resposta curta (2-4 frases), calorosa '
        'e em portugues correto, na primeira pessoa da padaria. Agradeca pelo '
        'nome quando houver; se a nota for baixa, reconheca o problema com '
        'humildade e convide a pessoa a voltar/entrar em contato — sem prometer '
        'nada especifico. Nao use emojis em excesso (no maximo um).\n\n'
        f'Nota: {estrelas} de 5 estrelas\n'
        f'Autor: {rev.autor or "cliente"}\n'
        f'Comentario: {rev.comentario or "(sem texto)"}\n\n'
        'Responda apenas com o texto da resposta, sem aspas.')
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key, timeout=45, max_retries=1)
        resp = client.messages.create(
            model=modelo, max_tokens=400,
            messages=[{'role': 'user', 'content': prompt}])
        from app.services import uso_ia
        uso_ia.registrar('avaliacao_google', modelo, getattr(resp, 'usage', None))
        partes = [b.text for b in resp.content
                  if getattr(b, 'type', '') == 'text']
        texto = ' '.join(p.strip() for p in partes if p).strip()
        return (texto or None), ('' if texto else 'A IA nao retornou texto.')
    except Exception as e:  # noqa: BLE001
        logger.warning('google_reviews: rascunho IA falhou: %s', e)
        return None, 'Falha ao gerar o rascunho.'


# ── Resumo pro painel ────────────────────────────────────────────────

def resumo(nota=None, sem_resposta=False, limite=200):
    """Dados do painel: KPIs + locations + lista de reviews (filtravel).
    KPIs via agregacao SQL (nao carrega o historico inteiro em memoria)."""
    from sqlalchemy import func

    from app.models import GoogleReview, GoogleReviewLocation
    locations = GoogleReviewLocation.query.order_by(
        GoogleReviewLocation.apelido).all()

    total = db.session.query(func.count(GoogleReview.id)).scalar() or 0
    media_raw = db.session.query(func.avg(GoogleReview.nota)).scalar()
    media = round(float(media_raw), 2) if media_raw is not None else None
    sem_resp = db.session.query(func.count(GoogleReview.id)).filter(
        (GoogleReview.resposta_texto.is_(None))
        | (GoogleReview.resposta_texto == '')).scalar() or 0
    por_nota_rows = (db.session.query(GoogleReview.nota, func.count(GoogleReview.id))
                     .group_by(GoogleReview.nota).all())
    contagem = {n: c for n, c in por_nota_rows}
    por_nota = {n: contagem.get(n, 0) for n in range(1, 6)}

    q = GoogleReview.query
    if nota:
        q = q.filter(GoogleReview.nota == nota)
    if sem_resposta:
        q = q.filter((GoogleReview.resposta_texto.is_(None))
                     | (GoogleReview.resposta_texto == ''))
    reviews = (q.order_by(GoogleReview.criado_em_google.desc().nullslast())
               .limit(limite).all())

    # Mapa location_name -> apelido/loja pra rotular cada review.
    loc_map = {loc.location_name: loc for loc in locations}
    return {
        'conectado': conectado(),
        'disponivel': disponivel(),
        'total': total,
        'media': media,
        'sem_resposta': sem_resp,
        'por_nota': por_nota,
        'locations': locations,
        'loc_map': loc_map,
        'reviews': reviews,
        'filtro_nota': nota,
        'filtro_sem_resposta': sem_resposta,
    }
