"""Interpreta originais guardados e projeta fatos Fiserv, sem movimentar caixa.

Observações são inseridas uma vez por versão do parser. A projeção escolhe
revisões pelas datas da fonte, nunca pelo momento de recebimento do arquivo.
"""

import re
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy.exc import SQLAlchemyError

from app.extensions import db
from app.models.fiserv import ArquivoFiservRecebido, InterpretacaoFiserv, ObservacaoFiserv
from app.services.fiserv_edi_comum import (
    METRICAS,
    VERSAO_PARSER,
    ErroLayoutFiserv,
    dinheiro,
    hash_dados,
    texto,
)
from app.services.fiserv_segredos import ErroSegredoFiserv, decifrar_bytes
from app.utils import BRT, hoje

_CATEGORIAS = (
    'vendas', 'pagamentos', 'antecipacoes', 'ajustes', 'suspensos',
    'cancelamentos', 'chargebacks', 'recebiveis', 'agenda', 'deducoes',
    'contratos', 'pix_pos', 'pix_tef', 'pix_psp', 'voucher',
)
_PAPEIS = {'detalhe', 'resumo', 'snapshot', 'composicao', 'informativo',
           'controle', 'total_arquivo', 'resumo_arquivo'}
_DETALHES = {
    'resumo_vendas', 'nsu', 'comprovante_parcela', 'ordem_pagamento',
    'resumo_financeiro', 'referencia_adquirente', 'parcela', 'total_parcelas',
    'unidade_recebivel', 'ajuste', 'cancelamento', 'chargeback',
    'tipo_transacao', 'tipo_pagamento', 'colecao', 'snapshot',
    'colecao_presente', 'efeito', 'tipo_efeito', 'tipo_evento',
    'tipo_movimento', 'codigo_status',
}
_AVISOS = {
    'IDENTIDADE_AUSENTE', 'CONTEXTO_AUSENTE', 'REGISTRO_NAO_SUPORTADO',
    'TIPO_NAO_SUPORTADO', 'TOTAL_DIVERGENTE', 'VALOR_AUSENTE',
    'SINAL_MONETARIO_NAO_CONFIRMADO', 'R_DATA_ATUALIZACAO_AUSENTE',
    'R_STATUS_NAO_SUPORTADO', 'R_DEDUCAO_IDENTIDADE_NAO_CONFIRMADA',
    'R_LIQUIDACAO_INCOMPLETA', 'R_PAI_AUSENTE', 'PIX_STATUS_NAO_SUPORTADO',
    'PIX_MOVIMENTO_NAO_CLASSIFICADO', 'PIX_TEF_STATUS_NAO_CONFIRMADO',
    'VOUCHER_STATUS_NAO_CONFIRMADO', 'SP_IDENTIDADE_AUSENTE',
    'SP_GRUPO_NAO_CONFERIDO', 'SP_STATUS_NAO_CONFIRMADO',
    'SP_PARCELAS_INFORMATIVAS', 'SP_AJUSTES_INFORMATIVOS',
    'SP_ANTECIPACAO_COMPOSICAO', 'SP_ANTECIPACAO_SOBREPOSTA',
    'SP_VOUCHER_CONFERENCIA', 'SP_REGISTRO_SEM_USO', 'AVISO_DE_LAYOUT',
}
_LIMITE_LINHAS = 200
_ROTULOS_AVISOS = {
    'IDENTIDADE_AUSENTE': 'Há registros sem identificação suficiente para consolidar valores.',
    'CONTEXTO_AUSENTE': 'Há registros sem estabelecimento ou documento identificado.',
    'REGISTRO_NAO_SUPORTADO': 'Há registros cujo formato ainda exige conferência.',
    'TIPO_NAO_SUPORTADO': 'Há tipos de arquivo que ainda exigem conferência.',
    'TOTAL_DIVERGENTE': 'Há totais informados que divergem de sua composição.',
    'VALOR_AUSENTE': 'Há registros sem valores monetários informados.',
    'SINAL_MONETARIO_NAO_CONFIRMADO': 'Há valores cuja direção não pôde ser confirmada.',
    'R_DATA_ATUALIZACAO_AUSENTE': 'Há recebíveis sem data de atualização confirmada.',
    'R_STATUS_NAO_SUPORTADO': 'Há recebíveis com situação ainda não reconhecida.',
    'R_DEDUCAO_IDENTIDADE_NAO_CONFIRMADA': 'Deduções sem identificação suficiente estão disponíveis apenas para conferência.',
    'R_LIQUIDACAO_INCOMPLETA': 'Há recebimentos sem combinação completa de valor e data de liquidação.',
    'R_PAI_AUSENTE': 'Há detalhes sem o recebível correspondente no arquivo.',
    'PIX_STATUS_NAO_SUPORTADO': 'Há operações Pix com situação ainda não reconhecida.',
    'PIX_MOVIMENTO_NAO_CLASSIFICADO': 'Há movimentos Pix cuja natureza ou direção exige conferência.',
    'PIX_TEF_STATUS_NAO_CONFIRMADO': 'As operações Pix do TEF estão disponíveis para conferência, sem confirmação de liquidação.',
    'VOUCHER_STATUS_NAO_CONFIRMADO': 'Os vouchers estão disponíveis para conferência, sem confirmação de liquidação.',
    'SP_IDENTIDADE_AUSENTE': 'Há registros de cartões sem identificação suficiente para consolidar valores.',
    'SP_GRUPO_NAO_CONFERIDO': 'Há resumos de cartões cuja composição não pôde ser conferida integralmente.',
    'SP_STATUS_NAO_CONFIRMADO': 'Há pagamentos sem situação ou direção confirmada.',
    'SP_PARCELAS_INFORMATIVAS': 'As datas das parcelas seguem o que foi informado no arquivo.',
    'SP_AJUSTES_INFORMATIVOS': 'Cancelamentos, contestações e acelerações são informativos e não geram baixa financeira.',
    'SP_ANTECIPACAO_COMPOSICAO': 'A composição de recebíveis não é somada novamente ao pagamento antecipado.',
    'SP_ANTECIPACAO_SOBREPOSTA': 'Há representações sobrepostas de antecipação; os totais excluem a duplicidade.',
    'SP_VOUCHER_CONFERENCIA': 'Os registros de voucher exigem conferência com o original.',
    'SP_REGISTRO_SEM_USO': 'Há registros de controle mantidos apenas para conferência.',
    'AVISO_DE_LAYOUT': 'Há informações do arquivo que exigem conferência.',
    'REVISOES_CONFLITANTES': 'Há versões divergentes sem ordem segura de atualização; esses valores estão fora dos totais.',
}


