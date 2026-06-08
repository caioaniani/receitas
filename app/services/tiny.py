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

import requests
from flask import current_app

logger = logging.getLogger(__name__)

BASE = 'https://api.tiny.com.br/api2'


def disponivel():
    return bool((current_app.config.get('TINY_API_TOKEN') or '').strip())


def _get(endpoint, params=None):
    """POST no Tiny (a API v2 usa POST com form-data). Devolve dict do JSON ou
    None em qualquer falha. Tiny envolve tudo em {'retorno': {'status': ...}}."""
    if not disponivel():
        return None
    token = current_app.config['TINY_API_TOKEN'].strip()
    url = f'{BASE}/{endpoint}'
    data = {'token': token, 'formato': 'JSON'}
    data.update(params or {})
    try:
        r = requests.post(url, data=data, timeout=12)
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
        # Tiny indica erro em retorno.status = 'Erro' OU codigo != 1
        status = (retorno.get('status') or '').lower()
        if status not in ('ok', '1'):
            erros = retorno.get('erros') or retorno.get('registros') or []
            logger.warning('tiny %s erro: status=%s erros=%s',
                           endpoint, status, str(erros)[:200])
            return None
        return retorno
    except requests.RequestException as exc:
        logger.error('tiny %s falhou: %s', endpoint, exc)
        return None


def _so_digitos(s):
    return ''.join(c for c in (s or '') if c.isdigit())


def buscar_pedido_por_cpf_e_numero(cpf, numero):
    """Procura UM pedido especifico no Tiny pela intersecao CPF + numero.
    E o caminho seguro pra o bot — sem CPF, nao retorna nada de outro cliente.

    Retorna dict com {'id', 'numero', 'situacao', 'nota_fiscal_id',
    'origem'} ou None se nao achar / erro."""
    cpf_d = _so_digitos(cpf)
    # Cliente costuma colar o numero como '#XXXX' (assim aparece no email do
    # VNDA). Tira o # e espaços antes de buscar — o Tiny indexa sem o prefixo.
    numero = (numero or '').strip().lstrip('#').strip()
    if not cpf_d or not numero:
        return None

    retorno = _get('pedidos.pesquisa.php',
                   params={'cpf_cnpj': cpf_d, 'numero': numero})
    if not retorno:
        return None

    pedidos = retorno.get('pedidos') or []
    # Tiny embrulha cada item em {'pedido': {...}}
    candidatos = []
    for item in pedidos:
        p = item.get('pedido') if isinstance(item, dict) else None
        if isinstance(p, dict):
            candidatos.append(p)
    # Match exato pelo numero (a pesquisa pode trazer prefixos parecidos).
    # Aceita match case-insensitive — '#d884' (cliente) vs 'D884' (Tiny).
    nlow = numero.lower()
    achados = [p for p in candidatos
               if str(p.get('numero') or '').strip().lower() == nlow]
    if not achados:
        logger.info('tiny: 0 pedidos p/ cpf=...%s numero=%r (retornados=%d)',
                    cpf_d[-4:], numero, len(candidatos))
        return None

    pedido = achados[0]
    return {
        'id': str(pedido.get('id') or ''),
        'numero': str(pedido.get('numero') or ''),
        'situacao': pedido.get('situacao') or '',
        'data_pedido': pedido.get('data_pedido') or '',
        'origem': (pedido.get('origem') or pedido.get('ecommerce') or '').strip(),
        # nota_fiscal pode vir como dict {id, numero} ou ausente
        'nota_fiscal_id': str(((pedido.get('nota_fiscal') or {})
                                .get('id')) or ''),
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
