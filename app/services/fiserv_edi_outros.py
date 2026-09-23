"""Normalização informativa de recebíveis, Pix e voucher; sem baixa financeira."""

from decimal import Decimal

from app.services.fiserv_edi_comum import (
    ErroLayoutFiserv,
    RegistroFinanceiro,
    campo,
    codigo,
    data,
    dinheiro,
    fingerprint_registro,
    identidade,
    instante,
    texto,
    valor_monetario,
)


def _nested(registro, objeto, atributo):
    valor = registro.get(objeto)
    return valor.get(atributo) if isinstance(valor, dict) else None


def _contexto(registro, *, cabecalho=False):
    if cabecalho:
        return {
            'documento': texto(campo(registro, 'clientDocument')
                               or _nested(registro, 'client', 'document')),
            'estabelecimento': texto(campo(registro, 'clientCode')
                                     or _nested(registro, 'client', 'code')),
        }
    return {
        'documento': texto(campo(registro, 'branchClientDocument')
                           or _nested(registro, 'branchClient', 'document')),
        'estabelecimento': texto(campo(registro, 'branchClientCode')
                                 or _nested(registro, 'branchClient', 'code')),
    }


def _ordinal(registro):
    return int(texto(campo(registro, 'recordNumber'), 20) or '0')


def _valores(registro, campos):
    return {metrica: valor_monetario(registro, *nomes) for metrica, nomes in campos.items()}


def _colecoes_presentes(registro):
    presentes = []
    for nome, colecao in (('contractsNegotiatedRU', 'contratos'), ('paymentRU', 'pagamentos')):
        if nome in registro:
            if not isinstance(registro[nome], list):
                raise ErroLayoutFiserv('CAMPO_INVALIDO')
            presentes.append(colecao)
    return presentes


def _conferir_totais(registro, linhas, quantidade, metricas):
    """Confere controles declarados sem somá-los aos detalhes do arquivo."""
    declarado = texto(campo(registro, quantidade), 20)
    if declarado is not None:
        if not declarado.isascii() or not declarado.isdigit():
            raise ErroLayoutFiserv('CAMPO_INVALIDO')
        if int(declarado) != len(linhas):
            raise ErroLayoutFiserv('TOTAL_DIVERGENTE')
    for nome, metrica in metricas.items():
        esperado = valor_monetario(registro, nome)
        if esperado is None:
            continue
        valores = [linha.valores.get(metrica) for linha in linhas]
        if any(valor is None for valor in valores) or sum(valores, Decimal('0.00')) != esperado:
            raise ErroLayoutFiserv('TOTAL_DIVERGENTE')


def _conferir_trailer(tipo, registro, linhas):
    if tipo == 'V':
        aprovadas = [linha for linha in linhas if linha.categoria == 'voucher'
                     and (linha.status or '').casefold() == 'autorizada']
        _conferir_totais(registro, aprovadas, 'voucherVanQuantity',
                        {'voucherVanGrossAmount': 'bruto'})
    elif tipo == 'PIX':
        aprovadas = [linha for linha in linhas if linha.categoria == 'pix_pos'
                     and linha.status == 'APPROVED']
        _conferir_totais(registro, aprovadas, 'pixPosCreditQuantity',
                        {'pixPosCreditAmount': 'bruto'})
    elif tipo == 'R':
        unidades = [linha for linha in linhas if linha.categoria == 'recebiveis']
        _conferir_totais(registro, unidades, 'receivableUnitQuantity', {
            'grossAmountTotal': 'bruto', 'freeNegotiationAmountTotal': 'livre',
            'paidAmountTotal': 'liquidado', 'allocatedAmountTotal': 'alocado',
            'receivableUnitAmountTotal': 'atualizado',
        })


def _base(registro, categoria, contexto, **atributos):
    return RegistroFinanceiro(
        categoria=categoria, registro=codigo(registro), ordinal=_ordinal(registro),
        documento=contexto.get('documento'), estabelecimento=contexto.get('estabelecimento'),
        fingerprint=fingerprint_registro(registro), **atributos,
    )


def _validar_identidade(linha, avisos):
    if not linha.chave_negocio:
        linha.incluir_totais = False
        avisos.add('IDENTIDADE_AUSENTE')
    if not linha.documento or not linha.estabelecimento:
        linha.incluir_totais = False
        avisos.add('CONTEXTO_AUSENTE')


