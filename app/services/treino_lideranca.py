"""Hierarquia de liderança e checklists de observação prática."""

from sqlalchemy import select, update

from app.extensions import db
from app.models import (
    Funcionario,
    Loja,
    TreinoChecklistAplicacao,
    TreinoItemChecklist,
)
from app.models.rh import funcionario_loja

PERIODOS_EQUIPE = ('Manhã', 'Tarde')
# O cadastro 45 foi confirmado pelo próprio dono em 25/08/2026. Ele mantém
# uma conta de treinamento separada da conta administrativa owner; por isso o
# vínculo Usuario.is_owner, sozinho, não identifica essa ficha do RH.
FUNCIONARIOS_DIRECAO = frozenset({45})


class LiderancaError(ValueError):
    pass


def eh_direcao(funcionario):
    """Reconhece o dono sem trocar ou invalidar sua conta de treinamento."""
    if funcionario is None:
        return False
    usuario = getattr(funcionario, 'usuario', None)
    return bool(
        funcionario.id in FUNCIONARIOS_DIRECAO
        or (usuario and usuario.is_dono())
    )


def liderados_do(gestor, *, incluir_inativos=False):
    if gestor is None:
        return []
    query = Funcionario.query.filter_by(lider_id=gestor.id)
    if not incluir_inativos:
        query = query.filter_by(ativo=True)
    return query.order_by(Funcionario.nome).all()


def pode_observar(gestor, funcionario, *, is_admin=False):
    return bool(is_admin or (
        gestor is not None and funcionario is not None
        and funcionario.ativo and funcionario.lider_id == gestor.id))


def _propor_vinculos(funcionarios, vinculos):
    """Valida a hierarquia inteira antes de alterar qualquer funcionário."""
    todos = {f.id: f for f in Funcionario.query.all()}
    propostos = {fid: f.lider_id for fid, f in todos.items()}

    for funcionario in funcionarios:
        lider_id = vinculos.get(funcionario.id)
        lider_id = int(lider_id) if lider_id else None
        if lider_id == funcionario.id:
            raise LiderancaError(
                f'{funcionario.nome} não pode ser líder de si mesmo.')
        if lider_id is not None:
            lider = todos.get(lider_id)
            if lider is None or not lider.ativo:
                raise LiderancaError(
                    f'O líder escolhido para {funcionario.nome} não está ativo.')
            if not lider.usuario_id:
                raise LiderancaError(
                    f'{lider.nome} precisa ter uma conta de acesso antes de '
                    'liderar uma equipe.')
        propostos[funcionario.id] = lider_id

    # Uma hierarquia pode ter vários níveis, mas nunca pode formar um círculo.
    for inicio in propostos:
        vistos, atual = set(), inicio
        while atual is not None:
            if atual in vistos:
                nomes = [todos[fid].nome for fid in vistos if fid in todos]
                raise LiderancaError(
                    'A liderança formaria um ciclo entre: ' +
                    ', '.join(sorted(nomes)) + '.')
            vistos.add(atual)
            atual = propostos.get(atual)
    return propostos


def salvar_vinculos(funcionarios, vinculos):
    """Salva líder direto em lote, recusando auto-liderança e ciclos."""
    propostos = _propor_vinculos(funcionarios, vinculos)
    alteracoes = 0

    for funcionario in funcionarios:
        novo = propostos[funcionario.id]
        if funcionario.lider_id != novo:
            funcionario.lider_id = novo
            alteracoes += 1
    db.session.commit()
    return alteracoes


def unidades_principais(funcionarios):
    """Retorna `{funcionario_id: loja_id}` para a unidade principal.

    Cadastros antigos não marcavam `loja_principal`; quando a pessoa pertence
    a uma única unidade, essa unidade é o fallback natural do formulário.
    """
    ids = [funcionario.id for funcionario in funcionarios]
    if not ids:
        return {}
    linhas = db.session.execute(
        select(funcionario_loja.c.funcionario_id,
               funcionario_loja.c.loja_id)
        .where(funcionario_loja.c.funcionario_id.in_(ids),
               funcionario_loja.c.loja_principal.is_(True))
    ).all()
    principais = {funcionario_id: loja_id
                  for funcionario_id, loja_id in linhas}
    for funcionario in funcionarios:
        if funcionario.id not in principais and len(funcionario.lojas) == 1:
            principais[funcionario.id] = funcionario.lojas[0].id
    return principais


