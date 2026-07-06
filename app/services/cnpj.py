"""Consulta de CNPJ na base pública da Receita Federal (06/07/2026).

Alimenta o "Buscar" do cadastro de cliente B2B: digitou o CNPJ → preenche
razão social, endereço fiscal estruturado (o que a NF-e exige), e-mail e
telefone — mesmo comportamento do Tiny.

Dois provedores públicos, sem chave, em cascata:
  1. BrasilAPI  (https://brasilapi.com.br/api/cnpj/v1/<cnpj>)
  2. minhareceita.org (fallback — a BrasilAPI já degradou em silêncio no
     caso do frete/CEP em 05/07/2026; aqui não confiamos em provedor único)

Devolve dict NORMALIZADO (chaves nossas, independentes do provedor) ou
{'erro': ...}. Nunca levanta exceção pro caller.
"""
import logging
import re

import requests

logger = logging.getLogger(__name__)

_TIMEOUT = 10


def _so_digitos(s):
    return re.sub(r'\D', '', s or '')


def _normalizar(d):
    """Mapeia o JSON dos provedores (mesmos nomes de campo nos dois) pro
    nosso formato. Campos ausentes viram ''."""
    def _s(k):
        v = d.get(k)
        return str(v).strip() if v is not None else ''

    tel = _so_digitos(_s('ddd_telefone_1'))
    logradouro = ' '.join(x for x in (_s('descricao_tipo_de_logradouro'),
                                      _s('logradouro')) if x)
    # minhareceita manda o CEP como NÚMERO — zero à esquerda some
    # (01001000 vira 1001000). zfill devolve os 8 dígitos.
    cep = _so_digitos(_s('cep'))
    if cep:
        cep = cep.zfill(8)
    return {
        'razao_social': _s('razao_social'),
        'nome_fantasia': _s('nome_fantasia'),
        'email': _s('email').lower(),
        'telefone': tel,
        'logradouro': logradouro,
        'numero': _s('numero'),
        'complemento': _s('complemento'),
        'bairro': _s('bairro'),
        'cep': cep,
        'cidade': _s('municipio'),
        'uf': _s('uf').upper(),
        'situacao': _s('descricao_situacao_cadastral'),
    }


def _consultar_url(url):
    """GET num provedor. Devolve dict do JSON, 'nao_encontrado' (404) ou
    None (erro/transiente — tenta o próximo provedor)."""
    try:
        r = requests.get(url, timeout=_TIMEOUT,
                         headers={'Accept': 'application/json'})
    except requests.RequestException as exc:
        logger.warning('cnpj: %s falhou (%s)', url, type(exc).__name__)
        return None
    if r.status_code == 404:
        return 'nao_encontrado'
    if r.status_code != 200:
        logger.warning('cnpj: %s HTTP %s', url, r.status_code)
        return None
    try:
        d = r.json()
    except ValueError:
        logger.warning('cnpj: %s resposta nao-JSON', url)
        return None
    return d if isinstance(d, dict) else None


def consultar(cnpj):
    """Consulta o CNPJ nos provedores em cascata. Devolve o dict
    normalizado (com 'cnpj' incluso) ou {'erro': mensagem}."""
    digitos = _so_digitos(cnpj)
    if len(digitos) != 14:
        return {'erro': 'CNPJ deve ter 14 dígitos.'}
    urls = (f'https://brasilapi.com.br/api/cnpj/v1/{digitos}',
            f'https://minhareceita.org/{digitos}')
    achou_404 = False
    for url in urls:
        d = _consultar_url(url)
        if d == 'nao_encontrado':
            achou_404 = True
            continue
        if d and d.get('razao_social'):
            out = _normalizar(d)
            out['cnpj'] = digitos
            return out
    if achou_404:
        return {'erro': 'CNPJ não encontrado na base da Receita.'}
    return {'erro': 'Consulta de CNPJ indisponível no momento — preencha '
                    'manualmente ou tente de novo.'}