def _ur(registro, contexto, adquirente):
    documento = texto(campo(registro, 'clientDocument')) or contexto.get('documento')
    chave = texto(campo(registro, 'receivableUnitKey'), 500)
    if chave:
        return identidade('fiserv-ur-chave', adquirente, documento, chave)
    return identidade('fiserv-ur-id', adquirente, documento,
                      texto(campo(registro, 'idUr')))


def _recebivel(registro, contexto, adquirente, pais, avisos):
    tipo = codigo(registro)
    contexto = dict(contexto)
    contexto['documento'] = texto(campo(registro, 'clientDocument')) or contexto['documento']
    chave_ur = _ur(registro, contexto, adquirente)
    pai = pais.get(chave_ur) if chave_ur else None
    atualizado = instante(campo(registro, 'updateDate')) if tipo == '001' else None
    if pai:
        atualizado = atualizado or pai.data_atualizacao
    if tipo == '001':
        status = texto(campo(registro, 'receivableUnitStatus'))
        status_codigo = (status or '').split(' - ', 1)[0]
        linha = _base(
            registro, 'recebiveis', contexto, chave_negocio=chave_ur,
            papel='snapshot', data_atualizacao=atualizado,
            data_vencimento=data(campo(registro, 'paymentDate')),
            bandeira=texto(campo(registro, 'cardSchemeDescription')),
            referencia=texto(campo(registro, 'idUr')), status=status,
            incluir_totais=status_codigo in {'A', 'B', 'C'} and atualizado is not None,
            valores=_valores(registro, {
                'bruto': ('grossAmount',), 'taxa': ('mdrAmount',),
                'atualizado': ('updatedTotalAmount',), 'ajuste': ('discountAmount',),
                'antecipacao': ('advanceFeeAmount',), 'liquidado': ('paidAmount',),
                'livre': ('freeNegotiationAmount',), 'alocado': ('allocatedAmount',),
            }), detalhes={'colecoes_presentes': _colecoes_presentes(registro)},
        )
        if atualizado is None:
            avisos.add('R_DATA_ATUALIZACAO_AUSENTE')
        if status_codigo not in {'A', 'B', 'C'}:
            avisos.add('R_STATUS_NAO_SUPORTADO')
        if chave_ur:
            pais[chave_ur] = linha
    elif tipo == '002':
        contrato = texto(campo(registro, 'contractIdentifier'))
        efeito = texto(campo(registro, 'contractEffectIndicator'))
        linha = _base(
            registro, 'contratos', contexto,
            chave_negocio=identidade('fiserv-ur-contrato', chave_ur, contrato, efeito),
            conjunto=chave_ur, papel='snapshot', data_atualizacao=atualizado,
            referencia=contrato, incluir_totais=False,
            valores=_valores(registro, {'alocado': ('effectContractAmount',)}),
            detalhes={'colecao': 'contratos', 'snapshot': True,
                      'efeito': efeito, 'tipo_efeito': texto(campo(registro, 'contractEffectTypeDescription'))},
        )
    elif tipo == '003':
        # Deduções não são um conjunto integral reenviado, nem uma nova venda.
        # O manual não fornece identidade global garantida para todo ajuste.
        linha = _base(
            registro, 'deducoes', contexto, conjunto=chave_ur,
            data_atualizacao=atualizado, data_evento=data(campo(registro, 'eventDate')),
            referencia=texto(campo(registro, 'salesReferenceNumber', 'acquirerReference')),
            direcao={'001': 1, '002': -1}.get((texto(campo(registro, 'socOperationType')) or '').zfill(3)),
            incluir_totais=False,
            valores=_valores(registro, {'ajuste': ('netAmountAdjustedDeduction',),
                                       'comissao': ('commissionAmount',)}),
            detalhes={'colecao': 'deducoes', 'snapshot': False,
                      'tipo_evento': texto(campo(registro, 'transactionTypeDescription'))},
        )
        avisos.add('R_DEDUCAO_IDENTIDADE_NAO_CONFIRMADA')
    else:
        prioridade = texto(campo(registro, 'contractPriorityId'))
        contrato = texto(campo(registro, 'contractIdentifier')) or 'ESTABELECIMENTO'
        # Dados de domicílio só entram no hash, nunca nos detalhes normalizados.
        destino = identidade(texto(campo(registro, 'domicileDocument')) or contexto['documento'],
                             texto(campo(registro, 'ispb', 'compeCode')),
                             texto(campo(registro, 'agency')),
                             texto(campo(registro, 'paymentAccount')))
        valores = _valores(registro, {'previsto': ('payAmount',),
                                      'liquidado': ('effectiveSettlementAmount',)})
        liquidacao = data(campo(registro, 'effectiveSettlementDate'))
        consistente = (valores['liquidado'] in (None, 0) if liquidacao is None
                       else valores['liquidado'] is not None)
        status = ('liquidacao_informada' if liquidacao and (valores['liquidado'] or 0) > 0 else
                  'previsto' if (valores['previsto'] or 0) > 0 else 'sem_valor_a_pagar')
        linha = _base(
            registro, 'agenda', contexto,
            chave_negocio=identidade('fiserv-ur-pagamento', chave_ur, prioridade, contrato, destino),
            conjunto=chave_ur, papel='snapshot', data_atualizacao=atualizado,
            data_vencimento=pai.data_vencimento if pai else None,
            data_evento=liquidacao, referencia=prioridade,
            status=status,
            incluir_totais=consistente and atualizado is not None, valores=valores,
            detalhes={'colecao': 'pagamentos', 'snapshot': True},
        )
        if not consistente:
            avisos.add('R_LIQUIDACAO_INCOMPLETA')
    if tipo in {'002', '004'}:
        if pai:
            colecao = linha.detalhes['colecao']
            if colecao not in pai.detalhes['colecoes_presentes']:
                pai.detalhes['colecoes_presentes'].append(colecao)
        else:
            linha.incluir_totais = False
            avisos.add('R_PAI_AUSENTE')
    _validar_identidade(linha, avisos)
    return linha