def salvar_estrutura(funcionarios, vinculos, unidades, periodos):
    """Salva líder, unidade principal e período numa única transação."""
    propostos = _propor_vinculos(funcionarios, vinculos)
    lojas = {loja.id: loja for loja in Loja.query.filter_by(ativa=True).all()}
    atuais_unidades = unidades_principais(funcionarios)
    dados = {}

    for funcionario in funcionarios:
        loja_id = unidades.get(funcionario.id)
        loja_id = int(loja_id) if loja_id else None
        if loja_id is not None and loja_id not in lojas:
            raise LiderancaError(
                f'A unidade escolhida para {funcionario.nome} não está ativa.')
        periodo = (periodos.get(funcionario.id) or '').strip()
        if periodo and periodo not in PERIODOS_EQUIPE:
            raise LiderancaError(
                f'O período de {funcionario.nome} deve ser Manhã ou Tarde.')
        dados[funcionario.id] = (loja_id, periodo or None)

    alteracoes = {'lideres': 0, 'unidades': 0, 'periodos': 0}
    for funcionario in funcionarios:
        lider_id = propostos[funcionario.id]
        loja_id, periodo = dados[funcionario.id]
        if funcionario.lider_id != lider_id:
            funcionario.lider_id = lider_id
            alteracoes['lideres'] += 1
        if funcionario.periodo != periodo:
            funcionario.periodo = periodo
            alteracoes['periodos'] += 1
        if atuais_unidades.get(funcionario.id) != loja_id:
            alteracoes['unidades'] += 1

        # Preserva vínculos com outras lojas, mas deixa uma única principal.
        db.session.execute(
            update(funcionario_loja)
            .where(funcionario_loja.c.funcionario_id == funcionario.id)
            .values(loja_principal=False))
        if loja_id is not None:
            existe = db.session.execute(
                select(funcionario_loja.c.funcionario_id).where(
                    funcionario_loja.c.funcionario_id == funcionario.id,
                    funcionario_loja.c.loja_id == loja_id)
            ).first()
            if existe is None:
                db.session.execute(funcionario_loja.insert().values(
                    funcionario_id=funcionario.id, loja_id=loja_id,
                    loja_principal=True))
            else:
                db.session.execute(
                    update(funcionario_loja).where(
                        funcionario_loja.c.funcionario_id == funcionario.id,
                        funcionario_loja.c.loja_id == loja_id)
                    .values(loja_principal=True))

    db.session.commit()
    return alteracoes


def checklist_da_trilha(trilha_id, *, criar=False):
    checklist = (TreinoChecklistAplicacao.query
                 .filter_by(trilha_id=trilha_id)
                 .order_by(TreinoChecklistAplicacao.id).first())
    if checklist is None and criar:
        checklist = TreinoChecklistAplicacao(
            trilha_id=trilha_id, descricao='Observação prática', ativo=True)
        db.session.add(checklist)
        db.session.flush()
    return checklist


def itens_ativos(checklist):
    if checklist is None or not checklist.ativo:
        return []
    return [item for item in checklist.itens if item.ativo]


def salvar_checklist(trilha, descricao, linhas):
    """Atualiza a lista preservando IDs; itens removidos ficam inativos."""
    descricao = (descricao or '').strip() or 'Observação prática'
    textos = []
    for linha in linhas:
        texto = (linha or '').strip()
        if texto and texto not in textos:
            textos.append(texto[:300])
    if not textos:
        raise LiderancaError('Inclua pelo menos um item no checklist.')

    checklist = checklist_da_trilha(trilha.id, criar=True)
    checklist.descricao = descricao[:200]
    checklist.ativo = True
    existentes = list(checklist.itens)
    usados = set()
    for ordem, texto in enumerate(textos):
        item = next((existente for existente in existentes
                     if existente.descricao == texto
                     and existente.id not in usados), None)
        if item is None:
            item = TreinoItemChecklist(
                checklist_id=checklist.id, descricao=texto,
                ordem=ordem, ativo=True)
            db.session.add(item)
            db.session.flush()
        else:
            item.ordem = ordem
            item.ativo = True
        usados.add(item.id)
    for item in existentes:
        if item.id not in usados:
            item.ativo = False
    db.session.commit()
    return checklist
