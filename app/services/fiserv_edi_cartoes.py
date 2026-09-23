"""Normalização pura de Vendas/Pagamentos do layout Fiserv EDI 7.7.

Não cria lançamentos financeiros. Resumos e detalhes são representações
alternativas; campos de cartão, portador e domicílio bancário não são copiados.
"""

import re
from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal

from .fiserv_edi_comum import (
    ErroLayoutFiserv,
    RegistroFinanceiro,
    campo,
    codigo,
    data,
    fingerprint_registro,
    identidade,
    texto,
    valor_monetario,
)

_VENDAS = {'010': '011', '012': '013', '014': '015', '016': '017', '018': '019'}
_PAGAMENTOS = {'020': '021', '022': '023', '024': '025', '033': '034', '035': '036'}
_PRODUTOS = {
    '010': 'Débito', '011': 'Débito', '012': 'Crédito à vista',
    '013': 'Crédito à vista', '014': 'Crédito parcelado',
    '015': 'Crédito parcelado', '016': 'Voucher', '017': 'Voucher',
    '018': 'Parcela acelerada', '019': 'Parcela acelerada',
    '020': 'Débito', '021': 'Débito', '022': 'Crédito à vista',
    '023': 'Crédito à vista', '024': 'Crédito parcelado',
    '025': 'Crédito parcelado', '035': 'Voucher', '036': 'Voucher',
}
_AVISOS = {
    'identidade': 'SP_IDENTIDADE_AUSENTE',
    'grupo': 'SP_GRUPO_NAO_CONFERIDO',
    'status': 'SP_STATUS_NAO_CONFIRMADO',
    'parcelas': 'SP_PARCELAS_INFORMATIVAS',
    'informativo': 'SP_AJUSTES_INFORMATIVOS',
    'antecipacao': 'SP_ANTECIPACAO_COMPOSICAO',
    'antecipacao_sobreposta': 'SP_ANTECIPACAO_SOBREPOSTA',
    'voucher': 'SP_VOUCHER_CONFERENCIA',
    'sem_uso': 'SP_REGISTRO_SEM_USO',
}


def _texto(registro, *nomes, limite=100):
    __tracebackhide__ = True
    return texto(campo(registro, *nomes), limite)


def _objeto(registro, nome):
    __tracebackhide__ = True
    valor = registro.get(nome)
    if valor is None:
        return {}
    if not isinstance(valor, dict):
        raise ErroLayoutFiserv('CAMPO_INVALIDO')
    return valor


def _numero(valor):
    __tracebackhide__ = True
    bruto = texto(valor, 12)
    if bruto is None:
        return None
    if not bruto.isascii() or not bruto.isdigit():
        raise ErroLayoutFiserv('CAMPO_INVALIDO')
    numero = int(bruto)
    if numero > 1_000_000:
        raise ErroLayoutFiserv('CAMPO_INVALIDO')
    return numero


def _enum(valor):
    __tracebackhide__ = True
    if isinstance(valor, dict):
        valor = campo(valor, 'code', 'description')
    bruto = texto(valor, 120)
    if bruto is None:
        return None
    codigo_rotulado = re.fullmatch(r'([0-9]+|[A-Za-z]+)\s*-\s*.+', bruto)
    token = codigo_rotulado.group(1) if codigo_rotulado else bruto
    if token.isascii() and token.isdigit():
        return str(int(token))
    return token.upper()


def _rotulo(registro, *nomes):
    __tracebackhide__ = True
    valor = campo(registro, *nomes)
    if isinstance(valor, dict):
        valor = campo(valor, 'description', 'code')
    return texto(valor, 100)


def _direcao(registro):
    __tracebackhide__ = True
    valor = _enum(campo(registro, 'receiptType', 'receiptTypeDescription'))
    if valor in {'C', 'CREDITO', 'CRÉDITO'}:
        return 1
    if valor in {'D', 'DEBITO', 'DÉBITO'}:
        return -1
    return None


