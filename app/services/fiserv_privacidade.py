"""Mantém requisições privadas e jobs Fiserv fora da telemetria externa."""

from urllib.parse import unquote, urlsplit

from flask import has_request_context, request


def caminho_privado(caminho):
    return caminho == '/financeiro/fiserv' or caminho.startswith('/financeiro/fiserv/')


def filtrar_evento(evento, hint=None):
    __tracebackhide__ = True
    tags = evento.get('tags') or {}
    if tags.get('fiserv_privado'):
        return None
    if has_request_context() and caminho_privado(request.path):
        return None
    url = (evento.get('request') or {}).get('url', '')
    try:
        caminho = unquote(urlsplit(url).path)
    except (TypeError, ValueError):
        caminho = ''
    transacao = str(evento.get('transaction') or '')
    if (caminho_privado(caminho)
            or transacao.startswith(('fiserv.', '/financeiro/fiserv'))):
        return None
    # SQL de tabelas privadas não deve compor breadcrumbs de outra requisição.
    breadcrumbs = evento.get('breadcrumbs') or {}
    if isinstance(breadcrumbs, dict):
        breadcrumbs['values'] = [
            item for item in breadcrumbs.get('values', [])
            if 'fiserv' not in str(item).lower()
        ]
    return evento
