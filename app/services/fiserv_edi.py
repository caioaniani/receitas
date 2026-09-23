"""Interpreta extratos JSON Fiserv sem lançar valores em caixa ou pedidos."""

import json
import unicodedata
from decimal import Decimal

from app.services.fiserv_edi_comum import (
    DocumentoFiserv,
    ErroLayoutFiserv,
    campo,
    codigo,
    data,
    hash_dados,
    sem_controles,
    texto,
)

REGISTROS = {
    'S': {'000', '101', '010', '011', '012', '013', '014', '015', '016', '017',
          '018', '019', '110', '111', '201', '999'},
    'P': {'000', '101', '020', '021', '022', '023', '024', '025', '027', '031',
          '033', '034', '035', '036', '200', '201', '999'},
    'R': {'000', '101', '001', '002', '003', '004', '200', '999'},
    'PIX': {'000', '101', '001', '002', '003', '999'},
    'V': {'000', '101', '001', '201', '999'},
}


def _pares_unicos(pares):
    __tracebackhide__ = True
    resultado = {}
    for chave, valor in pares:
        if chave in resultado:
            raise ErroLayoutFiserv('JSON_INVALIDO')
        resultado[chave] = valor
    return resultado


def _recusar_constante(_valor):
    raise ErroLayoutFiserv('JSON_INVALIDO')


def _numero_json(valor):
    if len(valor) > 100:
        raise ErroLayoutFiserv('LIMITE_EXCEDIDO')
    numero = Decimal(valor)
    if not numero.is_finite() or abs(numero.as_tuple().exponent) > 100:
        raise ErroLayoutFiserv('LIMITE_EXCEDIDO')
    return numero


def _registros_json(conteudo):
    __tracebackhide__ = True
    if not isinstance(conteudo, bytes) or len(conteudo) > 16 * 1024 * 1024:
        raise ErroLayoutFiserv('LIMITE_EXCEDIDO')
    try:
        raiz = json.loads(conteudo.decode('utf-8-sig'), parse_float=_numero_json,
                          parse_int=_numero_json,
                          object_pairs_hook=_pares_unicos, parse_constant=_recusar_constante)
    except (UnicodeError, ValueError, RecursionError):
        raise ErroLayoutFiserv('JSON_INVALIDO') from None
    registros = []
    visitados = 0

    def visitar(no, profundidade=0):
        __tracebackhide__ = True
        nonlocal visitados
        visitados += 1
        if profundidade > 80 or visitados > 1000000:
            raise ErroLayoutFiserv('LIMITE_EXCEDIDO')
        if isinstance(no, dict):
            if 'recordType' in no:
                registros.append(no)
                if len(registros) > 150000:
                    raise ErroLayoutFiserv('LIMITE_EXCEDIDO')
            for valor in no.values():
                if isinstance(valor, (dict, list)):
                    visitar(valor, profundidade + 1)
        elif isinstance(no, list):
            for valor in no:
                visitar(valor, profundidade + 1)

    visitar(raiz)
    if len(registros) < 2:
        raise ErroLayoutFiserv('LAYOUT_INVALIDO')
    # A sequência declarada é do arquivo, não a ordem das propriedades do
    # envelope JSON. Só ela define o contexto de filial e resumo precedente.
    controles_vazios = all(codigo(item) in {'000', '101', '200', '201', '999'} for item in registros)
    por_sequencia = {}
    for registro in registros:
        sequencia = texto(registro.get('recordNumber'), 12)
        if not sequencia or not sequencia.isdigit():
            raise ErroLayoutFiserv('SEQUENCIA_INVALIDA')
        numero = int(sequencia)
        if numero in por_sequencia:
            # Arquivos reais sem movimento repetem o número do cabeçalho101
            # no trailer200. Não se aplica a fatos financeiros nem a000/999.
            anteriores = por_sequencia[numero]
            if (not controles_vazios or codigo(registro) in {'000', '999'}
                    or any(codigo(item) in {'000', '999', codigo(registro)} for item in anteriores)):
                raise ErroLayoutFiserv('SEQUENCIA_INVALIDA')
            anteriores.append(registro)
        else:
            por_sequencia[numero] = [registro]
    if sorted(por_sequencia) != list(range(1, len(por_sequencia) + 1)):
        raise ErroLayoutFiserv('SEQUENCIA_INVALIDA')
    return [item for numero in sorted(por_sequencia)
            for item in sorted(por_sequencia[numero], key=codigo)]


