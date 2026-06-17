"""Cliente da API Tiny ERP (v2) — pra o bot consultar a NF do cliente.

Tiny v2: doc em https://www.tiny.com.br/ajuda/api2. Auth simples: token no
body/query. Endpoints usados aqui:
  - pedidos.pesquisa.php    busca pedidos por CPF/numero
  - pedido.obter.php        detalhes de UM pedido (inclui nota_fiscal.id)
  - nota.fiscal.obter.link.php   devolve URL temporaria do DANFE em PDF

Token: env var TINY_API_TOKEN (gerado em Painel Tiny -> Configuracoes -> API).
Se nao houver token, todas as funcoes retornam None (feature fica dormente).
"""
import logging
import threading
import time

import requests
from flask import current_app

logger = logging.getLogger(__name__)

BASE = 'https://api.tiny.com.br/api2'

# Causa da ULTIMA falha de `_get` por thread. Lida por `buscar_pedido_*` pra
# propagar no `diag` (e dai pro NFLog). Thread-local porque o webhook do bot
# processa em threads — sem isolamento, requests concorrentes embaralham o
# erro reportado.
_falha = threading.local()


def _registrar_falha(motivo):
    _falha.motivo = motivo


def _consumir_falha():
    motivo = getattr(_falha, 'motivo', None)
    _falha.motivo = None
    return motivo


def disponivel():
    return bool((current_app.config.get('TINY_API_TOKEN') or '').strip())


_TIMEOUT = 12
_RETRY_BACKOFF = (1.0, 2.0)   # delays antes da 2a e 3a tentativas
_HTTP_TRANSIENTES = (408, 429, 500, 502, 503, 504)


def _registros(retorno):
    """Normaliza `retorno.registros` numa LISTA de dicts 'registro'.

    A API v2 do Tiny é inconsistente: às vezes manda
    `registros: [{'registro': {...}}]` (lista), às vezes
    `registros: {'registro': {...}}` ou `{'registro': [...]}` (dict). Sem
    isso, `registros[0]` num dict estoura KeyError: 0 (bug real em prod)."""
    regs = (retorno or {}).get('registros')
    bruto = []
    if isinstance(regs, list):
        bruto = regs
    elif isinstance(regs, dict):
        # {'registro': {...}} ou {'registro': [...]}
        r = regs.get('registro', regs)
        bruto = r if isinstance(r, list) else [r]
    out = []
    for r in bruto:
        reg = r.get('registro') if isinstance(r, dict) and 'registro' in r else r
        if isinstance(reg, list):
            out.extend(x for x in reg if isinstance(x, dict))
        elif isinstance(reg, dict):
            out.append(reg)
    if not out and isinstance((retorno or {}).get('registro'), dict):
        out.append(retorno['registro'])
    return out


def _extrair_erros(retorno):
    """Junta as mensagens de erro de um retorno do Tiny (vêm em formatos
    diferentes: retorno.erros[].erro, ou nos registros)."""
    msgs = []
    if not isinstance(retorno, dict):
        return ''
    for e in (retorno.get('erros') or []):
        if isinstance(e, dict):
            msgs.append(str(e.get('erro') or e.get('descricao') or e))
        else:
            msgs.append(str(e))
    for reg in _registros(retorno):
        for e in (reg.get('erros') or []):
            if isinstance(e, dict):
                msgs.append(str(e.get('erro') or e))
            else:
                msgs.append(str(e))
    if retorno.get('codigo_erro'):
        msgs.append(f"cod {retorno['codigo_erro']}")
    return '; '.join(m for m in msgs if m)[:400]