class ErroProcessamentoFiserv(ValueError):
    def __init__(self):
        super().__init__('Não foi possível guardar o processamento financeiro. Tente novamente.')


def _hash(valor, *, opcional=False):
    __tracebackhide__ = True
    if opcional and valor is None:
        return None
    if not isinstance(valor, str) or re.fullmatch(r'[0-9a-f]{64}', valor) is None:
        raise ErroLayoutFiserv('CAMPO_INVALIDO')
    return valor


def _data(valor, *, obrigatoria=False):
    __tracebackhide__ = True
    if valor is None and not obrigatoria:
        return None
    if type(valor) is not date:
        raise ErroLayoutFiserv('DATA_INVALIDA')
    return valor


def _instante(valor):
    __tracebackhide__ = True
    if valor is None:
        return None
    if not isinstance(valor, datetime):
        raise ErroLayoutFiserv('DATA_INVALIDA')
    return valor.astimezone(BRT).replace(tzinfo=None) if valor.tzinfo else valor


def _detalhes(dados):
    """Só referências comerciais explícitas; nunca copia objetos do original."""
    __tracebackhide__ = True
    if not isinstance(dados, dict):
        raise ErroLayoutFiserv('CAMPO_INVALIDO')
    resultado = {}
    for chave in _DETALHES.intersection(dados):
        valor = dados[chave]
        if valor is None or isinstance(valor, bool):
            resultado[chave] = valor
        else:
            resultado[chave] = texto(valor)
    if 'colecoes_presentes' in dados:
        colecoes = dados['colecoes_presentes']
        if not isinstance(colecoes, list) or any(c not in {'contratos', 'pagamentos'} for c in colecoes):
            raise ErroLayoutFiserv('CAMPO_INVALIDO')
        resultado['colecoes_presentes'] = sorted(set(colecoes))
    return resultado


