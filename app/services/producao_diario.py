"""Diário factual: nenhuma operação deste módulo altera produção ou estoque."""

import math
from datetime import date, datetime, timedelta
from functools import wraps
from uuid import UUID

from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models import (
    MassaBase,
    MassaBaseItem,
    PlanejamentoItem,
    PlanejamentoProducao,
    ProducaoDiarioAlteracao,
    ProducaoDiarioEtapa,
    ProducaoDiarioLote,
)
from app.utils import agora, hoje, parse_float_br

MEDIDAS = (
    'farinha_kg', 'agua_kg', 'massa_kg', 'temperatura_agua',
    'temperatura_ambiente', 'temperatura_farinha', 'temperatura_massa',
)


class DiarioError(ValueError):
    """Entrada inválida ou operação incompatível com o estado atual."""


class DiarioConflito(DiarioError):
    """Outro registro alterou o lote; recarregar antes de continuar."""


def _transacao(func):
    @wraps(func)
    def executar(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except Exception:
            db.session.rollback()
            raise
    return executar


def _texto(valor, limite, nome, obrigatorio=False):
    if valor is None:
        valor = ''
    if not isinstance(valor, str):
        raise DiarioError(f'{nome}: informe um texto válido.')
    valor = valor.strip()
    if len(valor) > limite:
        raise DiarioError(f'{nome}: use no máximo {limite} caracteres.')
    if obrigatorio and not valor:
        raise DiarioError(f'Informe {nome.lower()}.')
    return valor or None


def _dia(valor, nome):
    try:
        if isinstance(valor, datetime):
            return valor.date()
        if isinstance(valor, date):
            return valor
        return date.fromisoformat(valor)
    except (TypeError, ValueError):
        raise DiarioError(f'{nome}: informe uma data válida.')


def _dia_producao(valor):
    dia = _dia(valor, 'Data de produção')
    if dia > hoje():
        raise DiarioError('A data de produção não pode estar no futuro.')
    return dia


def _horario(valor, nome, obrigatorio=False):
    if valor is None or valor == '':
        if obrigatorio:
            raise DiarioError(f'Informe {nome.lower()}.')
        return None
    try:
        horario = valor if isinstance(valor, datetime) else datetime.fromisoformat(valor)
    except (ValueError, TypeError):
        raise DiarioError(f'{nome}: informe data e hora válidas.')
    # O sistema registra BRT sem tzinfo. Rejeitar offsets evita conversões
    # silenciosas de horários digitados no relógio da produção.
    if horario.tzinfo is not None:
        raise DiarioError(f'{nome}: use o horário local, sem fuso horário.')
    if horario > agora() + timedelta(minutes=5):
        raise DiarioError(f'{nome}: o horário não pode estar no futuro.')
    return horario.replace(microsecond=0)


def _horarios(inicio, fim):
    inicio = _horario(inicio, 'Início da etapa', obrigatorio=True)
    fim = _horario(fim, 'Término da etapa')
    if fim is not None and fim < inicio:
        raise DiarioError('O término não pode ser anterior ao início da etapa.')
    return inicio, fim


def _id(valor, nome):
    try:
        if isinstance(valor, bool) or str(int(valor)) != str(valor):
            raise ValueError
        resultado = int(valor)
        if resultado <= 0:
            raise ValueError
        return resultado
    except (ValueError, TypeError, OverflowError):
        raise DiarioError(f'{nome} inválido.')


def _itens_dia(dia):
    return (PlanejamentoItem.query.join(PlanejamentoProducao)
            .filter(PlanejamentoProducao.data == dia,
                    PlanejamentoProducao.origem == 'cronograma',
                    PlanejamentoProducao.enviado_ao_padeiro.is_not(False))
            .order_by(PlanejamentoProducao.id, PlanejamentoItem.id))


def origens_do_dia(dia):
    """Itens e bases realmente presentes nas ordens liberadas para o padeiro."""
    dia = _dia(dia, 'Data da ordem')
    itens = _itens_dia(dia).all()
    resultado = [dict(tipo='item', id=item.id, nome=item.receita.nome,
                      data_ordem=dia, planejamento_id=item.planejamento_id, receita_id=item.receita_id)
                 for item in itens]
    receitas = {item.receita_id for item in itens}
    bases = (MassaBase.query.join(MassaBaseItem)
             .filter(MassaBaseItem.receita_id.in_(receitas))
             .order_by(MassaBase.nome, MassaBase.id).all()) if receitas else []
    for base in bases:
        membros = {m.receita_id for m in base.itens}
        plano_id = next(i.planejamento_id for i in itens if i.receita_id in membros)
        resultado.append(dict(tipo='base', id=base.id, nome=base.nome,
                              data_ordem=dia, planejamento_id=plano_id, receita_id=None))
    return resultado


def obter_origem(tipo, id, dia):
    origem_id = _id(id, 'Origem')
    if tipo not in ('item', 'base'):
        raise DiarioError('Tipo de origem inválido.')
    for origem in origens_do_dia(dia):
        if origem['tipo'] == tipo and origem['id'] == origem_id:
            return origem
    raise DiarioError('Esta origem não pertence à ordem enviada para o dia escolhido.')


def _snapshot_lote(lote):
    return dict(tipo_origem=lote.tipo_origem, origem_id=lote.origem_id, receita_id=lote.receita_id,
                planejamento_id=lote.planejamento_id, nome=lote.nome,
                data_ordem=lote.data_ordem.isoformat(),
                data_producao=lote.data_producao.isoformat(),
                identificacao=lote.identificacao, medidas=dict(lote.medidas or {}),
                observacao=lote.observacao, status=lote.status, versao=lote.versao)


def _snapshot_etapa(etapa):
    return dict(id=etapa.id, nome=etapa.nome,
                inicio_em=etapa.inicio_em.isoformat(),
                fim_em=etapa.fim_em.isoformat() if etapa.fim_em else None,
                observacao=etapa.observacao, autor_id=etapa.autor_id)


def _auditar(lote, evento, usuario_id, antes, depois, etapa=None):
    db.session.add(ProducaoDiarioAlteracao(
        lote_id=lote.id, etapa_id=etapa.id if etapa else None,
        usuario_id=usuario_id, evento=evento, antes=antes, depois=depois))


def _carregar_lote(lote_id):
    lote = db.session.get(ProducaoDiarioLote, _id(lote_id, 'Lote'), populate_existing=True)
    if lote is None:
        raise DiarioError('Lote não encontrado.')
    return lote


def _aberto(lote):
    if lote.status != 'aberto':
        raise DiarioError('Reabra o lote antes de registrar ou corrigir dados.')


def _reservar(lote, versao):
    """Compare-and-swap real no banco, inclusive SQLite e múltiplos workers."""
    versao = _id(versao, 'Versão')
    atualizado = agora()
    afetados = (ProducaoDiarioLote.query.filter_by(id=lote.id, versao=versao)
                .update({'versao': versao + 1, 'atualizado_em': atualizado},
                        synchronize_session=False))
    if afetados != 1:
        raise DiarioConflito('Este lote foi alterado em outra tela. Recarregue antes de salvar.')
    db.session.refresh(lote)


def _etapa_do_lote(lote_id, etapa_id):
    etapa = db.session.get(ProducaoDiarioEtapa, _id(etapa_id, 'Etapa'), populate_existing=True)
    if etapa is None or etapa.lote_id != lote_id:
        raise DiarioError('Etapa não encontrada neste lote.')
    return etapa


@_transacao
def criar_lote(tipo, origem_id, dia, data_producao, identificacao, chave, usuario_id):
    dia = _dia(dia, 'Data da ordem')
    data_producao = _dia_producao(data_producao)
    origem_id = _id(origem_id, 'Origem')
    identificacao = _texto(identificacao, 100, 'Identificação')
    try:
        chave = str(UUID(str(chave)))
    except (ValueError, TypeError, AttributeError):
        raise DiarioError('Chave do formulário inválida. Reabra a página.')

    def existente_compativel():
        existente = ProducaoDiarioLote.query.filter_by(chave_criacao=chave).first()
        if existente is not None and (
                existente.tipo_origem != tipo or existente.origem_id != origem_id
                or existente.data_ordem != dia or existente.data_producao != data_producao
                or existente.identificacao != identificacao or existente.criado_por_id != usuario_id):
            raise DiarioConflito('Este formulário já foi usado para outro lote. Reabra a página.')
        return existente

    existente = existente_compativel()
    if existente is not None:
        return existente
    origem = obter_origem(tipo, origem_id, dia)
    lote = ProducaoDiarioLote(
        chave_criacao=chave, tipo_origem=tipo, origem_id=origem_id,
        planejamento_id=origem['planejamento_id'], receita_id=origem['receita_id'], data_ordem=dia,
        data_producao=data_producao, nome=origem['nome'],
        identificacao=identificacao, medidas={}, criado_por_id=usuario_id)
    db.session.add(lote)
    try:
        db.session.flush()
        _auditar(lote, 'criar_lote', usuario_id, None, _snapshot_lote(lote))
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        existente = existente_compativel()
        if existente is not None:
            return existente
        raise
    return lote


def _medidas(dados, atuais):
    medidas = dict(atuais or {})
    valores = dados.get('medidas', dados)
    if not hasattr(valores, 'items'):
        raise DiarioError('Medidas inválidas.')
    for nome in MEDIDAS:
        if nome not in valores:
            continue
        try:
            valor = parse_float_br(valores[nome])
        except (TypeError, ValueError, OverflowError):
            raise DiarioError(f'{nome}: informe um número válido.')
        if valor is None:
            medidas.pop(nome, None)
            continue
        if not math.isfinite(valor):
            raise DiarioError(f'{nome}: informe um número finito.')
        if nome.endswith('_kg') and (valor < 0 or (valor == 0 and nome != 'agua_kg')):
            raise DiarioError('Os pesos devem ser positivos; água pode ser zero.')
        if nome.startswith('temperatura_') and not -30 <= valor <= 150:
            raise DiarioError('As temperaturas devem estar entre -30 °C e 150 °C.')
        medidas[nome] = valor
    return medidas


@_transacao
def salvar_lote(lote_id, dados, usuario_id, versao):
    lote = _carregar_lote(lote_id)
    if not hasattr(dados, 'get'):
        raise DiarioError('Dados do lote inválidos.')
    medidas = _medidas(dados, lote.medidas)
    observacao = _texto(dados.get('observacao', lote.observacao), 4000, 'Observação')
    identificacao = _texto(dados.get('identificacao', lote.identificacao), 100, 'Identificação')
    data = _dia_producao(dados.get('data_producao', lote.data_producao))
    antes = _snapshot_lote(lote)
    _reservar(lote, versao)
    lote.medidas, lote.observacao = medidas, observacao
    lote.identificacao, lote.data_producao = identificacao, data
    _auditar(lote, 'editar_lote', usuario_id, antes, _snapshot_lote(lote))
    db.session.commit()
    return lote


@_transacao
def adicionar_etapa(lote_id, nome, inicio, fim, observacao, usuario_id, versao):
    lote = _carregar_lote(lote_id)
    _aberto(lote)
    nome = _texto(nome, 100, 'Nome da etapa', obrigatorio=True)
    observacao = _texto(observacao, 4000, 'Observação')
    inicio, fim = _horarios(inicio, fim)
    _reservar(lote, versao)
    etapa = ProducaoDiarioEtapa(lote_id=lote.id, nome=nome, inicio_em=inicio,
                               fim_em=fim, observacao=observacao, autor_id=usuario_id)
    db.session.add(etapa)
    db.session.flush()
    _auditar(lote, 'adicionar_etapa', usuario_id, None, _snapshot_etapa(etapa), etapa)
    db.session.commit()
    return etapa


@_transacao
def concluir_etapa(lote_id, etapa_id, usuario_id):
    lote = _carregar_lote(lote_id)
    etapa = _etapa_do_lote(lote.id, etapa_id)
    if etapa.fim_em is not None:
        return etapa
    _aberto(lote)
    # A primeira transação reserva o lote. A segunda pode perder a versão
    # enquanto espera: reler após rollback preserva a conclusão idempotente.
    try:
        _reservar(lote, lote.versao)
    except DiarioConflito:
        db.session.rollback()
        etapa = _etapa_do_lote(lote_id, etapa_id)
        if etapa.fim_em is not None:
            return etapa
        raise
    etapa = _etapa_do_lote(lote.id, etapa_id)
    if etapa.fim_em is not None:
        db.session.rollback()
        return etapa
    antes = _snapshot_etapa(etapa)
    _, fim = _horarios(etapa.inicio_em, agora())
    etapa.fim_em, etapa.atualizado_em = fim, agora()
    _auditar(lote, 'concluir_etapa', usuario_id, antes, _snapshot_etapa(etapa), etapa)
    db.session.commit()
    return etapa


@_transacao
def corrigir_etapa(lote_id, etapa_id, inicio, fim, observacao, usuario_id, versao, nome=None):
    lote = _carregar_lote(lote_id)
    etapa = _etapa_do_lote(lote.id, etapa_id)
    inicio, fim = _horarios(inicio, fim)
    if lote.status == 'concluido' and fim is None:
        raise DiarioError('Reabra o lote antes de deixar uma etapa em andamento.')
    observacao = _texto(observacao, 4000, 'Observação')
    nome = _texto(nome, 100, 'Nome da etapa', obrigatorio=True) if nome is not None else etapa.nome
    antes = _snapshot_etapa(etapa)
    _reservar(lote, versao)
    etapa.inicio_em, etapa.fim_em, etapa.nome = inicio, fim, nome
    etapa.observacao, etapa.atualizado_em = observacao, agora()
    _auditar(lote, 'corrigir_etapa', usuario_id, antes, _snapshot_etapa(etapa), etapa)
    db.session.commit()
    return etapa


@_transacao
def definir_status(lote_id, status, usuario_id, versao):
    if status not in ('aberto', 'concluido'):
        raise DiarioError('Status do lote inválido.')
    lote = _carregar_lote(lote_id)
    antes = _snapshot_lote(lote)
    _reservar(lote, versao)
    if status == 'concluido' and ProducaoDiarioEtapa.query.filter_by(lote_id=lote.id, fim_em=None).first():
        raise DiarioError('Conclua as etapas em andamento antes de concluir o lote.')
    lote.status = status
    _auditar(lote, 'concluir_lote' if status == 'concluido' else 'reabrir_lote',
             usuario_id, antes, _snapshot_lote(lote))
    db.session.commit()
    return lote
