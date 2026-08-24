"""Hierarquia de liderança e checklists de observação prática."""

from app.extensions import db
from app.models import (
    Funcionario,
    TreinoChecklistAplicacao,
    TreinoItemChecklist,
)


class LiderancaError(ValueError):
    pass


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


def salvar_vinculos(funcionarios, vinculos):
    """Salva líder direto em lote, recusando auto-liderança e ciclos."""
    todos = {f.id: f for f in Funcionario.query.all()}
    propostos = {fid: f.lider_id for fid, f in todos.items()}
    alteracoes = 0

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

    for funcionario in funcionarios:
        novo = propostos[funcionario.id]
        if funcionario.lider_id != novo:
            funcionario.lider_id = novo
            alteracoes += 1
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
