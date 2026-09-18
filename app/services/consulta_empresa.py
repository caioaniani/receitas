"""Consulta pública para o checkout, sem acessar contatos privados do ERP.

CNPJ.ws: 3 tentativas/minuto por IP, inclusive as que falham. A reserva
atômica compartilha esse orçamento entre workers e termina antes do HTTP.
Cache e limite usam conexões Core próprias: nunca confirmam a sessão do
checkout. A base pública pode ter defasagem; atualizado_em é da fonte.
"""
import logging
import re
from datetime import timedelta

import requests
from sqlalchemy import or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import SQLAlchemyError

from app.extensions import db
from app.models.consulta_empresa import ConsultaEmpresaCache, ConsultaEmpresaLimite
from app.utils import agora

logger = logging.getLogger(__name__)

_PROVEDOR = 'cnpj_ws'
_INTERVALO = timedelta(seconds=21)
_TTL_IE = timedelta(hours=24)
_TTL_PENDENTE = timedelta(minutes=1)
_TIMEOUT = 3
_UFS = frozenset('AC AL AP AM BA CE DF ES GO MA MT MS MG PA PB PR PE PI RJ RN RS RO RR SC SP SE TO'.split())
_AVISO_IE = ('Não foi possível confirmar uma inscrição estadual ativa e única '
             'para esta empresa e UF. Confira a inscrição ou a condição de contribuinte.')


def _texto(valor):
    return valor.strip() if isinstance(valor, str) else ''


def _digitos(valor):
    if not isinstance(valor, (str, int)) or isinstance(valor, bool):
        return ''
    return re.sub(r'[^0-9]', '', str(valor))


def _documento_exato(valor, documento):
    return (isinstance(valor, str) and re.fullmatch(r'[0-9./ -]+', valor) is not None
            and _digitos(valor) == documento)


def _resultado(dados=None, *, origem='', atualizado_em=None, aviso=''):
    return {'dados': dados or {}, 'origem': origem,
            'atualizado_em': atualizado_em, 'aviso': aviso}


def _inserir(tabela, conexao):
    if conexao.dialect.name == 'postgresql':
        return pg_insert(tabela)
    if conexao.dialect.name == 'sqlite':
        return sqlite_insert(tabela)
    raise ValueError('Consulta cadastral requer PostgreSQL ou SQLite.')


def _ler_cache(documento):
    tabela = ConsultaEmpresaCache.__table__
    try:
        with db.engine.connect() as conexao:
            return conexao.execute(select(tabela.c.resultado).where(
                tabela.c.documento == documento, tabela.c.expira_em > agora(),
            )).scalar_one_or_none()
    except SQLAlchemyError as exc:
        logger.warning('Leitura do cache cadastral indisponível: %s', type(exc).__name__)
        return None


def _reservar_consulta():
    """Um único INSERT/UPSERT decide o vencedor, inclusive na primeira chamada."""
    tabela = ConsultaEmpresaLimite.__table__
    try:
        with db.engine.begin() as conexao:
            instante = agora()
            stmt = _inserir(tabela, conexao).values(
                provedor=_PROVEDOR, proxima_em=instante + _INTERVALO,
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=[tabela.c.provedor],
                set_={'proxima_em': stmt.excluded.proxima_em},
                where=tabela.c.proxima_em <= instante,
            )
            if conexao.execute(stmt).rowcount != 1:
                return False
            # O UPSERT pode ter esperado por uma trava. Renova a partir da
            # aquisição efetiva, ainda com a linha bloqueada nesta transação.
            conexao.execute(tabela.update().where(
                tabela.c.provedor == _PROVEDOR,
            ).values(proxima_em=agora() + _INTERVALO))
            return True
    except SQLAlchemyError as exc:
        # Inclusive SQLite bloqueado por uma escrita do caller: não o comita
        # nem faz a chamada sem contabilizar a tentativa no limite global.
        logger.warning('Reserva da consulta cadastral indisponível: %s', type(exc).__name__)
        return False


def _pausar_apos_429():
    tabela = ConsultaEmpresaLimite.__table__
    proxima = agora() + timedelta(minutes=1)
    try:
        with db.engine.begin() as conexao:
            conexao.execute(tabela.update().where(
                tabela.c.provedor == _PROVEDOR, tabela.c.proxima_em < proxima,
            ).values(proxima_em=proxima))
    except SQLAlchemyError as exc:
        logger.warning('Pausa da consulta cadastral indisponível: %s', type(exc).__name__)


def _salvar_cache(documento, resultado):
    tabela = ConsultaEmpresaCache.__table__
    instante = agora()
    tem_ie = resultado['dados'].get('situacao_ie') == 'contribuinte'
    valores = dict(documento=documento, resultado=resultado, tem_ie=tem_ie,
                   consultado_em=instante,
                   expira_em=instante + (_TTL_IE if tem_ie else _TTL_PENDENTE))
    try:
        with db.engine.begin() as conexao:
            stmt = _inserir(tabela, conexao).values(**valores)
            stmt = stmt.on_conflict_do_update(
                index_elements=[tabela.c.documento],
                set_={chave: getattr(stmt.excluded, chave)
                      for chave in valores if chave != 'documento'},
                # Uma resposta de fallback lenta não substitui IE válida
                # que outra consulta tenha acabado de colocar no cache.
                where=or_(tabela.c.expira_em <= instante,
                          tabela.c.tem_ie.is_(False), tem_ie),
            )
            conexao.execute(stmt)
            return conexao.execute(select(tabela.c.resultado).where(
                tabela.c.documento == documento,
            )).scalar_one()
    except SQLAlchemyError as exc:
        logger.warning('Gravação do cache cadastral indisponível: %s', type(exc).__name__)
        return resultado


