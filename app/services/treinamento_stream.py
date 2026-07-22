"""Cloudflare Stream — hospedagem e player dos vídeos de treinamento.

Decisão do dono (24/07/2026): depois que o self-host no volume /data do Railway
esbarrou em permissão (o volume é do root, o app roda como usuário limitado), o
vídeo passou a ir DIRETO do navegador pro Cloudflare Stream (upload direto — o
byte nunca toca o nosso servidor, então some o teto de 25 MB, o timeout do
worker e o volume) e a TOCAR embutido num iframe na NOSSA página: o funcionário
não sai do site.

Contrato (best-effort — nunca derruba o fluxo de negócio):
- `configurado()`            -> há account id + token?
- `criar_upload_direto(nome)`-> {'uid','uploadURL'}; a uploadURL é de uso único
  e é NELA que o navegador sobe o arquivo. ValueError se não configurado / API
  recusou.
- `status(uid)`             -> {'pronto': bool, 'pct': int, 'erro': str|None}.
- `deletar(uid)`            -> best-effort (troca/limpeza de vídeo).
- `embed_url(uid)`/`thumb_url(uid)` -> URLs do player (iframe) e da thumbnail.

Sem as envs, `configurado()` é False e a tela avisa "não configurado" — nada
quebra (mesmo padrão do Spotify). O SEGREDO (token) nunca vai pro navegador: o
browser só recebe a uploadURL de uso único que a própria API devolve.
"""
import re

import requests
from flask import current_app
from sqlalchemy.exc import SQLAlchemyError

from app.models.config import AppConfig

_BASE = 'https://api.cloudflare.com/client/v4'
_TIMEOUT = 20
_KEY_SUBDOMAIN = 'cloudflare_stream_subdomain'


def _cfg():
    return (
        (current_app.config.get('CLOUDFLARE_ACCOUNT_ID') or '').strip(),
        (current_app.config.get('CLOUDFLARE_STREAM_TOKEN') or '').strip(),
    )


def configurado():
    acct, token = _cfg()
    return bool(acct and token)


def _headers():
    _, token = _cfg()
    return {'Authorization': f'Bearer {token}'}


def criar_upload_direto(nome, max_duration_seconds=7200):
    """Pede ao Cloudflare uma URL de upload direto de uso único. O navegador
    sobe o arquivo NELA (não no nosso servidor). Retorna {'uid','uploadURL'}.
    ValueError com mensagem clara se não configurado ou a API recusar."""
    acct, token = _cfg()
    if not (acct and token):
        raise ValueError('Cloudflare Stream não configurado.')
    nome = (nome or 'aula')[:200]
    try:
        r = requests.post(
            f'{_BASE}/accounts/{acct}/stream/direct_upload',
            headers=_headers(),
            json={
                'maxDurationSeconds': int(max_duration_seconds),
                'requireSignedURLs': False,
                'meta': {'name': nome},
            },
            timeout=_TIMEOUT,
        )
    except requests.RequestException as e:
        raise ValueError(f'Falha ao falar com o Cloudflare: {e}') from e
    dados = _json(r)
    if not (r.ok and dados.get('success')):
        raise ValueError(f'Cloudflare recusou o upload: {_erro(dados, r)}')
    res = dados.get('result') or {}
    uid, url = res.get('uid'), res.get('uploadURL')
    if not (uid and url):
        raise ValueError('Cloudflare não devolveu uid/uploadURL.')
    return {'uid': uid, 'uploadURL': url}