def _assinar(item):
    """D/C qualifica o valor absoluto; a projeção não deve assinar novamente."""
    if item.direcao is not None:
        item.valores = {nome: abs(valor) * item.direcao if valor is not None else None
                        for nome, valor in item.valores.items()}


def _valores(registro, **nomes):
    __tracebackhide__ = True
    # O JSON entrega a base decimal e, em paralelo, ``Field`` em centavos.
    # A conversão/conferência de ambas fica no helper comum, sem escala local.
    return {metrica: valor_monetario(registro, *chaves) for metrica, chaves in nomes.items()}


def _referencias(registro):
    """Lista explícita de referências comerciais; nunca replica o JSON bruto."""
    __tracebackhide__ = True
    campos = {
        'resumo_vendas': ('salesSummaryNumber',),
        'nsu': ('salesReceiptNumber',),
        'comprovante_parcela': ('salesInstallmentReceiptNumber',),
        'ordem_pagamento': ('paymentInstructionNumber', 'invoice_number'),
        'resumo_financeiro': ('financeSummaryNumber',),
        'referencia_adquirente': ('acquirerReference', 'cancelTransactionArn'),
        'parcela': ('installmentNumber',),
        'total_parcelas': ('totalInstallments',),
        'unidade_recebivel': ('idUr', 'receivableId'),
        'ajuste': ('adjustmentNumber',),
        'cancelamento': ('cancelId',),
        'chargeback': ('chargeBackKey', 'chargebackReferenceCode'),
    }
    resultado = {}
    for nome, aliases in campos.items():
        valor = _texto(registro, *aliases)
        if valor is not None:
            resultado[nome] = valor
    for nome, aliases in {
        'tipo_transacao': ('transactionType', 'salesTransactionType', 'transactionTypeDescription'),
        'tipo_pagamento': ('paymentType', 'paymentTypeDescription'),
    }.items():
        valor = _enum(campo(registro, *aliases))
        if valor is not None:
            resultado[nome] = valor
    return resultado


@dataclass
class _Contexto:
    adquirente: str | None
    estabelecimento: str | None
    documento: str | None


@dataclass
class _Grupo:
    resumo: RegistroFinanceiro
    origem: dict = field(repr=False)
    detalhes: list[RegistroFinanceiro] = field(default_factory=list)


def _contexto(cabecalho, filial=None):
    __tracebackhide__ = True
    cliente = _objeto(cabecalho, 'client')
    estabelecimento = _texto(cabecalho, 'clientCode') or _texto(cliente, 'code')
    documento = _texto(cabecalho, 'clientDocument') or _texto(cliente, 'document')
    if filial is not None:
        filial_aninhada = _objeto(filial, 'branchClient')
        estabelecimento = _texto(filial, 'branchClientCode') or _texto(filial_aninhada, 'code')
        documento = _texto(filial, 'branchClientDocument') or _texto(filial_aninhada, 'document')
    return _Contexto(_texto(cabecalho, 'acquiringName'), estabelecimento, documento)


def _base(tipo, registro, contexto, ordinal):
    __tracebackhide__ = True
    cod = codigo(registro)
    estabelecimento = _texto(registro, 'clientCode', 'adjustmentClientCode', 'merchant_id')
    estabelecimento = estabelecimento or contexto.estabelecimento
    return RegistroFinanceiro(
        categoria='vendas' if tipo == 'S' else 'pagamentos',
        registro=cod,
        ordinal=ordinal,
        fingerprint=fingerprint_registro(registro),
        documento=contexto.documento,
        estabelecimento=estabelecimento,
        bandeira=_rotulo(registro, 'cardSchemeDescription', 'cardScheme', 'ds_bandeira'),
        produto=_PRODUTOS.get(cod),
        detalhes=_referencias(registro),
        incluir_totais=False,
    )


