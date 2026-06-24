#!/usr/bin/env python3
"""Upload de arquivo de backup pro Dropbox.

Usa as mesmas credenciais OAuth do app (DROPBOX_APP_KEY/SECRET/REFRESH_TOKEN),
mas roda standalone — nao precisa importar Flask nem subir o app. Util pra
chamar do backup.sh em cron, antes do app estar disponivel.

Uso:
    python3 deploy/upload_dropbox.py /caminho/arquivo.sql.gz [pasta-no-dropbox]

Se pasta_dropbox nao for passada, usa $DROPBOX_BACKUP_PASTA ou /backups-postgres.

Codigo de saida 0 = sucesso, 1 = falha (script chamador pode detectar).
Arquivos ate ~140MB usam upload simples; acima, usa upload session (chunks
de 100MB). Limite da API: 150GB por arquivo.
"""
import json
import os
import sys
import urllib.error
import urllib.request


UPLOAD_LIMITE_SIMPLES = 140 * 1024 * 1024  # 140 MB
CHUNK_SIZE = 100 * 1024 * 1024              # 100 MB


def _post_json(url, headers, body):
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers={
        **headers,
        'Content-Type': 'application/json',
    })
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def _post_bytes(url, headers, body):
    req = urllib.request.Request(url, data=body, headers=headers)
    with urllib.request.urlopen(req, timeout=300) as r:
        return json.loads(r.read())


def _obter_access_token():
    app_key = (os.environ.get('DROPBOX_APP_KEY') or '').strip()
    app_secret = (os.environ.get('DROPBOX_APP_SECRET') or '').strip()
    refresh = (os.environ.get('DROPBOX_REFRESH_TOKEN') or '').strip()

    # Fallback pro token direto (curto, 4h) — so pra teste
    if not (app_key and app_secret and refresh):
        legacy = (os.environ.get('DROPBOX_ACCESS_TOKEN') or '').strip()
        if legacy:
            return legacy
        raise RuntimeError(
            'Configure DROPBOX_APP_KEY + DROPBOX_APP_SECRET + DROPBOX_REFRESH_TOKEN '
            '(ou DROPBOX_ACCESS_TOKEN como fallback)'
        )

    # OAuth2 refresh — manda como x-www-form-urlencoded
    data = (
        f'grant_type=refresh_token&refresh_token={urllib.request.quote(refresh)}'
    ).encode()
    import base64
    auth_header = base64.b64encode(f'{app_key}:{app_secret}'.encode()).decode()
    req = urllib.request.Request(
        'https://api.dropbox.com/oauth2/token',
        data=data,
        headers={
            'Authorization': f'Basic {auth_header}',
            'Content-Type': 'application/x-www-form-urlencoded',
        },
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        body = json.loads(r.read())
    token = body.get('access_token')
    if not token:
        raise RuntimeError(f'Refresh retornou sem access_token: {body}')
    return token


def _upload_simples(token, local_path, dropbox_path):
    with open(local_path, 'rb') as f:
        body = f.read()
    api_args = json.dumps({
        'path': dropbox_path,
        'mode': 'overwrite',
        'autorename': False,
        'mute': True,
    })
    meta = _post_bytes(
        'https://content.dropboxapi.com/2/files/upload',
        headers={
            'Authorization': f'Bearer {token}',
            'Dropbox-API-Arg': api_args,
            'Content-Type': 'application/octet-stream',
        },
        body=body,
    )
    return meta


def _upload_session(token, local_path, dropbox_path):
    """Upload em chunks pra arquivos grandes."""
    tamanho = os.path.getsize(local_path)
    with open(local_path, 'rb') as f:
        # 1. start
        primeiro = f.read(CHUNK_SIZE)
        start = _post_bytes(
            'https://content.dropboxapi.com/2/files/upload_session/start',
            headers={
                'Authorization': f'Bearer {token}',
                'Dropbox-API-Arg': json.dumps({'close': False}),
                'Content-Type': 'application/octet-stream',
            },
            body=primeiro,
        )
        session_id = start['session_id']
        offset = len(primeiro)

        # 2. append loop
        while offset < tamanho:
            chunk = f.read(CHUNK_SIZE)
            if not chunk:
                break
            ultimo = (offset + len(chunk)) >= tamanho
            if not ultimo:
                _post_bytes(
                    'https://content.dropboxapi.com/2/files/upload_session/append_v2',
                    headers={
                        'Authorization': f'Bearer {token}',
                        'Dropbox-API-Arg': json.dumps({
                            'cursor': {'session_id': session_id, 'offset': offset},
                            'close': False,
                        }),
                        'Content-Type': 'application/octet-stream',
                    },
                    body=chunk,
                )
                offset += len(chunk)
            else:
                # 3. finish (ultimo chunk + commit)
                meta = _post_bytes(
                    'https://content.dropboxapi.com/2/files/upload_session/finish',
                    headers={
                        'Authorization': f'Bearer {token}',
                        'Dropbox-API-Arg': json.dumps({
                            'cursor': {'session_id': session_id, 'offset': offset},
                            'commit': {
                                'path': dropbox_path,
                                'mode': 'overwrite',
                                'autorename': False,
                                'mute': True,
                            },
                        }),
                        'Content-Type': 'application/octet-stream',
                    },
                    body=chunk,
                )
                return meta
    raise RuntimeError('upload_session: arquivo vazio?')


def main(argv):
    if len(argv) < 2:
        print(f'Uso: {argv[0]} <arquivo-local> [pasta-dropbox]', file=sys.stderr)
        return 1

    local = argv[1]
    if not os.path.isfile(local):
        print(f'Arquivo nao encontrado: {local}', file=sys.stderr)
        return 1

    pasta = (argv[2] if len(argv) >= 3 else
             os.environ.get('DROPBOX_BACKUP_PASTA') or
             '/backups-postgres').rstrip('/')
    nome = os.path.basename(local)
    dropbox_path = f'{pasta}/{nome}'

    tamanho = os.path.getsize(local)
    print(f'Upload pro Dropbox: {dropbox_path} ({tamanho / 1024 / 1024:.1f} MB)')

    try:
        token = _obter_access_token()
        if tamanho <= UPLOAD_LIMITE_SIMPLES:
            meta = _upload_simples(token, local, dropbox_path)
        else:
            meta = _upload_session(token, local, dropbox_path)
    except urllib.error.HTTPError as e:
        body = e.read().decode('utf-8', errors='replace')[:500]
        print(f'Falha HTTP {e.code}: {body}', file=sys.stderr)
        return 1
    except Exception as e:  # noqa: BLE001
        print(f'Falha: {type(e).__name__}: {e}', file=sys.stderr)
        return 1

    print(f'OK: {meta.get("path_display") or dropbox_path} ({meta.get("size") or tamanho} bytes)')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