def status(uid):
    """Estado do processamento do vídeo (o Cloudflare transcodifica após o
    upload). `pronto` = dá pra assistir; `pct` = % de processamento."""
    acct, token = _cfg()
    if not (acct and token and uid):
        return {'pronto': False, 'pct': 0, 'erro': 'não configurado'}
    try:
        r = requests.get(f'{_BASE}/accounts/{acct}/stream/{uid}',
                         headers=_headers(), timeout=_TIMEOUT)
        dados = _json(r)
    except requests.RequestException as e:
        return {'pronto': False, 'pct': 0, 'erro': str(e)}
    if not (r.ok and dados.get('success')):
        return {'pronto': False, 'pct': 0, 'erro': _erro(dados, r)}
    res = dados.get('result') or {}
    _cachear_subdomain(res)
    st = res.get('status') or {}
    return {
        'pronto': bool(res.get('readyToStream')),
        'pct': _int(st.get('pctComplete')),
        'erro': st.get('errorReasonText') or None,
    }


def deletar(uid):
    """Remove o vídeo do Cloudflare (troca de vídeo / limpeza). Best-effort."""
    acct, token = _cfg()
    if not (acct and token and uid):
        return
    try:
        requests.delete(f'{_BASE}/accounts/{acct}/stream/{uid}',
                        headers=_headers(), timeout=_TIMEOUT)
    except requests.RequestException:
        pass


def subdomain():
    """customer-XXXX.cloudflarestream.com — do config; senão do cache
    (AppConfig), preenchido pela 1ª consulta de status a um vídeo."""
    cfg = (current_app.config.get('CLOUDFLARE_STREAM_SUBDOMAIN') or '').strip()
    if cfg:
        return _normaliza_subdomain(cfg)
    return (AppConfig.get(_KEY_SUBDOMAIN) or '').strip() or None


def embed_url(uid):
    """URL do player em iframe (embutido na nossa página)."""
    sub = subdomain()
    if not (sub and uid):
        return None
    return f'https://{sub}/{uid}/iframe'


def thumb_url(uid):
    sub = subdomain()
    if not (sub and uid):
        return None
    return f'https://{sub}/{uid}/thumbnails/thumbnail.jpg'


# ── internos ────────────────────────────────────────────────────────────
def _normaliza_subdomain(v):
    v = v.strip().replace('https://', '').replace('http://', '').strip('/')
    if '.' not in v:                       # colaram só o código customer-XXXX
        v = f'{v}.cloudflarestream.com'
    return v


def _cachear_subdomain(result):
    """Descobre o subdomínio de entrega pela URL de preview/thumbnail do vídeo
    e cacheia em AppConfig — evita exigir a env CLOUDFLARE_STREAM_SUBDOMAIN.
    A gravação vai em sessão ISOLADA (best-effort) pra nunca contaminar a
    transação de negócio em curso (padrão uso_ia.registrar)."""
    if (current_app.config.get('CLOUDFLARE_STREAM_SUBDOMAIN') or '').strip():
        return
    if AppConfig.get(_KEY_SUBDOMAIN):
        return
    for chave in ('preview', 'thumbnail'):
        m = re.search(r'https?://([a-z0-9-]+\.cloudflarestream\.com)/',
                      result.get(chave) or '')
        if m:
            _gravar_subdomain(m.group(1))
            return


def _gravar_subdomain(valor):
    """Grava o subdomínio em sessão ISOLADA — não commita a sessão de negócio
    (que pode ter writes pendentes no request que chamou status())."""
    from sqlalchemy.orm import Session

    from app.extensions import db
    s = Session(bind=db.engine)
    try:
        row = s.query(AppConfig).filter_by(key=_KEY_SUBDOMAIN).first()
        if row:
            row.value = valor
        else:
            s.add(AppConfig(key=_KEY_SUBDOMAIN, value=valor))
        s.commit()
    except SQLAlchemyError:
        s.rollback()
    finally:
        s.close()


def _json(r):
    try:
        return r.json()
    except ValueError:
        return {}


def _int(x):
    try:
        return int(float(x))
    except (TypeError, ValueError):
        return 0


def _erro(dados, r):
    errs = dados.get('errors') or []
    if errs:
        return '; '.join(str(e.get('message') or e) for e in errs)[:300]
    return f'HTTP {r.status_code}'