def _chave_grupo(tipo, cod, registro, contexto, estabelecimento):
    __tracebackhide__ = True
    escopo = ('fiserv', contexto.adquirente, tipo, cod, estabelecimento)
    if tipo == 'S':
        partes = [data(campo(registro, 'salesDate')), _texto(registro, 'salesSummaryNumber')]
        if cod == '014':
            partes.append(_texto(registro, 'salesReceiptNumber'))
    elif cod == '033':
        partes = [data(campo(registro, 'paymentDate')), _texto(registro, 'paymentId')]
    else:
        partes = [
            data(campo(registro, 'paymentDate', 'payment_orig_date')),
            _texto(registro, 'paymentInstructionNumber', 'invoice_number'),
        ]
    return identidade(*escopo, *partes)


def _venda(registro, item, contexto):
    __tracebackhide__ = True
    cod = item.registro
    item.data_evento = data(campo(registro, 'salesDate'))
    item.data_vencimento = data(campo(registro, 'creditDate'))
    situacao = _enum(campo(registro, 'transactionStatus', 'transactionStatusDescription'))
    item.status = {'OK': 'OK', 'AP': 'Aceleração por cancelamento',
                   'DP': 'Desagendado por chargeback'}.get(situacao, 'Situação não informada')
    item.referencia = _texto(registro, 'salesReceiptNumber', 'salesSummaryNumber')
    if cod == '015':
        item.valores = _valores(
            registro, bruto=('splitGrossAmountInstallment',), taxa=('discountAmount',),
            comissao=('commissionAmount',), liquido=('netAmountParcel',),
        )
    else:
        item.valores = _valores(
            registro, bruto=('grossAmount', 'GrossAmount'),
            taxa=('discountAmount', 'DiscountAmount'),
            comissao=('commissionAmount',), liquido=('netAmount',),
        )
    item.direcao = 1
    if cod in {'018', '019'} or situacao in {'AP', 'DP'}:
        item.categoria = 'ajustes'
        item.direcao = None
    if cod in _VENDAS:
        item.papel = 'resumo'
        item.conjunto = _chave_grupo('S', cod, registro, contexto, item.estabelecimento)
        item.chave_negocio = identidade(item.conjunto, 'resumo')
    else:
        partes = [item.data_evento, _texto(registro, 'salesSummaryNumber'),
                  _texto(registro, 'salesReceiptNumber')]
        if cod in {'015', '019'}:
            partes.append(_texto(registro, 'salesInstallmentReceiptNumber'))
            partes.append(_texto(registro, 'installmentNumber'))
        item.chave_negocio = identidade(
            'fiserv', contexto.adquirente, 'S', cod, item.estabelecimento, *partes,
        )


def _pagamento(registro, item, contexto):
    __tracebackhide__ = True
    cod = item.registro
    item.data_evento = data(campo(registro, 'paymentDate', 'payment_orig_date'))
    item.data_vencimento = data(campo(registro, 'valueDate'))
    item.referencia = _texto(registro, 'paymentInstructionNumber', 'invoice_number')
    if cod in {'020', '022', '024', '035'}:
        item.papel = 'resumo'
        item.valores = _valores(registro, liquido=('paymentAmount', 'payment_val'))
        item.conjunto = _chave_grupo('P', cod, registro, contexto, item.estabelecimento)
        item.chave_negocio = identidade(item.conjunto, 'resumo')
        return
    item.valores = _valores(
        registro, bruto=('grossAmount',),
        taxa=('discountAmount',), comissao=('commissionAmount',),
        liquido=('feeModeNetAmount', 'netAmount'),
        antecipacao=('feeModeAmount',),
    )
    item.direcao = _direcao(registro)
    status = _enum(campo(registro, 'paymentStatus'))
    item.status = {'0': 'OK', '1': 'Retido', '2': 'Suspenso'}.get(status, 'Situação não confirmada')
    if status in {'1', '2'}:
        item.categoria = 'suspensos'
    elif _enum(campo(registro, 'paymentType', 'paymentTypeDescription')) in {'2', '7', '8'}:
        item.categoria = 'antecipacoes'
    elif _enum(campo(registro, 'paymentType', 'paymentTypeDescription')) in {'5', '6'}:
        item.categoria = 'ajustes'
    if status == '0' and item.direcao is not None:
        item.valores['liquidado'] = item.valores['liquido']
    else:
        item.valores['liquidado'] = None
    partes = [item.data_evento, item.referencia, _texto(registro, 'financeSummaryNumber'),
              _texto(registro, 'salesSummaryNumber'), _texto(registro, 'salesReceiptNumber')]
    if cod == '025':
        partes.append(_texto(registro, 'installmentNumber'))
    item.chave_negocio = identidade(
        'fiserv', contexto.adquirente, 'P', cod, item.estabelecimento, *partes,
        _enum(campo(registro, 'transactionType', 'transactionTypeDescription')),
        _enum(campo(registro, 'receiptType', 'receiptTypeDescription')),
    )