def _get(endpoint, params=None, retornar_erro=False):
    """POST no Tiny (a API v2 usa POST com form-data). Devolve dict do JSON ou
    None em qualquer falha. Tiny envolve tudo em {'retorno': {'status': ...}}.

    `retornar_erro=True`: em vez de None quando o Tiny responde status de
    erro, devolve o `retorno` completo (pra o caller ler `.registros[].erros`
    e propagar a mensagem real — usado na emissão de NF).

    Faz 3 tentativas com backoff (1s, 2s) em erros TRANSIENTES (HTTP 408/429/5xx
    ou timeout/connection error). Em prod 2026-06-09 o bot pegou janelas de
    intermitencia em que 1 retry nao bastou — o Tiny tossiu nas duas e o
    cliente foi pra atendente. Registra a causa exata em `_registrar_falha`
    pra propagar no NFLog (debug rapido)."""
    if not disponivel():
        _registrar_falha('TINY_API_TOKEN ausente')
        return None
    token = current_app.config['TINY_API_TOKEN'].strip()
    url = f'{BASE}/{endpoint}'
    data = {'token': token, 'formato': 'JSON'}
    data.update(params or {})

    ultima_causa = 'desconhecida'
    tentativas = len(_RETRY_BACKOFF) + 1
    for i in range(tentativas):
        if i > 0:
            time.sleep(_RETRY_BACKOFF[i - 1])
        try:
            r = requests.post(url, data=data, timeout=_TIMEOUT)
        except requests.Timeout as exc:
            ultima_causa = f'timeout ({_TIMEOUT}s)'
            logger.warning('tiny %s tentativa %d: timeout (%s)', endpoint, i + 1, exc)
            continue
        except requests.RequestException as exc:
            ultima_causa = f'{type(exc).__name__}'
            logger.warning('tiny %s tentativa %d: %s: %s',
                           endpoint, i + 1, type(exc).__name__, exc)
            continue
        if r.status_code in _HTTP_TRANSIENTES:
            ultima_causa = f'HTTP {r.status_code}'
            logger.warning('tiny %s tentativa %d: HTTP %s — retry',
                           endpoint, i + 1, r.status_code)
            continue
        if r.status_code not in (200, 201):
            _registrar_falha(f'HTTP {r.status_code} (nao-transient)')
            logger.warning('tiny %s: HTTP %s', endpoint, r.status_code)
            return None
        try:
            payload = r.json()
        except ValueError:
            _registrar_falha('resposta nao-JSON')
            logger.warning('tiny %s: resposta nao-JSON', endpoint)
            return None
        retorno = payload.get('retorno') if isinstance(payload, dict) else None
        if not isinstance(retorno, dict):
            _registrar_falha('payload sem .retorno')
            return None
        status = (retorno.get('status') or '').lower()
        if status not in ('ok', '1'):
            detalhe = _extrair_erros(retorno)
            _registrar_falha(detalhe or f'retorno.status={status!r}')
            logger.warning('tiny %s erro: status=%s detalhe=%s',
                           endpoint, status, detalhe[:200])
            return retorno if retornar_erro else None
        return retorno
    # esgotou todas as tentativas
    _registrar_falha(f'{ultima_causa} (apos {tentativas} tentativas)')
    logger.error('tiny %s falhou apos %d tentativas: %s',
                 endpoint, tentativas, ultima_causa)
    return None


def _so_digitos(s):
    return ''.join(c for c in (s or '') if c.isdigit())