def _pix(registro, contexto, adquirente, avisos):
    tipo = codigo(registro)
    contexto = dict(contexto)
    contexto['documento'] = texto(campo(registro, 'document', 'Document', 'accountCnpj', 'DOCUMENT_NUM')) or contexto['documento']
    contexto['estabelecimento'] = texto(campo(registro, 'merchantId', 'MERCHANT_COD')) or contexto['estabelecimento']
    if tipo == '001':
        status = texto(campo(registro, 'qrCodeStatus'))
        evento = data(campo(registro, 'dateQRCodeGenerated'))
        nsu = texto(campo(registro, 'nsuTransaction'))
        autorizacao = texto(campo(registro, 'authorizationCode'))
        terminal = texto(campo(registro, 'terminalRegister'))
        chave = identidade('fiserv-pix-pos', adquirente, contexto['documento'],
                           contexto['estabelecimento'], terminal, evento, nsu, autorizacao)
        linha = _base(
            registro, 'pix_pos', contexto, chave_negocio=chave,
            data_evento=evento, referencia=nsu, status=status,
            incluir_totais=status == 'APPROVED', valores=_valores(registro, {'bruto': ('amountTransaction',)}),
        )
        if status not in {'APPROVED', 'CANCELED', 'UNDONE', 'UNCONFIRMED', 'UNAUTHORIZED', 'TIMEOUT'}:
            avisos.add('PIX_STATUS_NAO_SUPORTADO')
    elif tipo == '002':
        criado = instante(campo(registro, 'createdAt'))
        movimento = texto(campo(registro, 'movementTpe'))
        natureza = texto(campo(registro, 'TransactionType'))
        referencia = texto(campo(registro, 'Txid'))
        fim_a_fim = texto(campo(registro, 'endToEndId'))
        valor = dinheiro(campo(registro, 'Amount'))
        direcao = {'0': -1, '1': 1}.get(movimento)
        chave = identidade('fiserv-pix-psp', adquirente, contexto['documento'],
                           criado, movimento, natureza, referencia, fim_a_fim)
        inconsistencia = campo(registro, 'inconsistencyReproval')
        linha = _base(
            registro, 'pix_psp', contexto, chave_negocio=chave,
            data_evento=criado.date() if criado else None, data_atualizacao=criado,
            referencia=referencia, status='movimento_informado', direcao=direcao,
            incluir_totais=(direcao is not None and natureza in {str(n) for n in range(15)}
                            and valor is not None and valor >= 0 and inconsistencia is None),
            valores={'movimento': valor}, detalhes={'tipo_movimento': natureza},
        )
        if not linha.incluir_totais:
            avisos.add('PIX_MOVIMENTO_NAO_CLASSIFICADO')
    else:
        criado = instante(campo(registro, 'TRANSACTION_DTT'))
        nsu = texto(campo(registro, 'NSU_NUM'))
        status = texto(campo(registro, 'TRANSACTION_STATUS_NM'))
        chave = identidade('fiserv-pix-tef', adquirente, contexto['documento'],
                           contexto['estabelecimento'], criado,
                           texto(campo(registro, 'TERMINAL_COD')), nsu)
        linha = _base(
            registro, 'pix_tef', contexto, chave_negocio=chave,
            data_evento=criado.date() if criado else None, referencia=nsu, status=status,
            incluir_totais=False, valores=_valores(registro, {'bruto': ('TRANSACTION_AMT',)}),
            detalhes={'codigo_status': texto(campo(registro, 'TRANSACTION_STATUS_COD'))},
        )
        # Domínios de aprovação/cancelamento TEF não estão definidos no manual.
        avisos.add('PIX_TEF_STATUS_NAO_CONFIRMADO')
    _validar_identidade(linha, avisos)
    return linha