def _antecipacao(registro, item, contexto, grupo):
    __tracebackhide__ = True
    item.categoria = 'antecipacoes'
    if item.registro == '033':
        item.papel = 'resumo'
        item.data_evento = data(campo(registro, 'paymentDate'))
        item.referencia = _texto(registro, 'paymentId')
        item.valores = _valores(
            registro, liquido=('paymentValue',), antecipacao=('discountValue',),
        )
        item.conjunto = _chave_grupo('P', '033', registro, contexto, item.estabelecimento)
        item.chave_negocio = identidade(item.conjunto, 'pagamento')
    else:
        item.papel = 'composicao'
        item.data_vencimento = data(campo(registro, 'originalPaymentDate'))
        item.referencia = _texto(registro, 'receivableId')
        item.valores = _valores(registro, bruto=('receivableValue',), antecipacao=('discountValue',))
        item.conjunto = grupo.resumo.conjunto if grupo else None
        item.chave_negocio = identidade(item.conjunto, 'unidade_recebivel', item.referencia)


def _isolado(tipo, registro, item, contexto):
    __tracebackhide__ = True
    cod = item.registro
    escopo = ('fiserv', contexto.adquirente, tipo, cod, item.estabelecimento)
    if cod == '110':
        item.categoria = 'cancelamentos'
        item.papel = 'informativo'
        item.data_evento = data(campo(registro, 'cancelTransactionDate'))
        item.data_vencimento = data(campo(registro, 'originalSettlementDate'))
        item.referencia = _texto(registro, 'cancelId', 'slipNumber')
        item.status = _texto(registro, 'cancelTransactionStatus')
        item.valores = _valores(registro, ajuste=('cancelAmount',), liquido=('netAmount',),
                               comissao=('commissionAmount',), taxa=('mdrAmount',))
        item.chave_negocio = identidade(*escopo, item.data_evento, item.referencia,
                                       _texto(registro, 'slipOriginal'))
    elif cod == '111':
        item.categoria = 'chargebacks'
        item.papel = 'informativo'
        item.data_evento = data(campo(registro, 'processingDate'))
        item.referencia = _texto(registro, 'chargeBackKey')
        item.valores = _valores(registro, ajuste=('chargeBackAmount',))
        item.chave_negocio = identidade(*escopo, item.referencia)
    elif cod == '027':
        item.categoria = 'suspensos'
        item.data_evento = data(campo(registro, 'recordDate'))
        item.data_vencimento = data(campo(registro, 'valueDate'))
        item.referencia = _texto(registro, 'financeSummaryNumber')
        item.status = 'Suspenso'
        item.direcao = _direcao(registro)
        item.valores = _valores(registro, liquido=('feeModeNetAmount',))
        item.chave_negocio = identidade(*escopo, item.data_evento, item.referencia,
                                       _texto(registro, 'slipReference'))
    elif cod == '031':
        item.categoria = 'ajustes'
        item.data_evento = data(campo(registro, 'recordDate'))
        item.data_vencimento = data(campo(registro, 'adjustmentPaymentDate'))
        item.referencia = _texto(registro, 'adjustmentNumber')
        item.status = 'Programado'
        item.direcao = _direcao(registro)
        item.valores = _valores(registro, ajuste=('adjustmentAmount',),
                               bruto=('grossAdjustmentAmount',), comissao=('commissionAmount',))
        item.chave_negocio = identidade(*escopo, item.data_evento, item.referencia)
    else:
        item.papel = 'controle'
        item.valores = _valores(registro, liquido=('totalNetAmount', 'totalPaymentValue'),
                               antecipacao=('totalFeeModeAmount', 'totalDiscountValue'))
        if tipo == 'S' and cod in {'201', '999'}:
            # Os trailers reais separam objetos por produto; não são movimentos.
            # Conferir os espelhos mesmo quando seus valores não serão somados.
            for nome in ('debitSales', 'creditSales', 'installmentPlanSales',
                         'installmentSales', 'installmentSalesIssuer', 'voucherPatSales'):
                componente = _objeto(registro, nome)
                _valores(componente, bruto=('grossAmount',), taxa=('discountAmount',),
                         liquido=('netAmount',))