def _observacao(registro):
    __tracebackhide__ = True
    if registro.categoria not in _CATEGORIAS or registro.papel not in _PAPEIS:
        raise ErroLayoutFiserv('REGISTRO_NAO_SUPORTADO')
    if type(registro.ordinal) is not int or not 0 <= registro.ordinal <= 1_000_000:
        raise ErroLayoutFiserv('SEQUENCIA_INVALIDA')
    if registro.direcao is not None and (type(registro.direcao) is not int or registro.direcao not in {-1, 1}):
        raise ErroLayoutFiserv('CAMPO_INVALIDO')
    if not isinstance(registro.valores, dict) or set(registro.valores) - METRICAS:
        raise ErroLayoutFiserv('VALOR_INVALIDO')
    atributos = {
        'ordinal': registro.ordinal, 'categoria': registro.categoria,
        'registro': texto(registro.registro, 10),
        'chave_negocio': _hash(registro.chave_negocio, opcional=True),
        'fingerprint': _hash(registro.fingerprint),
        'data_evento': _data(registro.data_evento),
        'data_vencimento': _data(registro.data_vencimento),
        'data_atualizacao': _instante(registro.data_atualizacao),
        'direcao': registro.direcao, 'papel': registro.papel,
        'conjunto': _hash(registro.conjunto, opcional=True),
        'incluir_totais': registro.incluir_totais is True,
        'detalhes_json': _detalhes(registro.detalhes),
    }
    for nome in ('documento', 'estabelecimento', 'bandeira', 'produto', 'referencia', 'status'):
        atributos[nome] = texto(getattr(registro, nome))
    for metrica in METRICAS:
        atributos[metrica] = dinheiro(registro.valores.get(metrica))
    if atributos['registro'] is None:
        raise ErroLayoutFiserv('CAMPO_INVALIDO')
    return ObservacaoFiserv(**atributos)


def _guardar_documento(arquivo_id, documento):
    __tracebackhide__ = True
    if documento.tipo not in {'S', 'P', 'R', 'PIX', 'V'}:
        raise ErroLayoutFiserv('TIPO_NAO_SUPORTADO')
    if type(documento.total_registros) is not int or documento.total_registros < 0:
        raise ErroLayoutFiserv('CAMPO_INVALIDO')
    avisos = sorted({aviso if isinstance(aviso, str) and aviso in _AVISOS else 'AVISO_DE_LAYOUT'
                     for aviso in documento.avisos})
    interpretacao = InterpretacaoFiserv(
        arquivo_id=arquivo_id, versao_parser=VERSAO_PARSER, status='processado',
        tipo=documento.tipo, layout=texto(documento.layout, 40),
        data_processamento=_data(documento.data_processamento, obrigatoria=True),
        numero_processamento=texto(documento.numero_processamento, 100),
        tipo_processamento=texto(documento.tipo_processamento, 100),
        adquirente=texto(documento.adquirente), documento=texto(documento.documento),
        hash_semantico=_hash(documento.hash_semantico),
        total_registros=documento.total_registros, avisos_json=avisos,
    )
    duplicado = InterpretacaoFiserv.query.filter_by(
        versao_parser=VERSAO_PARSER, status='processado', tipo=interpretacao.tipo,
        documento=interpretacao.documento, adquirente=interpretacao.adquirente,
        hash_semantico=interpretacao.hash_semantico,
    ).order_by(InterpretacaoFiserv.id).first()
    if duplicado is not None:
        interpretacao.status = 'duplicado'
        interpretacao.duplicado_de_id = duplicado.id
        interpretacao.total_observacoes = duplicado.total_observacoes
        db.session.add(interpretacao)
    else:
        observacoes = [_observacao(registro) for registro in documento.registros]
        if len({item.ordinal for item in observacoes}) != len(observacoes):
            raise ErroLayoutFiserv('SEQUENCIA_INVALIDA')
        interpretacao.total_observacoes = len(observacoes)
        db.session.add(interpretacao)
        db.session.flush()
        for item in observacoes:
            item.interpretacao_id = interpretacao.id
        db.session.add_all(observacoes)
    db.session.commit()
    return interpretacao.status