def _familia(cabecalho):
    descricao = texto(cabecalho.get('fileTypeDescription')) or ''
    normal = ''.join(c for c in unicodedata.normalize('NFKD', descricao.casefold())
                     if not unicodedata.combining(c))
    normal = ' '.join(normal.split())
    tipos = {
        'movimento de vendas': 'S', 'movimento financeiro': 'P',
        'movimento de pagamentos': 'P', 'movimento de recebiveis': 'R',
        'movimento de pix': 'PIX', 'movimento de voucher van': 'V',
        'movimento de voucher': 'V',
    }
    if normal not in tipos:
        raise ErroLayoutFiserv('TIPO_NAO_SUPORTADO')
    return tipos[normal]


def interpretar_fiserv(conteudo):
    __tracebackhide__ = True
    registros = _registros_json(conteudo)
    codigos = [codigo(item) for item in registros]
    if codigos[0] != '000' or codigos[-1] != '999' or codigos.count('000') != 1 or codigos.count('999') != 1:
        raise ErroLayoutFiserv('LAYOUT_INVALIDO')
    cabecalho = registros[0]
    tipo = _familia(cabecalho)
    if any(item not in REGISTROS[tipo] for item in codigos):
        raise ErroLayoutFiserv('REGISTRO_NAO_SUPORTADO')
    layout = texto(cabecalho.get('fileLayoutVersion'), 16) or ''
    if layout not in {'7.5', '7.6', '7.7', '7.5.0', '7.6.0', '7.7.0'}:
        raise ErroLayoutFiserv('TIPO_NAO_SUPORTADO')
    processamento = data(cabecalho.get('processingDate'))
    if processamento is None:
        raise ErroLayoutFiserv('DATA_INVALIDA')
    cliente = cabecalho.get('client')
    documento = texto(campo(cabecalho, 'clientDocument'))
    if not documento and isinstance(cliente, dict):
        documento = texto(cliente.get('document'))
    if not documento:
        raise ErroLayoutFiserv('CONTEXTO_AUSENTE')
    # Controle de transporte/geração não muda o conteúdo financeiro. Mantém
    # a multiplicidade dos registros, os contextos e todos os valores/datas.
    semanticos = []
    for item in registros:
        ignorar = {'recordNumber'}
        if codigo(item) == '000':
            ignorar |= {'processingDate', 'fileNumber', 'processingType'}
        semanticos.append(sem_controles({k: v for k, v in item.items() if k not in ignorar}))
    documento_financeiro = DocumentoFiserv(
        tipo=tipo, layout=layout, data_processamento=processamento,
        numero_processamento=texto(cabecalho.get('fileNumber'), 100) or '',
        tipo_processamento=texto(cabecalho.get('processingType'), 100) or '',
        adquirente=texto(cabecalho.get('acquiringName')) or '', documento=documento,
        hash_semantico=hash_dados(semanticos), total_registros=len(registros),
    )
    if tipo in {'S', 'P'}:
        from app.services.fiserv_edi_cartoes import normalizar_cartoes
        linhas, avisos = normalizar_cartoes(tipo, registros, cabecalho)
    else:
        from app.services.fiserv_edi_outros import normalizar_outros
        linhas, avisos = normalizar_outros(tipo, registros, cabecalho)
    documento_financeiro.registros = linhas
    documento_financeiro.avisos = sorted(set(avisos))
    return documento_financeiro
