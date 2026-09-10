"""Revisão e aprovação explícita de um lote do plano de carreira.

Não escolhe pessoas, cria vínculos, infere promoções ou faz commit. A rota
assina a revisão para o dono e controla a transação inteira, inclusive o
rollback de exceções. Aqui o estado é revalidado sob locks antes da escrita.
"""
import hashlib
import hmac
import json
from decimal import Decimal

from sqlalchemy import tuple_
from sqlalchemy.orm.attributes import set_committed_value

from app.extensions import db
from app.models import (
    Cargo,
    Funcionario,
    PlanoCarreiraCargoVinculo,
    PlanoCarreiraEnquadramento,
    PlanoCarreiraFaixa,
    PlanoCarreiraImportacao,
    RhMovimentacao,
    Usuario,
)
from app.services import plano_carreira_import
from app.services.rh_cargos import nome_cargo_exibicao
from app.services.rh_remuneracao import comparar, reais, valor_decimal

MAX_LOTE = 200
_ESTADO_MUDOU = ('O cadastro, o plano ou os valores mudaram desde a revisão. '
                 'Selecione as pessoas e revise o lote novamente.')


def motivo_bloqueio(enquadramento, cargo_mapeado):
    """Motivo curto para a seleção na tabela; não altera o enquadramento."""
    if not enquadramento.funcionario or not enquadramento.funcionario.ativo:
        return 'Funcionário inativo.'
    if (enquadramento.decisao or '').strip().casefold() == 'aprovado':
        return 'Já aprovado.'
    if enquadramento.nivel is not None:
        if enquadramento.nivel < 1 or not cargo_mapeado:
            return 'A faixa precisa de um cargo do RH vinculado.'
        if not cargo_mapeado.ativo:
            return 'O cargo da faixa está inativo.'
    return None


def _ids_validos(ids):
    if not isinstance(ids, (list, tuple)) or not ids:
        raise ValueError('Selecione pelo menos uma pessoa para aprovar.')
    if len(ids) > MAX_LOTE:
        raise ValueError(f'Selecione no máximo {MAX_LOTE} pessoas por lote.')
    resultado = []
    for valor in ids:
        if type(valor) is int:
            numero = valor
        elif isinstance(valor, str) and valor.isascii() and valor.isdigit() and len(valor) <= 10:
            numero = int(valor)
        else:
            raise ValueError('A seleção contém um identificador inválido.')
        if numero < 1 or numero > 2147483647:
            raise ValueError('A seleção contém um identificador inválido.')
        resultado.append(numero)
    if len(set(resultado)) != len(resultado):
        raise ValueError('A seleção contém pessoas repetidas. Revise o lote.')
    return sorted(resultado)


def _ler(query, *, bloquear=False):
    query = query.populate_existing()
    if bloquear:
        query = query.with_for_update()
    return query.all()


def _campos(obj, nomes):
    return {nome: getattr(obj, nome) for nome in nomes} if obj else None