def _pendentes():
    processado = db.session.query(InterpretacaoFiserv.id).filter(
        InterpretacaoFiserv.arquivo_id == ArquivoFiservRecebido.id,
        InterpretacaoFiserv.versao_parser == VERSAO_PARSER,
    ).exists()
    return ArquivoFiservRecebido.query.filter(~processado)


def processar_pendentes(max_arquivos=20, prazo=30):
    """Lote local e atômico por original; prazo conferido entre arquivos.

    Erros de layout ficam marcados nesta versão, sem repetir indefinidamente.
    Uma nova versão do parser pode interpretar novamente o mesmo original.
    """
    __tracebackhide__ = True
    from app.services.fiserv_edi import interpretar_fiserv
    from app.services.fiserv_integracao import ColetaEmAndamento, trava

    if type(max_arquivos) is not int or not 1 <= max_arquivos <= 20:
        raise ValueError('Limite de processamento inválido.')
    if type(prazo) not in {int, float} or not 0 < prazo <= 30:
        raise ValueError('Prazo de processamento inválido.')
    resultado = {'processados': 0, 'duplicados': 0, 'erros': 0, 'pendentes': 0, 'ocupada': False}
    limite = time.monotonic() + prazo
    try:
        with trava():
            ids = [item.id for item in _pendentes().order_by(ArquivoFiservRecebido.id).limit(max_arquivos)]
            for arquivo_id in ids:
                if time.monotonic() >= limite:
                    break
                try:
                    arquivo = db.session.get(ArquivoFiservRecebido, arquivo_id)
                    documento = interpretar_fiserv(decifrar_bytes(arquivo.conteudo_cifrado))
                    status = _guardar_documento(arquivo_id, documento)
                except SQLAlchemyError:
                    db.session.rollback()
                    # Uma falha transitória de armazenamento não torna o original
                    # um erro definitivo de layout nesta versão do parser.
                    raise ErroProcessamentoFiserv() from None
                except Exception as erro:
                    db.session.rollback()
                    if isinstance(erro, ErroLayoutFiserv):
                        codigo = ErroLayoutFiserv(erro.codigo).codigo
                    elif isinstance(erro, ErroSegredoFiserv):
                        codigo = 'ORIGINAL_INDISPONIVEL'
                    else:
                        codigo = 'ERRO_PROCESSAMENTO'
                    try:
                        db.session.add(InterpretacaoFiserv(
                            arquivo_id=arquivo_id, versao_parser=VERSAO_PARSER,
                            status='erro', codigo_erro=codigo,
                            total_registros=0, total_observacoes=0, avisos_json=[],
                        ))
                        db.session.commit()
                    except Exception:
                        db.session.rollback()
                        raise ErroProcessamentoFiserv() from None
                    resultado['erros'] += 1
                else:
                    resultado['duplicados' if status == 'duplicado' else 'processados'] += 1
            resultado['pendentes'] = _pendentes().count()
    except ColetaEmAndamento:
        resultado['ocupada'] = True
        resultado['pendentes'] = _pendentes().count()
    return resultado


@dataclass(frozen=True)
class _Linha:
    observacao: ObservacaoFiserv
    fonte: InterpretacaoFiserv
    informativa: bool = False

    @property
    def escopo(self):
        __tracebackhide__ = True
        return (self.fonte.tipo, self.fonte.adquirente,
                self.observacao.documento or self.fonte.documento,
                self.observacao.estabelecimento)


def _prioridade(linha):
    __tracebackhide__ = True
    instante = linha.observacao.data_atualizacao
    processamento = linha.fonte.data_processamento
    return (instante or datetime.combine(processamento, datetime.min.time()), processamento)


def _mais_recentes(itens, *, prioridade, fonte):
    """Número só desempata quando todos os candidatos têm sequência numérica."""
    __tracebackhide__ = True
    maior = max(prioridade(item) for item in itens)
    candidatos = [item for item in itens if prioridade(item) == maior]
    numeros = [fonte(item).numero_processamento or '' for item in candidatos]
    if all(re.fullmatch(r'[0-9]{1,100}', numero) for numero in numeros):
        ultimo = max(int(numero) for numero in numeros)
        candidatos = [item for item, numero in zip(candidatos, numeros) if int(numero) == ultimo]
    return candidatos