def _associar(tipo, cod_resumo, registro, item, contexto, grupos):
    __tracebackhide__ = True
    chave = _chave_grupo(tipo, cod_resumo, registro, contexto, item.estabelecimento)
    item.conjunto = chave
    grupo = grupos.get(chave) if chave else None
    if grupo is not None:
        grupo.detalhes.append(item)
    return grupo


def _selecionar_totais(grupo, tipo, avisar):
    __tracebackhide__ = True
    resumo = grupo.resumo
    detalhes = grupo.detalhes
    if resumo.registro == '033':
        # URs descrevem a composição, não uma segunda liquidação da ordem.
        resumo.incluir_totais = resumo.chave_negocio is not None
        return
    if tipo == 'S' and resumo.categoria == 'ajustes':
        return
    if not detalhes:
        resumo.incluir_totais = tipo == 'S' and resumo.chave_negocio is not None
        avisar('grupo')
        return
    quantidade = None
    if tipo == 'S':
        nome = 'totalInstallments' if resumo.registro == '014' else 'quantityOfSales'
        quantidade = _numero(campo(grupo.origem, nome))
    completas = all(item.chave_negocio is not None for item in detalhes)
    completas = completas and len({item.chave_negocio for item in detalhes}) == len(detalhes)
    if quantidade is not None:
        completas = completas and quantidade == len(detalhes)
    comparaveis = 0
    divergente = False
    for metrica, esperado in resumo.valores.items():
        if esperado is None:
            continue
        valores = [item.valores.get(metrica) for item in detalhes]
        if any(valor is None for valor in valores):
            completas = False
            continue
        if tipo == 'P' and any(item.direcao is None for item in detalhes):
            completas = False
            continue
        comparaveis += 1
        if sum(valores, Decimal('0')) != esperado:
            divergente = True
    completas = completas and comparaveis > 0 and not divergente
    if tipo == 'P':
        # A ordem não conferida não prova que todos os seus componentes foram
        # pagos. Somar apenas fatos identificados; a ordem fica para conferência.
        resumo.incluir_totais = False
        ocorrencias = Counter(item.chave_negocio for item in detalhes)
        for item in detalhes:
            item.incluir_totais = (
                item.chave_negocio is not None and ocorrencias[item.chave_negocio] == 1
                and item.status == 'OK' and item.direcao is not None
                and item.valores.get('liquidado') is not None
            )
        if not completas:
            avisar('grupo')
        if any(item.status != 'OK' or item.direcao is None for item in detalhes):
            avisar('status')
        return
    if completas:
        for item in detalhes:
            item.incluir_totais = True
    else:
        # A alternativa é o resumo identificado, nunca resumo + detalhes.
        resumo.incluir_totais = resumo.chave_negocio is not None
        avisar('grupo')