def _carregar(ids, *, bloquear):
    """Locks na mesma ordem da promoção: pessoas, cargos, plano e faixas.

    Uma leitura inicial localiza as dependências. Depois dos locks, mudanças
    de identidade dessas dependências fazem a operação recomeçar pela UI.
    A importação pode substituir IDs; sua identidade também entra na revisão.
    """
    consulta = PlanoCarreiraEnquadramento.query.filter(
        PlanoCarreiraEnquadramento.id.in_(ids)).order_by(PlanoCarreiraEnquadramento.id)
    enquadramentos = _ler(consulta)
    if len(enquadramentos) != len(ids):
        raise ValueError('Uma pessoa selecionada não está mais no plano. Revise o lote.')
    identidades = [(e.id, e.funcionario_id, e.importacao_id, e.familia, e.nivel)
                   for e in enquadramentos]
    pessoa_ids = sorted({e.funcionario_id for e in enquadramentos})
    pessoas = {f.id: f for f in _ler(
        Funcionario.query.filter(Funcionario.id.in_(pessoa_ids)).order_by(Funcionario.id),
        bloquear=bloquear)}
    chaves_faixa = {(e.familia, e.nivel) for e in enquadramentos if e.nivel is not None}
    consulta_faixas = PlanoCarreiraFaixa.query.filter(tuple_(
        PlanoCarreiraFaixa.familia, PlanoCarreiraFaixa.nivel).in_(chaves_faixa))
    faixas = _ler(consulta_faixas.order_by(PlanoCarreiraFaixa.id)) if chaves_faixa else []
    identidades_faixas = [(f.id, f.importacao_id, f.familia, f.nivel) for f in faixas]
    faixa_ids = [f.id for f in faixas]
    consulta_vinculos = PlanoCarreiraCargoVinculo.query.filter(
        PlanoCarreiraCargoVinculo.faixa_id.in_(faixa_ids)).order_by(PlanoCarreiraCargoVinculo.id)
    vinculos = _ler(consulta_vinculos) if faixa_ids else []
    identidades_vinculos = [(v.id, v.faixa_id, v.cargo_id) for v in vinculos]
    cargo_ids = {f.cargo_id for f in pessoas.values()} | {v.cargo_id for v in vinculos}
    cargo_ids.discard(None)
    cargos = {c.id: c for c in _ler(
        Cargo.query.filter(Cargo.id.in_(cargo_ids)).order_by(Cargo.id), bloquear=bloquear)}
    if bloquear:
        enquadramentos = _ler(consulta, bloquear=True)
        vinculos = _ler(consulta_vinculos, bloquear=True) if faixa_ids else []
        faixas = _ler(consulta_faixas.order_by(PlanoCarreiraFaixa.id), bloquear=True) if chaves_faixa else []
        if (identidades != [(e.id, e.funcionario_id, e.importacao_id, e.familia, e.nivel)
                            for e in enquadramentos]
                or identidades_faixas != [(f.id, f.importacao_id, f.familia, f.nivel) for f in faixas]
                or identidades_vinculos != [(v.id, v.faixa_id, v.cargo_id) for v in vinculos]):
            raise ValueError(_ESTADO_MUDOU)
    importacao_ids = {e.importacao_id for e in enquadramentos} | {f.importacao_id for f in faixas}
    importacoes = {i.id: i for i in _ler(
        PlanoCarreiraImportacao.query.filter(PlanoCarreiraImportacao.id.in_(importacao_ids))
        .order_by(PlanoCarreiraImportacao.id), bloquear=bloquear)}
    for e in enquadramentos:
        pessoa = pessoas.get(e.funcionario_id)
        set_committed_value(e, 'funcionario', pessoa)
        if pessoa:
            set_committed_value(pessoa, 'cargo', cargos.get(pessoa.cargo_id))
    ultimos = dict(db.session.query(RhMovimentacao.funcionario_id, db.func.max(RhMovimentacao.id))
                   .filter(RhMovimentacao.funcionario_id.in_(pessoa_ids))
                   .group_by(RhMovimentacao.funcionario_id).all())
    return enquadramentos, faixas, vinculos, cargos, importacoes, ultimos