def _fontes_vigentes():
    """Reprints não promovem a prioridade financeira de conteúdo já observado."""
    __tracebackhide__ = True
    fontes = InterpretacaoFiserv.query.filter(
        InterpretacaoFiserv.versao_parser == VERSAO_PARSER,
        InterpretacaoFiserv.status.in_(('processado', 'duplicado')),
    ).all()
    por_original = defaultdict(list)
    for fonte in fontes:
        por_original[fonte.duplicado_de_id or fonte.id].append(fonte)
    escolhidas = {}
    for original, copias in por_original.items():
        primeira_data = min(item.data_processamento for item in copias)
        primeiras = [item for item in copias if item.data_processamento == primeira_data]
        numeros = [item.numero_processamento or '' for item in primeiras]
        if all(re.fullmatch(r'[0-9]{1,100}', numero) for numero in numeros):
            primeiro_numero = min(int(numero) for numero in numeros)
            primeiras = [item for item in primeiras if int(item.numero_processamento) == primeiro_numero]
        else:
            # Sem sequência comprovada, não conferir a esse conteúdo uma
            # prioridade numérica que uma de suas próprias cópias desconhece.
            primeiras = [item for item in primeiras
                         if not re.fullmatch(r'[0-9]{1,100}', item.numero_processamento or '')]
        # O id apenas escolhe a proveniência entre fontes equivalentes.
        escolhidas[original] = min(primeiras, key=lambda item: item.id)
    return escolhidas


def _assinatura(linha):
    """Compara os dados financeiros normalizados e as regras de totalização."""
    __tracebackhide__ = True
    obs = linha.observacao
    return hash_dados({
        'fingerprint': obs.fingerprint, 'categoria': obs.categoria,
        'papel': obs.papel, 'incluir_totais': obs.incluir_totais,
        'valores': {metrica: getattr(obs, metrica) for metrica in METRICAS},
        'detalhes': obs.detalhes_json,
    })


def _conflito(linhas, motivo):
    __tracebackhide__ = True
    return {'motivo': motivo, 'arquivos': sorted({linha.fonte.arquivo_id for linha in linhas}),
            'categoria': linhas[0].observacao.categoria,
            'referencia': linhas[0].observacao.referencia,
            'documento': linhas[0].observacao.documento or linhas[0].fonte.documento}


def _selecionar_conjuntos(linhas):
    """Resumos/detalhes S/P são alternativas de uma mesma composição."""
    __tracebackhide__ = True
    grupos = defaultdict(lambda: defaultdict(list))
    livres = []
    conflitos = []
    for linha in linhas:
        obs = linha.observacao
        if linha.fonte.tipo in {'S', 'P'} and obs.conjunto:
            grupos[(linha.escopo, obs.conjunto)][linha.fonte.id].append(linha)
        else:
            livres.append(linha)
    for fontes in grupos.values():
        versoes = list(fontes.values())
        recentes = _mais_recentes(
            versoes, prioridade=lambda grupo: max(_prioridade(linha) for linha in grupo),
            fonte=lambda grupo: grupo[0].fonte,
        )
        assinaturas = {hash_dados(sorted(_assinatura(linha) for linha in grupo)) for grupo in recentes}
        if len(assinaturas) != 1:
            conflitos.append(_conflito([linha for grupo in recentes for linha in grupo], 'CONJUNTO_DIVERGENTE'))
        else:
            livres.extend(min(recentes, key=lambda grupo: grupo[0].fonte.id))
    return livres, conflitos