def _voucher(registro, contexto, adquirente, avisos):
    contexto = dict(contexto)
    contexto['estabelecimento'] = texto(campo(registro, 'clientCode', 'RETAILER_COD')) or contexto['estabelecimento']
    evento = data(campo(registro, 'transactionDate', 'TRANSACTION_DAT'))
    comprovante = texto(campo(registro, 'transactionInvoiceNumber', 'TRANSACIONT_INVOICE_NUM'))
    autorizacao = texto(campo(registro, 'authorizationCode', 'TRANSACIONT_APPROVAL_COD'))
    status = texto(campo(registro, 'transactionStatus', 'TRANSACTION_STATUS_NM'))
    status_normal = (status or '').casefold()
    chave = identidade('fiserv-voucher-van', adquirente, contexto['documento'],
                       contexto['estabelecimento'], evento,
                       texto(campo(registro, 'terminalNumber', 'TERMINAL_NM')), comprovante, autorizacao)
    linha = _base(
        registro, 'voucher', contexto, chave_negocio=chave, data_evento=evento,
        referencia=comprovante, status=status, incluir_totais=status_normal == 'autorizada',
        bandeira=texto(campo(registro, 'cardSchemeDescription', 'CARD_SCHEME_NM')),
        produto=texto(campo(registro, 'product', 'PRODUCT_NM')),
        valores=_valores(registro, {'bruto': ('grossAmount', 'TRANSACTION_REQUSTED_AMT')}),
    )
    if status_normal not in {'autorizada', 'recusada'}:
        avisos.add('VOUCHER_STATUS_NAO_CONFIRMADO')
    _validar_identidade(linha, avisos)
    return linha


def normalizar_outros(tipo: str, registros: list[dict], cabecalho: dict
                      ) -> tuple[list[RegistroFinanceiro], list[str]]:
    """Interpreta registros já ordenados, preservando contexto e observações."""
    __tracebackhide__ = True
    avisos = set()
    saida = []
    contexto = _contexto(cabecalho, cabecalho=True)
    adquirente = texto(campo(cabecalho, 'acquiringName'))
    pais = {}
    filial = []
    aceitos = {'R': {'001', '002', '003', '004'}, 'PIX': {'001', '002', '003'}, 'V': {'001'}}
    if tipo not in aceitos:
        return [], ['TIPO_NAO_SUPORTADO']
    for registro in registros:
        cod = codigo(registro)
        if cod == '101':
            contexto = _contexto(registro)
            pais = {}
            filial = []
            continue
        if cod == '999':
            _conferir_trailer(tipo, registro, saida)
            continue
        if tipo == 'V' and cod == '201':
            _conferir_trailer(tipo, registro, filial)
            continue
        if cod == '000' or (tipo == 'R' and cod == '200'):
            continue
        if cod not in aceitos[tipo]:
            avisos.add('REGISTRO_NAO_SUPORTADO')
            continue
        if tipo == 'R':
            linha = _recebivel(registro, contexto, adquirente, pais, avisos)
        elif tipo == 'PIX':
            linha = _pix(registro, contexto, adquirente, avisos)
        else:
            linha = _voucher(registro, contexto, adquirente, avisos)
        if any(valor is not None and valor < 0 for valor in linha.valores.values()):
            linha.incluir_totais = False
            avisos.add('SINAL_MONETARIO_NAO_CONFIRMADO')
        if not any(valor is not None for valor in linha.valores.values()):
            linha.incluir_totais = False
            avisos.add('VALOR_AUSENTE')
        saida.append(linha)
        filial.append(linha)
    return saida, sorted(avisos)
