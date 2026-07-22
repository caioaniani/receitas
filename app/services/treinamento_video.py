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


def salvar_stream(stream, treino_id, nome_original):
    """Grava um STREAM bruto (corpo da request, sem multipart) no volume, em
    BLOCOS — sem carregar o vídeo na RAM. `nome_original` só dá a extensão.
    Retorna a `video_ref`. ValueError se a extensão não for suportada. É o
    caminho do upload por XHR (corpo cru), que evita o parse de formulário."""
    ext = _ext(nome_original)
    if ext not in EXTENSOES_OK:
        raise ValueError(
            f'Formato de vídeo não suportado: {ext or "?"}. '
            'Use MP4, WebM ou MOV.')
    ref = f'treino-{int(treino_id)}-{secrets.token_hex(8)}{ext}'
    destino = os.path.join(media_dir(), ref)
    with open(destino, 'wb') as out:
        while True:
            bloco = stream.read(_CHUNK)
            if not bloco:
                break
            out.write(bloco)
    return ref


def salvar_video(file_storage, treino_id):
    """Grava um FileStorage (upload multipart) no volume. Delega pro
    `salvar_stream` — mantido pra compatibilidade/testes."""
    return salvar_stream(file_storage.stream, treino_id,
                         file_storage.filename or '')


# ── Upload por PEDAÇOS (chunked) ────────────────────────────────────────
# Vídeo grande (5-10 min) não sobe numa request só: o teto global de 25 MB, o
# timeout do worker (gthread, 120s) e limites de proxy cortam a conexão no meio
# e o navegador mostra "erro de rede". A cura canônica é fatiar o arquivo no
# cliente e mandar cada pedaço numa request pequena e rápida — assim NENHUM
# desses limites é tocado. Cada pedaço é anexado a um arquivo temporário
# `.part-<treino>-<token>` no volume; o último pedaço fecha e renomeia.
_TOKEN_RE = re.compile(r'^[0-9a-f]{8,64}$')


def _part_path(treino_id, token):
    """Caminho do temporário do upload por pedaços, BLINDADO: o token é hex
    puro (gerado pelo cliente) — barra/traversal levanta ValueError."""
    if not _TOKEN_RE.match(token or ''):
        raise ValueError('Sessão de upload inválida.')
    return os.path.join(media_dir(), f'.part-{int(treino_id)}-{token}')


def anexar_chunk(dados, treino_id, token, indice, nome_original, max_bytes):
    """Anexa um PEDAÇO (bytes já lidos do corpo da request) ao temporário.
    `indice==0` começa do zero (permite re-tentar do início); os demais anexam
    em ordem. Valida a extensão já no primeiro pedaço (falha cedo, antes de
    subir centenas de MB) e o teto ACUMULADO a cada pedaço.

    Recebe BYTES (não stream) de propósito: a rota lê o corpo inteiro do pedaço
    ANTES de tocar em disco, pra qualquer falha de escrita virar um HTTP legível
    em vez de resetar a conexão (que o navegador mostraria como "erro de rede").
    ValueError = culpa do cliente (extensão/teto); OSError sobe pra rota tratar
    como falha de servidor."""
    if indice == 0:
        ext = _ext(nome_original)
        if ext not in EXTENSOES_OK:
            raise ValueError(
                f'Formato de vídeo não suportado: {ext or "?"}. '
                'Use MP4, WebM ou MOV.')
    destino = _part_path(treino_id, token)
    modo = 'wb' if indice == 0 else 'ab'
    with open(destino, modo) as out:
        out.write(dados)
    if os.path.getsize(destino) > int(max_bytes):
        try:
            os.remove(destino)
        except OSError:
            pass
        raise ValueError('Vídeo maior que o limite permitido.')


def finalizar_chunk(treino_id, token, nome_original):
    """Fecha o upload por pedaços: renomeia o temporário pro nome final e
    devolve a `video_ref`. ValueError se o temporário sumiu (upload
    incompleto) ou a extensão for inválida."""
    origem = _part_path(treino_id, token)
    if not os.path.exists(origem):
        raise ValueError('Upload incompleto — reenvie o vídeo.')
    ext = _ext(nome_original)
    if ext not in EXTENSOES_OK:               # blinda o rename (já validado no 0)
        try:
            os.remove(origem)
        except OSError:
            pass
        raise ValueError('Formato de vídeo não suportado.')
    ref = f'treino-{int(treino_id)}-{secrets.token_hex(8)}{ext}'
    os.replace(origem, os.path.join(media_dir(), ref))
    return ref


def limpar_parciais(horas=12):
    """Remove restos de upload por pedaços (`.part-*`) mais velhos que N horas
    — worker caiu no meio, aba fechada. Best-effort, nunca levanta."""
    import time
    try:
        limite = time.time() - horas * 3600
        for nome in os.listdir(media_dir()):
            if not nome.startswith('.part-'):
                continue
            p = os.path.join(media_dir(), nome)
            try:
                if os.path.getmtime(p) < limite:
                    os.remove(p)
            except OSError:
                pass
    except OSError:
        pass


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