def _selecionar_colecoes(linhas):
    """Ausência preserva a coleção anterior; presença vazia a substitui."""
    __tracebackhide__ = True
    filhos = defaultdict(list)
    declaracoes = defaultdict(list)
    contextos_declarados = set()
    livres = []
    conflitos = []
    for linha in linhas:
        obs = linha.observacao
        detalhes = obs.detalhes_json or {}
        if linha.fonte.tipo == 'R' and obs.categoria == 'recebiveis' and obs.chave_negocio:
            for colecao in detalhes.get('colecoes_presentes', []):
                chave = (linha.escopo, obs.chave_negocio, colecao)
                declaracoes[chave].append(linha)
                contextos_declarados.add((chave, linha.fonte.id, obs.data_atualizacao))
        if linha.fonte.tipo == 'R' and detalhes.get('snapshot') is True and obs.conjunto:
            chave = (linha.escopo, obs.conjunto, detalhes.get('colecao'))
            filhos[(chave, linha.fonte.id, obs.data_atualizacao)].append(linha)
        else:
            livres.append(linha)
    for chave, pais in declaracoes.items():
        recentes = _mais_recentes(pais, prioridade=_prioridade, fonte=lambda linha: linha.fonte)
        versoes = [(pai, filhos.get((chave, pai.fonte.id, pai.observacao.data_atualizacao), []))
                   for pai in recentes]
        assinaturas = {hash_dados(sorted(_assinatura(filho) for filho in grupo)) for _, grupo in versoes}
        if len(assinaturas) != 1:
            conflitos.append(_conflito(recentes, 'COLECAO_DIVERGENTE'))
        else:
            _, escolhidos = min(versoes, key=lambda versao: versao[0].fonte.id)
            livres.extend(escolhidos)
    # Filhos sem declaração continuam visíveis para conferência, sem totais.
    for contexto, orfaos in filhos.items():
        if contexto not in contextos_declarados:
            livres.extend(_Linha(linha.observacao, linha.fonte, informativa=True) for linha in orfaos)
    return livres, conflitos


def _selecionar_linhas(linhas):
    __tracebackhide__ = True
    grupos = defaultdict(list)
    vigentes = []
    conflitos = []
    for linha in linhas:
        chave = linha.observacao.chave_negocio
        if chave is None or linha.informativa:
            # Não usar fingerprint aqui: duas ocorrências iguais podem ser reais.
            vigentes.append(linha)
        else:
            grupos[(linha.escopo, chave)].append(linha)
    for grupo in grupos.values():
        recentes = _mais_recentes(grupo, prioridade=_prioridade, fonte=lambda linha: linha.fonte)
        if len({_assinatura(linha) for linha in recentes}) != 1:
            conflitos.append(_conflito(recentes, 'REVISAO_DIVERGENTE'))
        else:
            vigentes.append(min(recentes, key=lambda linha: (linha.fonte.id, linha.observacao.id)))
    return vigentes, conflitos


def _data_filtro(linha):
    __tracebackhide__ = True
    obs = linha.observacao
    if linha.fonte.tipo == 'P':
        # paymentDate é o pagamento informado; valueDate conserva o
        # vencimento original e não deve deslocar um pagamento para outro dia.
        return obs.data_evento or obs.data_vencimento or linha.fonte.data_processamento
    if obs.categoria in {'pagamentos', 'antecipacoes', 'suspensos', 'agenda', 'recebiveis', 'contratos'}:
        return obs.data_vencimento or obs.data_evento or linha.fonte.data_processamento
    return obs.data_evento or obs.data_vencimento or linha.fonte.data_processamento


def _valores(linha):
    __tracebackhide__ = True
    obs = linha.observacao
    valores = {nome: getattr(obs, nome) for nome in sorted(METRICAS)}
    # S/P já chega assinado. Somente PSP traz magnitude + direção separadas.
    if obs.categoria == 'pix_psp' and valores['movimento'] is not None and obs.direcao in {-1, 1}:
        valores['movimento'] *= obs.direcao
    return valores


def _dto(linha):
    __tracebackhide__ = True
    obs = linha.observacao
    return {
        'arquivo_id': linha.fonte.arquivo_id, 'observacao_id': obs.id,
        'tipo': linha.fonte.tipo, 'categoria': obs.categoria, 'registro': obs.registro,
        'documento': obs.documento or linha.fonte.documento, 'estabelecimento': obs.estabelecimento,
        'data_evento': obs.data_evento, 'data_vencimento': obs.data_vencimento,
        'data_atualizacao': obs.data_atualizacao, 'data_processamento': linha.fonte.data_processamento,
        'bandeira': obs.bandeira, 'produto': obs.produto, 'referencia': obs.referencia,
        'status': obs.status, 'papel': obs.papel,
        'incluir_totais': bool(obs.incluir_totais and obs.chave_negocio and not linha.informativa
                               and obs.papel not in {'controle', 'composicao', 'informativo',
                                                     'total_arquivo', 'resumo_arquivo'}),
        'sem_identidade': obs.chave_negocio is None and obs.papel != 'controle',
        'valores': _valores(linha), 'detalhes': obs.detalhes_json or {},
    }


