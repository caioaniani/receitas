"""Armazenamento e streaming dos vídeos de treinamento (self-host, decisão do
dono 24/07/2026).

Os vídeos vivem no VOLUME do Railway (config `TREINAMENTO_MEDIA_DIR` = /data em
prod, ~/.padaria/treinamento em dev). Este serviço:
- SALVA o upload em BLOCOS, sem carregar o arquivo inteiro na memória do worker
  (senão um vídeo de centenas de MB estoura a RAM);
- SERVE com suporte a HTTP Range (206), pro `<video>` começar na hora e o
  funcionário arrastar a barra sem baixar tudo.

A fonte é TROCÁVEL de propósito: migrar os bytes pro Cloudflare R2 (tráfego de
saída grátis) depois é só reimplementar salvar/servir aqui, sem mexer no resto.
"""
import os
import re
import secrets

from flask import Response, current_app, request

# Formatos que o <video> do navegador toca nativamente.
EXTENSOES_OK = {'.mp4', '.webm', '.m4v', '.mov', '.ogg'}
_MIME = {
    '.mp4': 'video/mp4', '.m4v': 'video/mp4', '.mov': 'video/quicktime',
    '.webm': 'video/webm', '.ogg': 'video/ogg',
}
_CHUNK = 512 * 1024   # 512 KB por bloco (upload e streaming)
_RANGE_RE = re.compile(r'bytes=(\d*)-(\d*)')


def media_dir():
    """Pasta dos vídeos (cria se não existe)."""
    d = current_app.config['TREINAMENTO_MEDIA_DIR']
    os.makedirs(d, exist_ok=True)
    return d


def _ext(nome):
    return os.path.splitext(nome or '')[1].lower()


def extensao_valida(nome):
    return _ext(nome) in EXTENSOES_OK


def caminho_video(ref):
    """Caminho absoluto do arquivo, BLINDADO contra path traversal: `ref` é só
    o nome-base gerado por nós — se vier com '/' ou '..', devolve None."""
    if not ref:
        return None
    base = os.path.basename(ref)
    if base != ref:                      # tinha barra/traversal
        return None
    return os.path.join(media_dir(), base)


def salvar_video(file_storage, treino_id):
    """Grava o upload no volume, em BLOCOS. Retorna o nome do arquivo salvo (a
    `video_ref` do Treinamento). ValueError se a extensão não for suportada."""
    ext = _ext(file_storage.filename or '')
    if ext not in EXTENSOES_OK:
        raise ValueError(
            f'Formato de vídeo não suportado: {ext or "?"}. '
            'Use MP4, WebM ou MOV.')
    ref = f'treino-{int(treino_id)}-{secrets.token_hex(8)}{ext}'
    destino = os.path.join(media_dir(), ref)
    stream = file_storage.stream
    with open(destino, 'wb') as out:
        while True:
            bloco = stream.read(_CHUNK)
            if not bloco:
                break
            out.write(bloco)
    return ref


def remover_video(ref):
    """Apaga o arquivo (best-effort)."""
    try:
        caminho = caminho_video(ref)
        if caminho and os.path.exists(caminho):
            os.remove(caminho)
    except OSError:
        pass


def resposta_range(ref):
    """Response do vídeo com HTTP Range. Com header `Range` devolve 206 +
    Content-Range (trecho); sem, 200 com o arquivo inteiro — sempre em blocos.
    404 se o arquivo sumiu; 416 se o Range for inválido."""
    caminho = caminho_video(ref)
    if not caminho or not os.path.exists(caminho):
        return Response('vídeo não encontrado', status=404)
    tamanho = os.path.getsize(caminho)
    mime = _MIME.get(_ext(ref), 'application/octet-stream')

    inicio, fim, status = 0, tamanho - 1, 200
    range_h = request.headers.get('Range')
    if range_h:
        m = _RANGE_RE.match(range_h.strip())
        if m:
            g1, g2 = m.group(1), m.group(2)
            if not g1 and g2:
                # suffix-range 'bytes=-N': os ÚLTIMOS N bytes (RFC 7233).
                inicio = max(0, tamanho - int(g2))
            else:
                if g1:
                    inicio = int(g1)
                if g2:
                    fim = int(g2)
            if inicio > fim or inicio >= tamanho:
                r = Response(status=416)
                r.headers['Content-Range'] = f'bytes */{tamanho}'
                return r
            fim = min(fim, tamanho - 1)
            status = 206

    comprimento = fim - inicio + 1

    def gerar():
        with open(caminho, 'rb') as f:
            f.seek(inicio)
            restante = comprimento
            while restante > 0:
                bloco = f.read(min(_CHUNK, restante))
                if not bloco:
                    break
                restante -= len(bloco)
                yield bloco

    r = Response(gerar(), status=status, mimetype=mime,
                 direct_passthrough=True)
    r.headers['Accept-Ranges'] = 'bytes'
    r.headers['Content-Length'] = str(comprimento)
    if status == 206:
        r.headers['Content-Range'] = f'bytes {inicio}-{fim}/{tamanho}'
    return r