def normalizar_cartoes(tipo: str, registros: list[dict], cabecalho: dict
                       ) -> tuple[list[RegistroFinanceiro], list[str]]:
    """Recebe registros já ordenados/validados pelo leitor de envelope.

    Não presume IDs universais, deduz valores ausentes ou cria datas de revisão.
    A saída conserva controles sem totalização e usa somente campos permitidos.
    """
    __tracebackhide__ = True
    if tipo not in {'S', 'P'}:
        raise ErroLayoutFiserv('TIPO_NAO_SUPORTADO')
    pares = _VENDAS if tipo == 'S' else _PAGAMENTOS
    inversos = {detalhe: resumo for resumo, detalhe in pares.items()}
    extras = {'110', '111'} if tipo == 'S' else {'027', '028', '029', '031', '032', '200'}
    contexto = _contexto(cabecalho)
    resultado = []
    avisos = []
    grupos = {}
    grupo_antecipacao = None

    def avisar(chave):
        mensagem = _AVISOS[chave]
        if mensagem not in avisos:
            avisos.append(mensagem)

    for posicao, registro in enumerate(registros, 1):
        cod = codigo(registro)
        if cod == '000':
            continue
        if cod == '101':
            contexto = _contexto(cabecalho, registro)
            grupo_antecipacao = None
            continue
        if cod not in pares and cod not in inversos and cod not in extras and cod not in {'201', '999'}:
            raise ErroLayoutFiserv('REGISTRO_NAO_SUPORTADO')
        ordinal = _numero(campo(registro, 'recordNumber', 'RecordNumber')) or posicao
        item = _base(tipo, registro, contexto, ordinal)
        if tipo == 'S' and (cod in pares or cod in inversos):
            _venda(registro, item, contexto)
        elif tipo == 'P' and cod in {'033', '034'}:
            _antecipacao(registro, item, contexto, grupo_antecipacao)
            avisar('antecipacao')
        elif tipo == 'P' and (cod in pares or cod in inversos):
            _pagamento(registro, item, contexto)
            if item.papel == 'detalhe' and item.valores.get('liquidado') is None:
                avisar('status')
        else:
            _isolado(tipo, registro, item, contexto)
        if tipo == 'P':
            _assinar(item)
        if cod in pares:
            grupo = _Grupo(item, registro)
            if item.conjunto is not None:
                if item.conjunto in grupos:
                    raise ErroLayoutFiserv('SEQUENCIA_INVALIDA')
                grupos[item.conjunto] = grupo
            if cod == '033':
                grupo_antecipacao = grupo
        elif cod in inversos and cod != '034':
            grupo = _associar(tipo, inversos[cod], registro, item, contexto, grupos)
            if grupo is None:
                # Sem resumo, detalhes identificados ainda são fatos individuais.
                item.incluir_totais = item.chave_negocio is not None and (
                    tipo != 'S' or item.categoria != 'ajustes')
        elif cod in {'027', '031'}:
            item.incluir_totais = item.chave_negocio is not None and item.direcao is not None
        if tipo == 'P' and cod in inversos and cod != '034' and (
                item.direcao is None or item.status != 'OK'
                or item.valores.get('liquidado') is None):
            item.incluir_totais = False
        if item.chave_negocio is None and item.papel not in {'controle'}:
            avisar('identidade')
        if cod == '015':
            avisar('parcelas')
        if cod in {'018', '019', '110', '111'}:
            avisar('informativo')
        if cod in {'035', '036'}:
            avisar('voucher')
        if cod in {'028', '029', '032'}:
            avisar('sem_uso')
        resultado.append(item)
        if cod in {'201', '999'}:
            grupo_antecipacao = None
    for grupo in grupos.values():
        _selecionar_totais(grupo, tipo, avisar)
    if tipo == 'P' and any(item.registro in {'021', '023', '025', '036'}
                           and item.categoria == 'antecipacoes' for item in resultado):
        for item in resultado:
            if item.registro == '033':
                item.incluir_totais = False
                avisar('antecipacao_sobreposta')
    return resultado, avisos