def buscar_pedido_por_cpf_e_numero(cpf, numero, diag=None):
    """Procura UM pedido especifico no Tiny pela intersecao CPF + numero.
    E o caminho seguro pra o bot — sem CPF, nao retorna nada de outro cliente.

    Retorna dict com {'id', 'numero', 'situacao', 'nota_fiscal_id',
    'origem'} ou None se nao achar / erro.

    IMPORTANTE: a v2 do Tiny IGNORA os params `numero`/`numero_ecommerce`/
    `numero_ordem_compra` em `pedidos.pesquisa.php` — esses filtros nao
    funcionam, so o `cpf_cnpj` filtra. Por isso a gente lista TODOS os pedidos
    do CPF e procura pelo numero no codigo. Limite de 5 paginas (500 pedidos)
    cobre 99% dos casos de atendimento; cliente com mais que isso e fora da
    norma (atacadista, B2B).

    Tambem: `pesquisa.php` nao traz `nota_fiscal_id` nem `origem` — esses
    so vem em `pedido.obter.php`. Por isso, depois de achar pela pesquisa,
    a gente faz a segunda chamada pra resolver os detalhes."""
    cpf_d = _so_digitos(cpf)
    # Cliente costuma colar o numero como '#XXXX' (assim aparece no email do
    # VNDA). Tira o # e espaços antes de buscar — o Tiny indexa sem o prefixo.
    numero = (numero or '').strip().lstrip('#').strip()
    if not cpf_d or not numero:
        return None

    nlow = numero.lower()
    candidato = None
    paginas_lidas = 0
    pedidos_vistos = 0
    api_falhou_em = None
    for pagina in range(1, 6):  # ate 5 paginas = ate 500 pedidos
        retorno = _get('pedidos.pesquisa.php',
                       params={'cpf_cnpj': cpf_d, 'pagina': str(pagina)})
        if not retorno:
            # `_get` ja loga a causa (HTTP X, retry, etc). Para o caller saber
            # que esta diferenciacao importa, marca diag — sem isso, "nao bateu"
            # se confunde com "API caiu" (ja causou bug em prod).
            api_falhou_em = pagina
            break
        paginas_lidas += 1
        pedidos = retorno.get('pedidos') or []
        if not pedidos:
            break
        pedidos_vistos += len(pedidos)
        for item in pedidos:
            p = item.get('pedido') if isinstance(item, dict) else None
            if not isinstance(p, dict):
                continue
            n_interno = str(p.get('numero') or '').strip().lower()
            n_ecom = str(p.get('numero_ecommerce') or '').strip().lower()
            n_oc = str(p.get('numero_ordem_compra') or '').strip().lower()
            if nlow in (n_interno, n_ecom, n_oc):
                candidato = p
                break
        if candidato:
            break
        # Total de paginas? Se o Tiny retornou < 100, e a ultima.
        if len(pedidos) < 100:
            break

    if isinstance(diag, dict):
        diag['paginas_lidas'] = paginas_lidas
        diag['pedidos_vistos'] = pedidos_vistos
        diag['api_falhou_em_pagina'] = api_falhou_em

    if not candidato:
        logger.info('tiny: 0 pedidos batendo p/ cpf=...%s numero=%r '
                    '(paginas=%d, vistos=%d, api_falhou=%s)',
                    cpf_d[-4:], numero, paginas_lidas, pedidos_vistos,
                    api_falhou_em)
        return None

    # `pesquisa.php` nao traz nota_fiscal nem origem completos. Buscar detalhe.
    pedido_id = str(candidato.get('id') or '')
    detalhe = obter_pedido_detalhe(pedido_id) or {}

    # A v2 do Tiny retorna o ID da NF como campo SOLTO `id_nota_fiscal` no
    # pedido (nao como dict aninhado). Fallback pros nomes que docs antigas
    # mencionam, por defesa.
    nf_id = (str(detalhe.get('id_nota_fiscal') or '').strip()
             or str(((detalhe.get('nota_fiscal') or {})).get('id') or '').strip()
             or str(((candidato.get('nota_fiscal') or {})).get('id') or '').strip())

    # `origem`/`ecommerce` na v2 pode vir como dict {nome,...} OU string. Normaliza.
    def _txt(v):
        if isinstance(v, dict):
            return str(v.get('nome') or v.get('descricao') or '').strip()
        return str(v or '').strip()

    return {
        'id': pedido_id,
        'numero': str(candidato.get('numero') or ''),
        'numero_ecommerce': str(candidato.get('numero_ecommerce') or ''),
        'situacao': detalhe.get('situacao') or candidato.get('situacao') or '',
        'data_pedido': (detalhe.get('data_pedido')
                        or candidato.get('data_pedido') or ''),
        'origem': (_txt(detalhe.get('origem'))
                    or _txt(detalhe.get('ecommerce'))
                    or _txt(candidato.get('origem'))),
        'nota_fiscal_id': nf_id,
        'nota_fiscal_situacao': '',
    }


def obter_pedido_detalhe(pedido_id):
    """pedido.obter.php — traz detalhes (inclui nota_fiscal se ja emitida)."""
    if not pedido_id:
        return None
    retorno = _get('pedido.obter.php', params={'id': str(pedido_id)})
    if not retorno:
        return None
    pedido = retorno.get('pedido') or {}
    return pedido if isinstance(pedido, dict) else None