def prever(ids, bloquear=False):
    """Prévia integral sem escrita: inválidos recusam o lote, nunca um subconjunto."""
    ids = _ids_validos(ids)
    with db.session.no_autoflush:
        enquadramentos, faixas, vinculos, cargos, importacoes, ultimos = _carregar(ids, bloquear=bloquear)
        faixa_por_chave = {(f.familia, f.nivel): f for f in faixas}
        vinculo_por_faixa = {v.faixa_id: v for v in vinculos}
        linhas, estados = [], []
        for e in enquadramentos:
            pessoa = e.funcionario
            faixa = faixa_por_chave.get((e.familia, e.nivel)) if e.nivel is not None else None
            vinculo = vinculo_por_faixa.get(faixa.id) if faixa else None
            cargo = cargos.get(vinculo.cargo_id) if vinculo else None
            motivo = motivo_bloqueio(e, cargo)
            if motivo:
                nome = pessoa.nome if pessoa else 'Cadastro ausente'
                raise ValueError(f'{nome}: {motivo}')
            if e.importacao_id not in importacoes or (faixa and faixa.importacao_id != e.importacao_id):
                raise ValueError('O vínculo do plano foi alterado. Revise o enquadramento antes de aprovar.')
            valores = comparar(pessoa, cargo)
            linha = {
                'enquadramento_id': e.id, 'funcionario_id': pessoa.id,
                'nome': pessoa.nome, 'familia': e.familia, 'nivel': e.nivel,
                'decisao_atual': e.decisao,
                'somente_decisao': e.nivel is None,
                'cargo_atual_id': pessoa.cargo_id,
                'cargo_novo_id': cargo.id if cargo else pessoa.cargo_id,
                'cargo_atual': nome_cargo_exibicao(pessoa.cargo.nome if pessoa.cargo else pessoa.funcao),
                'cargo_novo': nome_cargo_exibicao(cargo.nome if cargo else
                                                (pessoa.cargo.nome if pessoa.cargo else pessoa.funcao)),
                **valores,
                'total_alvo_planilha': reais(valor_decimal(e.total_alvo)),
                'diferenca_referencia': valores['total_referencia_novo'] - valores['total_referencia_atual'],
            }
            linhas.append(linha)
            estados.append({
                'enquadramento': _campos(e, ('id', 'importacao_id', 'funcionario_id', 'familia', 'nivel',
                                             'cargo_proposto', 'decisao', 'salario_base_alvo',
                                             'complemento_funcao_alvo', 'total_alvo', 'total_atual')),
                'importacao': _campos(importacoes[e.importacao_id], (
                    'id', 'sha256', 'nome_arquivo', 'referencia', 'importado_em', 'importado_por_id')),
                'pessoa': _campos(pessoa, ('id', 'nome', 'ativo', 'cargo_id', 'funcao', 'salario_base',
                                           'premiacao', 'tem_cargo_confianca')),
                'cargo_atual': _campos(pessoa.cargo, ('id', 'nome', 'salario_base', 'ativo')),
                'cargo_novo': _campos(cargo, ('id', 'nome', 'salario_base', 'ativo')),
                'faixa': _campos(faixa, ('id', 'importacao_id', 'familia', 'nivel', 'cargo_proposto',
                                       'equivalente_mensal', 'complemento_funcao', 'total_alvo')),
                'vinculo': _campos(vinculo, ('id', 'faixa_id', 'cargo_id')),
                'ultimo_registro': ultimos.get(pessoa.id),
                'previa': linha,
            })
        atual = sum((linha['total_referencia_atual'] for linha in linhas), Decimal('0.00'))
        novo = sum((linha['total_referencia_novo'] for linha in linhas), Decimal('0.00'))
        resumo = {
            'quantidade': len(linhas),
            'com_cargo': sum(not linha['somente_decisao'] for linha in linhas),
            'somente_decisao': sum(linha['somente_decisao'] for linha in linhas),
            'cargos_alterados': sum(linha['cargo_atual_id'] != linha['cargo_novo_id'] for linha in linhas),
            'salarios_alterados': sum(linha['salario_base_atual'] != linha['salario_base_novo'] for linha in linhas),
            'total_referencia_atual': atual, 'total_referencia_novo': novo,
            'diferenca_referencia': novo - atual,
        }
        estado = hashlib.sha256(json.dumps(estados, sort_keys=True, default=str).encode()).hexdigest()
        return {'ids': ids, 'linhas': linhas, 'resumo': resumo, 'estado': estado}


def confirmar(ids, estado_esperado, actor_id):
    """Revalida todo o lote, aplica e audita; o chamador faz commit ou rollback."""
    with db.session.no_autoflush:
        actor = db.session.get(Usuario, actor_id) if actor_id else None
        if not actor or not actor.is_dono():
            raise ValueError('Somente o dono pode aprovar o lote.')
        if (not isinstance(estado_esperado, str) or len(estado_esperado) != 64
                or any(c not in '0123456789abcdef' for c in estado_esperado)):
            raise ValueError('Revise o lote antes de confirmar a aprovação.')
        previa = prever(ids, bloquear=True)
        if not hmac.compare_digest(previa['estado'], estado_esperado):
            raise ValueError(_ESTADO_MUDOU)
        # Nenhuma mutação precede a validação de todas as pessoas e valores.
        for id in previa['ids']:
            e = db.session.get(PlanoCarreiraEnquadramento, id)
            e.decisao = 'Aprovado'
            plano_carreira_import.aplicar_cargo_aprovado(
                e, actor_id=actor_id, origem='aprovacao_lote', registrar_decisao=True)
        db.session.flush()
        return previa
