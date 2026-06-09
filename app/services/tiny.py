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


def _get(endpoint, params=None):
    """POST no Tiny (a API v2 usa POST com form-data). Devolve dict do JSON ou
    None em qualquer falha. Tiny envolve tudo em {'retorno': {'status': ...}}.

    Faz 1 retry em erros TRANSIENTES (HTTP 429/5xx ou timeout). Glitches do
    Tiny aconteciam silenciosamente — o bot de NF recusava o cliente com
    'nao encontrei' (visto em prod 2026-06-09)."""
    if not disponivel():
        return None
    token = current_app.config['TINY_API_TOKEN'].strip()
    url = f'{BASE}/{endpoint}'
    data = {'token': token, 'formato': 'JSON'}
    data.update(params or {})

    for tentativa in (1, 2):
        try:
            r = requests.post(url, data=data, timeout=12)
        except requests.RequestException as exc:
            logger.warning('tiny %s tentativa %d: %s', endpoint, tentativa, exc)
            if tentativa == 1:
                continue
            logger.error('tiny %s falhou em ambas tentativas: %s', endpoint, exc)
            return None
        if r.status_code in (429, 500, 502, 503, 504) and tentativa == 1:
            logger.warning('tiny %s HTTP %s — retry', endpoint, r.status_code)
            continue
        if r.status_code not in (200, 201):
            logger.warning('tiny %s: HTTP %s', endpoint, r.status_code)
            return None
        try:
            payload = r.json()
        except ValueError:
            logger.warning('tiny %s: resposta nao-JSON', endpoint)
            return None
        retorno = payload.get('retorno') if isinstance(payload, dict) else None
        if not isinstance(retorno, dict):
            return None
        status = (retorno.get('status') or '').lower()
        if status not in ('ok', '1'):
            erros = retorno.get('erros') or retorno.get('registros') or []
            logger.warning('tiny %s erro: status=%s erros=%s',
                           endpoint, status, str(erros)[:200])
            return None
        return retorno
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