def listar_produtos(max_paginas=20):
    """Lista os produtos do Tiny (produtos.pesquisa.php), paginado. Devolve
    [{'sku', 'nome', 'tiny_id'}] — usado pra sugerir o SKU por nome no
    mapeamento da loja (Fase 5). Lista vazia se sem token/erro."""
    out = []
    for pagina in range(1, max_paginas + 1):
        retorno = _get('produtos.pesquisa.php', params={'pagina': str(pagina)})
        if not retorno:
            break
        produtos = retorno.get('produtos') or []
        if not produtos:
            break
        for item in produtos:
            p = item.get('produto') if isinstance(item, dict) else None
            if not isinstance(p, dict):
                continue
            sku = str(p.get('codigo') or '').strip()
            nome = str(p.get('nome') or '').strip()
            if sku or nome:
                out.append({'sku': sku, 'nome': nome,
                            'tiny_id': str(p.get('id') or '')})
        # Tiny v2: paginas de 100. Menos que isso = ultima.
        if len(produtos) < 100:
            break
    return out


def incluir_pedido(pedido_dict):
    """pedido.incluir.php — cria um pedido no Tiny (formato JSON na v2).
    Devolve {ok, id, numero, erro}. Em erro, `erro` traz a mensagem real do
    Tiny (não só 'ver logs')."""
    import json as _json
    retorno = _get('pedido.incluir.php',
                   params={'pedido': _json.dumps({'pedido': pedido_dict})},
                   retornar_erro=True)
    if not retorno:
        return {'ok': False, 'erro': _consumir_falha() or 'sem resposta do Tiny'}
    regs = _registros(retorno)
    reg = regs[0] if regs else {}
    pid = str(reg.get('id') or '').strip()
    if not pid:
        return {'ok': False, 'erro': _extrair_erros(retorno) or 'sem id no retorno'}
    return {'ok': True, 'id': pid, 'numero': str(reg.get('numero') or '')}


def gerar_nota_fiscal_pedido(pedido_id, modelo='NFe'):
    """gerar.nota.fiscal.pedido.php — cria a NF (rascunho) a partir de um
    pedido no Tiny. `modelo` = 'NFe' (modelo 55, e-commerce com entrega) ou
    'NFCe'. Devolve {ok, id_nota_fiscal, erro}."""
    retorno = _get('gerar.nota.fiscal.pedido.php',
                   params={'id': str(pedido_id), 'modelo': modelo},
                   retornar_erro=True)
    if not retorno:
        return {'ok': False, 'erro': _consumir_falha() or 'sem resposta'}
    reg = retorno.get('registro') or {}
    nf_id = str(reg.get('id_nota_fiscal') or reg.get('id') or '').strip()
    if not nf_id:
        return {'ok': False, 'erro': _extrair_erros(retorno) or 'sem id_nota_fiscal'}
    return {'ok': True, 'id_nota_fiscal': nf_id,
            'numero': str(reg.get('numero') or '')}


def emitir_nota_fiscal(nota_id):
    """nota.fiscal.emitir.php — autoriza a NF na SEFAZ (sai do rascunho).
    Em homologação não tem efeito legal. Devolve {ok, status, erro?}."""
    retorno = _get('nota.fiscal.emitir.php', params={'id': str(nota_id)},
                   retornar_erro=True)
    if not retorno:
        return {'ok': False, 'erro': _consumir_falha() or 'sem resposta'}
    status = (retorno.get('status_processamento')
              or retorno.get('status') or '').lower()
    ok = status in ('ok', '1', '100', 'emitida', 'autorizada')
    return {'ok': ok, 'status': status,
            'erro': None if ok else (_extrair_erros(retorno) or status)}


def obter_nota_fiscal(nota_id):
    """nota.fiscal.obter.php — situação atual da NF (chave, número,
    autorizada/rejeitada). Devolve dict cru do Tiny ou None."""
    if not nota_id:
        return None
    retorno = _get('nota.fiscal.obter.php', params={'id': str(nota_id)})
    if not retorno:
        return None
    nf = retorno.get('nota_fiscal') or retorno.get('registro') or {}
    return nf if isinstance(nf, dict) else None


def obter_link_nota_fiscal(nota_id):
    """Retorna URL temporaria do DANFE em PDF, ou None.
    A URL e publica mas com expiracao do lado do Tiny."""
    if not nota_id:
        return None
    retorno = _get('nota.fiscal.obter.link.php', params={'id': str(nota_id)})
    if not retorno:
        return None
    link = (retorno.get('link_nfe') or retorno.get('link') or '').strip()
    return link or None
