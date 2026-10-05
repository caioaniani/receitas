"""Confirmação de entrega somente por leitura, sem novas postagens no Slack."""
import re
from datetime import UTC, datetime
from decimal import Decimal
from urllib.parse import urlsplit

from app.services import slack
from app.utils import BRT

_MAX_PAGINAS = 20
_TAMANHO_PAGINA = 100
_ERROS_CONSULTA = frozenset({
    'not_in_channel', 'channel_not_found', 'missing_scope', 'no_permission',
    'invalid_auth', 'not_authed', 'token_revoked', 'token_expired',
    'account_inactive', 'restricted_action', 'not_allowed_token_type',
    'ratelimited', 'rate_limited', 'service_unavailable', 'internal_error',
    'fatal_error',
})


def _cliente_leitura():
    client = slack._client()
    client.timeout = 5
    client.retry_handlers = []
    return client


def _canal_valido(canal):
    return isinstance(canal, str) and bool(re.fullmatch(r'[CDG][A-Z0-9]{1,79}', canal))


def _ts_valido(ts):
    return (isinstance(ts, str) and len(ts) <= 32
            and bool(re.fullmatch(r'[0-9]+(?:\.[0-9]+)?', ts))
            and Decimal(ts) > 0)


def _indisponivel(codigo='falha_consulta_slack'):
    return {'estado': 'indisponivel', 'codigo_erro': codigo}


def _erro_consulta(exc):
    codigo = slack._codigo_erro_api(exc)
    return _indisponivel(codigo if codigo in _ERROS_CONSULTA else 'falha_consulta_slack')


def _link_mensagem(client, canal, ts):
    try:
        resposta = client.chat_getPermalink(channel=canal, message_ts=ts)
        if resposta.get('ok') is not True:
            return None
        url = resposta.get('permalink')
        if not isinstance(url, str):
            return None
        partes = urlsplit(url)
        host = (partes.hostname or '').lower()
        if (partes.scheme == 'https' and (host == 'slack.com' or host.endswith('.slack.com'))
                and not partes.username and not partes.password
                and partes.port in (None, 443)):
            return url
    except Exception:
        pass
    return None


def info_canal(canal):
    """Nome do canal e se o aplicativo é membro (conversations.info, só
    leitura). Timeout curto e sem retentativa: serve a aviso e diagnóstico.
    Nunca levanta; falha vira {'erro': código}."""
    if not _canal_valido(canal):
        return {'erro': 'canal_invalido'}
    try:
        resposta = _cliente_leitura().conversations_info(channel=canal)
        if resposta.get('ok') is not True:
            return {'erro': 'resposta_slack_invalida'}
        dados = resposta.get('channel')
        if not isinstance(dados, dict):
            return {'erro': 'resposta_slack_invalida'}
        nome = dados.get('name')
        return {'nome': nome if isinstance(nome, str) else None,
                'is_member': dados.get('is_member'),
                'is_archived': dados.get('is_archived'),
                'is_private': dados.get('is_private')}
    except Exception as exc:  # noqa: BLE001 — consulta opcional; o código basta
        return {'erro': _erro_consulta(exc)['codigo_erro']}


def permalink(canal, ts):
    """Link oficial de uma mensagem conhecida; falhas não confirmam ausência."""
    if not _canal_valido(canal) or not _ts_valido(ts):
        return None
    try:
        return _link_mensagem(_cliente_leitura(), canal, ts)
    except Exception:
        return None


def verificar_mensagem(canal, texto, desde):
    """Busca texto exato do bot no dia da última tentativa (naive significa BRT).

    Consulta desde o início desse dia em BRT, para não perder a postagem por
    pequena diferença entre o relógio do servidor e o timestamp do Slack.
    Só retorna ``ausente`` ao concluir todo o intervalo consultado. Erros,
    histórico limitado, paginação incompleta e respostas malformadas deixam a
    entrega indisponível para confirmação, nunca autorizam um reenvio.
    """
    if not _canal_valido(canal) or not isinstance(texto, str) or not texto:
        return _indisponivel('parametros_invalidos')
    if not isinstance(desde, datetime):
        return _indisponivel('data_tentativa_invalida')
    try:
        if desde.tzinfo is None:
            desde = desde.replace(tzinfo=BRT)
        inicio_dia = desde.astimezone(BRT).replace(hour=0, minute=0, second=0, microsecond=0)
        oldest = f'{int(inicio_dia.timestamp())}.000000'
        latest = f'{datetime.now(UTC).timestamp():.6f}'
        if not _ts_valido(oldest) or desde.timestamp() > float(latest):
            return _indisponivel('data_tentativa_invalida')
        client = _cliente_leitura()
        autenticacao = client.auth_test()
        autor = autenticacao.get('user_id')
        if (autenticacao.get('ok') is not True or not isinstance(autor, str)
                or not re.fullmatch(r'[UW][A-Z0-9]{1,79}', autor)):
            return _indisponivel('bot_nao_identificado')
        encontradas = set()
        cursores = set()
        cursor = None
        for _ in range(_MAX_PAGINAS):
            parametros = {'channel': canal, 'oldest': oldest, 'latest': latest,
                          'inclusive': True, 'limit': _TAMANHO_PAGINA}
            if cursor:
                parametros['cursor'] = cursor
            resposta = client.conversations_history(**parametros)
            if resposta.get('ok') is not True:
                return _indisponivel('resposta_slack_invalida')
            if resposta.get('is_limited'):
                return _indisponivel('historico_limitado')
            if resposta.get('channel', canal) != canal:
                return _indisponivel('canal_divergente')
            mensagens = resposta.get('messages')
            if not isinstance(mensagens, list) or len(mensagens) > _TAMANHO_PAGINA:
                return _indisponivel('resposta_slack_invalida')
            for mensagem in mensagens:
                if not isinstance(mensagem, dict):
                    return _indisponivel('resposta_slack_invalida')
                if mensagem.get('text') != texto:
                    continue
                usuario = mensagem.get('user')
                if not isinstance(usuario, str) or not re.fullmatch(r'[UW][A-Z0-9]{1,79}', usuario):
                    return _indisponivel('autor_nao_identificado')
                if usuario != autor:
                    continue
                ts = mensagem.get('ts')
                if not _ts_valido(ts) or mensagem.get('channel', canal) != canal:
                    return _indisponivel('resposta_slack_invalida')
                if Decimal(oldest) <= Decimal(ts) <= Decimal(latest):
                    encontradas.add(ts)
            metadata = resposta.get('response_metadata', {})
            if not isinstance(metadata, dict):
                return _indisponivel('paginacao_invalida')
            cursor = metadata.get('next_cursor', '')
            has_more = resposta.get('has_more')
            if not isinstance(cursor, str) or not isinstance(has_more, bool):
                return _indisponivel('paginacao_invalida')
            cursor = cursor.strip()
            if cursor:
                if cursor in cursores:
                    return _indisponivel('paginacao_repetida')
                cursores.add(cursor)
                continue
            if has_more:
                return _indisponivel('paginacao_incompleta')
            if len(encontradas) > 1:
                return {'estado': 'ambiguo', 'codigo_erro': 'mensagens_duplicadas'}
            if not encontradas:
                return {'estado': 'ausente'}
            ts = encontradas.pop()
            resultado = {'estado': 'encontrada', 'ts': ts, 'canal': canal}
            url = _link_mensagem(client, canal, ts)
            if url:
                resultado['url'] = url
            return resultado
        return _indisponivel('limite_paginacao')
    except Exception as exc:
        return _erro_consulta(exc)
