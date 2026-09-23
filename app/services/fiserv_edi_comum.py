"""Tipos e conversões exatas do extrato Fiserv; nenhuma operação de rede."""

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

VERSAO_PARSER = 1
METRICAS = frozenset({
    'bruto', 'taxa', 'comissao', 'liquido', 'antecipacao', 'previsto',
    'liquidado', 'atualizado', 'livre', 'alocado', 'ajuste', 'movimento',
})


class ErroLayoutFiserv(ValueError):
    """Código local, sem conteúdo do arquivo na mensagem de erro."""

    def __init__(self, codigo='LAYOUT_INVALIDO'):
        codigos = {
            'LAYOUT_INVALIDO', 'JSON_INVALIDO', 'LIMITE_EXCEDIDO',
            'CAMPO_INVALIDO', 'DATA_INVALIDA', 'VALOR_INVALIDO',
            'CONTEXTO_AUSENTE', 'REGISTRO_NAO_SUPORTADO', 'IDENTIDADE_AUSENTE',
            'TOTAL_DIVERGENTE', 'SEQUENCIA_INVALIDA', 'TIPO_NAO_SUPORTADO',
        }
        self.codigo = codigo if codigo in codigos else 'LAYOUT_INVALIDO'
        super().__init__(self.codigo)


@dataclass
class RegistroFinanceiro:
    categoria: str
    registro: str
    ordinal: int
    chave_negocio: str | None = None
    fingerprint: str = ''
    documento: str | None = None
    estabelecimento: str | None = None
    data_evento: date | None = None
    data_vencimento: date | None = None
    data_atualizacao: datetime | None = None
    bandeira: str | None = None
    produto: str | None = None
    referencia: str | None = None
    status: str | None = None
    direcao: int | None = None
    papel: str = 'detalhe'
    conjunto: str | None = None
    incluir_totais: bool = True
    valores: dict = field(default_factory=dict)
    detalhes: dict = field(default_factory=dict)


@dataclass
class DocumentoFiserv:
    tipo: str
    layout: str
    data_processamento: date
    numero_processamento: str
    tipo_processamento: str
    adquirente: str
    documento: str
    hash_semantico: str
    total_registros: int
    registros: list[RegistroFinanceiro] = field(default_factory=list)
    avisos: list[str] = field(default_factory=list)


def campo(registro, *nomes):
    for nome in nomes:
        valor = registro.get(nome)
        if valor is not None and valor != '':
            return valor
    return None


def texto(valor, limite=200):
    if valor is None or valor == '':
        return None
    if isinstance(valor, bool) or not isinstance(valor, (str, int, Decimal)):
        raise ErroLayoutFiserv('CAMPO_INVALIDO')
    resultado = str(valor).strip()
    if len(resultado) > limite or any(ord(c) < 32 for c in resultado):
        raise ErroLayoutFiserv('CAMPO_INVALIDO')
    return resultado or None


def dinheiro(valor):
    if valor is None or valor == '':
        return None
    if isinstance(valor, bool) or not isinstance(valor, (str, int, Decimal)):
        raise ErroLayoutFiserv('VALOR_INVALIDO')
    bruto = str(valor).strip()
    if not re.fullmatch(r'-?\d+(?:[.,]\d+)?', bruto):
        raise ErroLayoutFiserv('VALOR_INVALIDO')
    try:
        numero = Decimal(bruto.replace(',', '.'))
        if not numero.is_finite() or abs(numero) >= Decimal('10000000000000000'):
            raise ErroLayoutFiserv('VALOR_INVALIDO')
        centavos = numero.quantize(Decimal('0.01'))
        if numero != centavos:
            raise ErroLayoutFiserv('VALOR_INVALIDO')
        return centavos
    except InvalidOperation:
        raise ErroLayoutFiserv('VALOR_INVALIDO') from None


def valor_monetario(registro, *nomes):
    """Leiaute JSON: decimal em reais e espelho *Field em centavos fixos."""
    for nome in nomes:
        base = nome[:-5] if nome.endswith('Field') else nome
        direto = campo(registro, base)
        fixo = campo(registro, base + 'Field')
        if direto is None and fixo is None:
            continue
        valor = dinheiro(direto)
        if fixo is not None:
            bruto = texto(fixo, 32)
            if not re.fullmatch(r'-?\d{15}', bruto):
                raise ErroLayoutFiserv('VALOR_INVALIDO')
            espelho = dinheiro(Decimal(bruto) / 100)
            if valor is not None and valor != espelho:
                raise ErroLayoutFiserv('TOTAL_DIVERGENTE')
            if valor is None:
                valor = espelho
        return valor
    return None


def data(valor):
    if valor is None or valor == '':
        return None
    if isinstance(valor, datetime):
        return valor.date()
    if isinstance(valor, date):
        return valor
    bruto = texto(valor, 40)
    for formato in ('%d%m%Y', '%d/%m/%Y', '%Y-%m-%d', '%Y%m%d'):
        try:
            resultado = datetime.strptime(bruto, formato).date()
            if 1900 <= resultado.year <= 2199:
                return resultado
        except ValueError:
            pass
    try:
        resultado = datetime.fromisoformat(bruto.replace('Z', '+00:00')).date()
        if not 1900 <= resultado.year <= 2199:
            raise ValueError
        return resultado
    except (ValueError, AttributeError):
        raise ErroLayoutFiserv('DATA_INVALIDA') from None


def instante(valor):
    if valor is None or valor == '':
        return None
    bruto = texto(valor, 40)
    try:
        return datetime.fromisoformat(bruto.replace('Z', '+00:00'))
    except ValueError:
        return datetime.combine(data(bruto), datetime.min.time())


def codigo(registro):
    valor = texto(registro.get('recordType'), 3)
    if valor is None or not valor.isdigit():
        raise ErroLayoutFiserv('LAYOUT_INVALIDO')
    return valor.zfill(3)


def serializavel(valor):
    if isinstance(valor, (Decimal, date, datetime)):
        return str(valor)
    raise TypeError('Tipo não serializável no extrato.')


def hash_dados(valor):
    def canonico(item):
        if isinstance(item, bool) or item is None:
            return item
        if isinstance(item, (int, Decimal)):
            numero = Decimal(item)
            return '0' if numero == 0 else format(numero.normalize(), 'f')
        if isinstance(item, dict):
            return {chave: canonico(dado) for chave, dado in item.items()}
        if isinstance(item, (list, tuple)):
            return [canonico(dado) for dado in item]
        return item

    bruto = json.dumps(canonico(valor), sort_keys=True, ensure_ascii=False,
                       separators=(',', ':'), default=serializavel)
    return hashlib.sha256(bruto.encode('utf-8')).hexdigest()


def fingerprint_registro(registro):
    return hash_dados(sem_controles(registro, excluir_filhos=True))


def sem_controles(valor, *, excluir_filhos=False):
    """Remove sequência técnica; cada registro filho possui identidade própria."""
    if isinstance(valor, dict):
        return {chave: sem_controles(item, excluir_filhos=excluir_filhos)
                for chave, item in valor.items() if chave != 'recordNumber'
                and not (excluir_filhos and isinstance(item, list)
                         and any(isinstance(filho, dict) and 'recordType' in filho for filho in item))}
    if isinstance(valor, list):
        return [sem_controles(item, excluir_filhos=excluir_filhos) for item in valor]
    return valor


def identidade(*partes):
    if any(parte is None or parte == '' for parte in partes):
        return None
    return hash_dados(partes)