def _data_fonte(*valores):
    return next((_texto(valor) for valor in valores if _texto(valor)), None)


def _normalizar_cnpj_ws(payload, documento):
    if not isinstance(payload, dict):
        return None
    estabelecimento = payload.get('estabelecimento')
    if not isinstance(estabelecimento, dict) or not _documento_exato(estabelecimento.get('cnpj'), documento):
        return None
    estado = estabelecimento.get('estado')
    cidade = estabelecimento.get('cidade')
    if not isinstance(estado, dict) or not isinstance(cidade, dict):
        return None
    uf = _texto(estado.get('sigla')).upper()
    nome = _texto(payload.get('razao_social'))
    endereco = ' '.join(filter(None, (_texto(estabelecimento.get('tipo_logradouro')),
                                     _texto(estabelecimento.get('logradouro')))))
    if not nome or not endereco or uf not in _UFS or not _texto(cidade.get('nome')):
        return None
    cep = _digitos(estabelecimento.get('cep'))
    dados = {
        'nome': nome, 'endereco': endereco,
        'numero': _texto(estabelecimento.get('numero')),
        'complemento': _texto(estabelecimento.get('complemento')),
        'bairro': _texto(estabelecimento.get('bairro')),
        'cidade': _texto(cidade.get('nome')), 'uf': uf,
        'cep': cep.zfill(8) if cep and len(cep) <= 8 else '',
        'ie': '', 'situacao_ie': None,
    }
    inscricoes = estabelecimento.get('inscricoes_estaduais')
    elegiveis = {}
    if isinstance(inscricoes, list):
        for inscricao in inscricoes:
            if not isinstance(inscricao, dict) or inscricao.get('ativo') is not True:
                continue
            estado_ie = inscricao.get('estado')
            if not isinstance(estado_ie, dict) or _texto(estado_ie.get('sigla')).upper() != uf:
                continue
            raw_ie = _texto(inscricao.get('inscricao_estadual'))
            # Somente números e máscara; texto como ISENTO não comprova IE.
            if not raw_ie or not re.fullmatch(r'[0-9. /-]+', raw_ie):
                continue
            ie = _digitos(raw_ie)
            if 2 <= len(ie) <= 14:
                elegiveis[ie] = inscricao
    atualizado = _data_fonte(estabelecimento.get('atualizado_em'), payload.get('atualizado_em'))
    if len(elegiveis) == 1:
        ie, inscricao = next(iter(elegiveis.items()))
        dados.update(ie=ie, situacao_ie='contribuinte')
        atualizado = _data_fonte(inscricao.get('atualizado_em'), atualizado)
    return _resultado(dados, origem=_PROVEDOR, atualizado_em=atualizado,
                      aviso='' if dados['ie'] else _AVISO_IE)


def _consultar_cnpj_ws(documento):
    try:
        resposta = requests.get(f'https://publica.cnpj.ws/cnpj/{documento}',
                                headers={'Accept': 'application/json'},
                                timeout=_TIMEOUT, allow_redirects=False)
    except requests.RequestException as exc:
        logger.warning('Consulta CNPJ.ws indisponível: %s', type(exc).__name__)
        return None
    if resposta.status_code == 429:
        _pausar_apos_429()
        return None
    if resposta.status_code != 200:
        logger.warning('Consulta CNPJ.ws retornou HTTP %s', resposta.status_code)
        return None
    try:
        payload = resposta.json()
    except ValueError:
        logger.warning('Consulta CNPJ.ws retornou JSON inválido.')
        return None
    resultado = _normalizar_cnpj_ws(payload, documento)
    if resultado is None:
        logger.warning('Consulta CNPJ.ws retornou cadastro incompatível com a consulta.')
    return resultado


def _consultar_endereco(documento):
    from app.services import cnpj

    publico = cnpj.consultar(documento, timeout=_TIMEOUT)
    if (not isinstance(publico, dict) or publico.get('erro')
            or not _documento_exato(publico.get('cnpj'), documento) or not _texto(publico.get('razao_social'))):
        return _resultado(aviso='Consulta cadastral indisponível. Tente novamente em instantes.')
    dados = {campo: _texto(publico.get('logradouro' if campo == 'endereco' else campo))
             for campo in ('endereco', 'numero', 'complemento', 'bairro', 'cidade', 'uf', 'cep')}
    dados.update(nome=_texto(publico.get('razao_social')), ie='', situacao_ie=None)
    return _resultado(dados, origem='cnpj_publico', aviso=_AVISO_IE)


def consultar(doc):
    """Retorna apenas campos fiscais públicos; falha nunca é tratada como isenção."""
    from app.services.loja_checkout import _cnpj_valido

    documento = _digitos(doc)
    if not _cnpj_valido(documento):
        return _resultado(aviso='Informe um CNPJ válido.')
    cache = _ler_cache(documento)
    if cache is not None:
        return cache
    resultado = _consultar_cnpj_ws(documento) if _reservar_consulta() else None
    if resultado is None:
        resultado = _consultar_endereco(documento)
    return _salvar_cache(documento, resultado)