def _somar_informados(valores):
    """Ausência de informação não é um zero financeiro informado."""
    conhecidos = [valor for valor in valores if valor is not None]
    return sum(conhecidos, Decimal('0.00')) if conhecidos else None


def _leitura_interpretativa(linhas, categorias, hoje_referencia):
    """Leitura pura das linhas vigentes e filtradas, antes de paginar a tabela.

    A agenda vencida é apenas uma previsão sem confirmação na última posição
    conhecida. Não representa débito bancário nem confirma inadimplência.
    """
    __tracebackhide__ = True
    vendas = categorias.get('vendas', {}).get('metricas', {})
    bruto, taxas, liquido = (vendas.get(nome) for nome in ('bruto', 'taxa', 'liquido'))
    percentual = None
    if (bruto is not None and bruto > 0 and taxas is not None and 0 <= taxas <= bruto
            and liquido is not None and bruto - taxas == liquido):
        percentual = taxas * Decimal('100') / bruto

    pagos = {'pagamentos': [], 'antecipacoes': [], 'ajustes': []}
    custos = []
    agenda = []
    vencidas = []
    por_data = defaultdict(list)
    for linha in linhas:
        obs = linha.observacao
        elegivel = (obs.incluir_totais and obs.chave_negocio and not linha.informativa
                    and obs.papel not in {'controle', 'composicao', 'informativo',
                                          'total_arquivo', 'resumo_arquivo'})
        if not elegivel:
            continue
        if linha.fonte.tipo == 'P' and obs.categoria in pagos:
            pagos[obs.categoria].append(obs.liquidado)
            if obs.categoria == 'antecipacoes' and obs.liquidado is not None:
                custos.append(obs.antecipacao)
        if obs.categoria == 'agenda' and obs.status == 'previsto':
            agenda.append(obs.previsto)
            if obs.data_vencimento is not None:
                if obs.data_vencimento < hoje_referencia:
                    vencidas.append(obs.previsto)
                elif obs.previsto is not None:
                    por_data[obs.data_vencimento].append(obs.previsto)

    pagamentos = {categoria: _somar_informados(valores) for categoria, valores in pagos.items()}
    return {
        'vendas_bruto': bruto, 'vendas_taxas': taxas, 'vendas_liquido': liquido,
        'taxa_percentual': percentual,
        'pagamentos_normais': pagamentos['pagamentos'],
        'pagamentos_antecipados': pagamentos['antecipacoes'],
        'pagamentos_ajustes': pagamentos['ajustes'],
        'total_pago': _somar_informados(pagamentos.values()),
        'custo_antecipacao': _somar_informados(custos),
        'agenda_prevista': _somar_informados(agenda),
        'agenda_vencida': _somar_informados(vencidas),
        'proximas_datas': [{'data': dia, 'valor': _somar_informados(por_data[dia])}
                           for dia in sorted(por_data)[:5]],
        'arquivo_mais_recente': max((linha.fonte.data_processamento for linha in linhas), default=None),
    }


