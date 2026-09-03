"""Vínculo vivo do checklist com unidade e período de Organizar equipe.

Não copia nomes nem altera permissões ou a escala. A responsabilidade é
compartilhada quando há mais de um gerente/atendente chefe no mesmo período.
O histórico continua identificando a pessoa que efetivamente preencheu.
"""
from sqlalchemy.orm import joinedload, selectinload

from app.models import Funcionario
from app.services import checklist_loja, treino_lideranca
from app.services.rh_cargos import normalizar_nome_cargo

CARGOS_RESPONSAVEIS = frozenset({'gerente', 'gerente de loja', 'atendente chefe'})


def _eh_responsavel(funcionario):
    cargo = funcionario.cargo.nome if funcionario.cargo else funcionario.funcao
    return (normalizar_nome_cargo(cargo) in CARGOS_RESPONSAVEIS
            or bool(funcionario.usuario and funcionario.usuario.is_gerente()))


def loja_do_usuario(usuario):
    """Unidade principal do RH, usada apenas como seleção inicial do checklist."""
    funcionario = usuario.funcionario
    if not funcionario or not funcionario.ativo:
        return None
    return treino_lideranca.unidades_principais([funcionario]).get(funcionario.id)


def quadro(loja_id=None):
    """Retorna lojas, períodos e pessoas, além dos cadastros incompletos.

    Gerência geral/RH não implica responsabilidade de turno. O perfil Gerente
    já atribuído à conta também é uma fonte válida (há líderes operacionais
    cujo cargo no RH ainda é Atendente). Nunca promove uma conta por nome.
    """
    lojas = sorted(checklist_loja.lojas_operacionais(), key=lambda l: l.nome)
    funcionarios = (Funcionario.query.filter_by(ativo=True)
                    .options(joinedload(Funcionario.cargo),
                             joinedload(Funcionario.usuario),
                             selectinload(Funcionario.lojas))
                    .order_by(Funcionario.nome).all())
    unidades = treino_lideranca.unidades_principais(funcionarios)
    por_loja = {
        loja.id: {'loja': loja, 'turnos': {
            periodo: [] for periodo in treino_lideranca.PERIODOS_EQUIPE}}
        for loja in lojas}
    pendentes = []
    for funcionario in funcionarios:
        if not _eh_responsavel(funcionario):
            continue
        unidade_id = unidades.get(funcionario.id)
        if unidade_id is not None and unidade_id not in por_loja:
            continue  # Indústria e lojas inativas não são checklist de loja.
        if loja_id is not None and unidade_id != loja_id:
            continue
        usuario = funcionario.usuario
        acesso = bool(usuario and not usuario.somente_treino
                      and not usuario.is_observador() and usuario.pode_checklist())
        pessoa = {'nome': funcionario.nome,
                  'cargo': funcionario.cargo.nome if funcionario.cargo else funcionario.funcao,
                  'acesso': acesso}
        if unidade_id is None or funcionario.periodo not in treino_lideranca.PERIODOS_EQUIPE:
            pendentes.append(pessoa)
            continue
        por_loja[unidade_id]['turnos'][funcionario.periodo].append(pessoa)
    return {'lojas': [linha for lid, linha in por_loja.items()
                      if loja_id is None or lid == loja_id],
            'pendentes': pendentes}
