"""Vínculo vivo do checklist com unidade e período de Organizar equipe.

Não copia nomes nem altera cargos ou a escala. A responsabilidade é
compartilhada quando há mais de um gerente/atendente chefe no mesmo período.
O admin também pode acrescentar funcionários, liberando só o checklist.
O histórico continua identificando a pessoa que efetivamente preencheu.
"""
from sqlalchemy.orm import joinedload, selectinload

from app.models import ChecklistResponsavel, Funcionario, Loja
from app.services import checklist_loja, treino_lideranca
from app.services.rh_cargos import normalizar_nome_cargo

CARGOS_RESPONSAVEIS = frozenset({'gerente', 'gerente de loja', 'atendente chefe'})


def tem_liberacao(usuario_id):
    """Concessão viva: funcionário e loja precisam continuar ativos."""
    return (ChecklistResponsavel.query
            .join(Funcionario, ChecklistResponsavel.funcionario_id == Funcionario.id)
            .join(Loja, ChecklistResponsavel.loja_id == Loja.id)
            .filter(ChecklistResponsavel.ativo.is_(True),
                    Funcionario.usuario_id == usuario_id, Funcionario.ativo.is_(True),
                    Loja.ativa.is_(True), Loja.nome != 'Industria')
            .first()) is not None


def candidatos():
    return (Funcionario.query.filter_by(ativo=True)
            .options(joinedload(Funcionario.usuario))
            .order_by(Funcionario.nome).all())


def _pessoa(funcionario, adicional=None):
    usuario = funcionario.usuario
    return {'funcionario_id': funcionario.id, 'nome': funcionario.nome,
            'cargo': funcionario.cargo.nome if funcionario.cargo else funcionario.funcao,
            'acesso': bool(usuario and usuario.pode_checklist()),
            'tem_conta': bool(usuario),
            'primeiro_acesso': bool(usuario and usuario.senha_provisoria),
            'observador': bool(usuario and usuario.is_observador()),
            'adicional_id': adicional.id if adicional else None}


def _eh_responsavel(funcionario, lideres_do_periodo):
    cargo = funcionario.cargo.nome if funcionario.cargo else funcionario.funcao
    return (normalizar_nome_cargo(cargo) in CARGOS_RESPONSAVEIS
            or bool(funcionario.id in lideres_do_periodo
                    and funcionario.usuario and funcionario.usuario.is_gerente()))


def loja_do_usuario(usuario):
    """Unidade principal do RH, usada apenas como seleção inicial do checklist."""
    funcionario = usuario.funcionario
    if not funcionario or not funcionario.ativo:
        return None
    return treino_lideranca.unidades_principais([funcionario]).get(funcionario.id)


def quadro(loja_id=None):
    """Retorna lojas, períodos e pessoas, além dos cadastros incompletos.

    Gerência geral/RH não implica responsabilidade de turno. O perfil Gerente
    já atribuído à conta só complementa o cargo quando a pessoa lidera alguém
    na mesma unidade e período (há líderes cujo cargo ainda é Atendente).
    Permissão de gerente, sozinha, não atribui responsabilidade operacional.
    """
    lojas = sorted(checklist_loja.lojas_operacionais(), key=lambda l: l.nome)
    funcionarios = (Funcionario.query.filter_by(ativo=True)
                    .options(joinedload(Funcionario.cargo),
                             joinedload(Funcionario.usuario),
                             selectinload(Funcionario.lojas))
                    .order_by(Funcionario.nome).all())
    unidades = treino_lideranca.unidades_principais(funcionarios)
    por_id = {f.id: f for f in funcionarios}
    lideres_do_periodo = set()
    for pessoa in funcionarios:
        lider = por_id.get(pessoa.lider_id)
        if (lider and unidades.get(lider.id) is not None
                and unidades.get(lider.id) == unidades.get(pessoa.id)
                and lider.periodo in treino_lideranca.PERIODOS_EQUIPE
                and lider.periodo == pessoa.periodo):
            lideres_do_periodo.add(lider.id)
    por_loja = {
        loja.id: {'loja': loja, 'turnos': {
            periodo: [] for periodo in treino_lideranca.PERIODOS_EQUIPE}}
        for loja in lojas}
    pendentes = []
    for funcionario in funcionarios:
        if not _eh_responsavel(funcionario, lideres_do_periodo):
            continue
        unidade_id = unidades.get(funcionario.id)
        if unidade_id is not None and unidade_id not in por_loja:
            continue  # Indústria e lojas inativas não são checklist de loja.
        if loja_id is not None and unidade_id != loja_id:
            continue
        pessoa = _pessoa(funcionario)
        if unidade_id is None or funcionario.periodo not in treino_lideranca.PERIODOS_EQUIPE:
            pendentes.append(pessoa)
            continue
        por_loja[unidade_id]['turnos'][funcionario.periodo].append(pessoa)
    adicionais = ChecklistResponsavel.query.filter_by(ativo=True).all()
    for adicional in adicionais:
        funcionario = por_id.get(adicional.funcionario_id)
        if (not funcionario or adicional.loja_id not in por_loja
                or (loja_id is not None and adicional.loja_id != loja_id)):
            continue
        pessoas = por_loja[adicional.loja_id]['turnos'][adicional.periodo]
        existente = next((p for p in pessoas
                          if p['funcionario_id'] == funcionario.id), None)
        if existente:
            existente['adicional_id'] = adicional.id
        else:
            pessoas.append(_pessoa(funcionario, adicional))
        pessoas.sort(key=lambda p: p['nome'].casefold())
    return {'lojas': [linha for lid, linha in por_loja.items()
                      if loja_id is None or lid == loja_id],
            'pendentes': pendentes}