def obter_painel(inicio=None, fim=None, tipo=None, documento=None):
    """Projeção somente leitura; nenhum original é processado durante o GET."""
    __tracebackhide__ = True
    inicio, fim = _data(inicio), _data(fim)
    if inicio and fim and inicio > fim:
        raise ValueError('O início do período deve ser anterior ao fim.')
    if tipo is not None and tipo not in {'S', 'P', 'R', 'PIX', 'V'}:
        raise ValueError('Tipo de extrato inválido.')
    documento = texto(documento)
    fontes = _fontes_vigentes()
    tipos = sorted({fonte.tipo for fonte in fontes.values()})
    documentos = {fonte.documento for fonte in fontes.values() if fonte.documento}
    avisos = {aviso for fonte in fontes.values() for aviso in fonte.avisos_json or []}
    linhas = []
    if fontes:
        # Join evita um IN com milhares de parâmetros e carrega apenas a versão atual.
        consulta = ObservacaoFiserv.query.join(
            InterpretacaoFiserv, ObservacaoFiserv.interpretacao_id == InterpretacaoFiserv.id,
        ).filter(InterpretacaoFiserv.versao_parser == VERSAO_PARSER,
                 InterpretacaoFiserv.status == 'processado')
        for obs in consulta.yield_per(1000):
            fonte = fontes.get(obs.interpretacao_id)
            if fonte is None:
                continue
            if obs.documento:
                documentos.add(obs.documento)
            if (tipo and fonte.tipo != tipo) or (documento and (obs.documento or fonte.documento) != documento):
                continue
            linhas.append(_Linha(obs, fonte))
    linhas, conflitos_grupos = _selecionar_conjuntos(linhas)
    linhas, conflitos_colecoes = _selecionar_colecoes(linhas)
    linhas, conflitos_linhas = _selecionar_linhas(linhas)
    conflitos = conflitos_grupos + conflitos_colecoes + conflitos_linhas
    datas = [_data_filtro(linha) for linha in linhas]
    # Filtrar depois de selecionar versões evita ressuscitar uma revisão antiga.
    linhas = [linha for linha in linhas
              if (not inicio or _data_filtro(linha) >= inicio) and (not fim or _data_filtro(linha) <= fim)]
    categorias = {nome: {'metricas': {metrica: None for metrica in sorted(METRICAS)},
                         'quantidade': 0, 'sem_identidade': 0} for nome in _CATEGORIAS}
    totais_arquivo = []
    for linha in linhas:
        obs = linha.observacao
        if obs.papel == 'controle':
            # Cabeçalhos e trailers não representam fatos financeiros nem
            # precisam de identidade comercial para conferir o original.
            continue
        categoria = categorias[obs.categoria]
        categoria['quantidade'] += 1
        categoria['sem_identidade'] += obs.chave_negocio is None
        if obs.papel in {'total_arquivo', 'resumo_arquivo'}:
            totais_arquivo.append(_dto(linha))
            continue
        if (linha.informativa or not obs.incluir_totais or not obs.chave_negocio
                or obs.papel in {'controle', 'composicao', 'informativo'}):
            continue
        for metrica, valor in _valores(linha).items():
            if valor is not None:
                categoria['metricas'][metrica] = (categoria['metricas'][metrica] or Decimal('0.00')) + valor
    if conflitos:
        avisos.add('REVISOES_CONFLITANTES')
    if any(categoria['sem_identidade'] for categoria in categorias.values()):
        avisos.add('IDENTIDADE_AUSENTE')
    linhas.sort(key=lambda linha: (_data_filtro(linha), linha.fonte.arquivo_id, linha.observacao.ordinal), reverse=True)
    contagens = dict(db.session.query(InterpretacaoFiserv.status, db.func.count(InterpretacaoFiserv.id)).filter(
        InterpretacaoFiserv.versao_parser == VERSAO_PARSER,
    ).group_by(InterpretacaoFiserv.status).all())
    arquivos_erros = [{
        'arquivo_id': item.arquivo_id, 'codigo': item.codigo_erro, 'status': item.status,
    } for item in InterpretacaoFiserv.query.filter_by(
        versao_parser=VERSAO_PARSER, status='erro',
    ).order_by(InterpretacaoFiserv.arquivo_id.desc()).limit(20)]
    return {
        'leitura': _leitura_interpretativa(linhas, categorias, hoje()),
        'categorias': categorias, 'linhas': [_dto(linha) for linha in linhas[:_LIMITE_LINHAS]],
        'total_linhas': len(linhas), 'limite_linhas': _LIMITE_LINHAS,
        'total_conflitos': len(conflitos), 'conflitos': conflitos[:_LIMITE_LINHAS],
        'avisos': [_ROTULOS_AVISOS.get(aviso, _ROTULOS_AVISOS['AVISO_DE_LAYOUT']) for aviso in sorted(avisos)],
        'avisos_codigos': sorted(avisos), 'tipos': tipos, 'documentos': sorted(documentos),
        'data_inicio': min(datas) if datas else None, 'data_fim': max(datas) if datas else None,
        'arquivos_erros': arquivos_erros,
        'totais_arquivo': totais_arquivo[:_LIMITE_LINHAS],
        'arquivos': {'processados': contagens.get('processado', 0),
                     'duplicados': contagens.get('duplicado', 0),
                     'erros': contagens.get('erro', 0), 'pendentes': _pendentes().count()},
    }
