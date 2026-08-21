"""Associação segura entre a função legada e o Cargo estruturado do RH.

`Funcionario.funcao` existe desde antes do cadastro estruturado de cargos.
Algumas fichas, portanto, têm uma função legível, mas `cargo_id` vazio. O
treinamento usa `cargo_id` para descobrir as trilhas obrigatórias.

Este módulo só associa nomes equivalentes depois de normalizar caixa, acentos
e espaços. Correspondências ausentes ou ambíguas ficam para revisão humana.
"""
import unicodedata

from app.extensions import db
from app.models import Cargo, Funcionario


def normalizar_nome_cargo(valor):
    """Normaliza um nome sem transformar cargos diferentes em sinônimos."""
    texto = unicodedata.normalize('NFKD', str(valor or ''))
    texto = ''.join(c for c in texto if not unicodedata.combining(c))
    return ' '.join(texto.casefold().split())


def _indice_cargos(cargos=None):
    """Devolve somente nomes normalizados que apontam para um único cargo."""
    por_nome = {}
    for cargo in cargos if cargos is not None else Cargo.query.all():
        chave = normalizar_nome_cargo(cargo.nome)
        if chave:
            por_nome.setdefault(chave, []).append(cargo)
    return {nome: itens[0] for nome, itens in por_nome.items()
            if len(itens) == 1}


def encontrar_cargo(funcao, cargos=None):
    """Encontra um cargo por equivalência segura de nome, ou devolve None."""
    return _indice_cargos(cargos).get(normalizar_nome_cargo(funcao))


def associar_funcionario(funcionario, cargos=None):
    """Preenche o cargo de uma ficha, sem substituir decisão já registrada."""
    if funcionario.cargo_id:
        return None
    cargo = encontrar_cargo(funcionario.funcao, cargos)
    if cargo:
        funcionario.cargo_id = cargo.id
    return cargo


def associar_pendentes(funcionarios=None, *, commit=False):
    """Associa em lote fichas sem cargo e informa o que ficou para revisão."""
    cargos = Cargo.query.all()
    indice = _indice_cargos(cargos)
    if funcionarios is None:
        funcionarios = Funcionario.query.filter(
            Funcionario.cargo_id.is_(None)).all()

    associados, sem_correspondencia = [], []
    for funcionario in funcionarios:
        if funcionario.cargo_id:
            continue
        cargo = indice.get(normalizar_nome_cargo(funcionario.funcao))
        if cargo:
            funcionario.cargo_id = cargo.id
            associados.append((funcionario, cargo))
        else:
            sem_correspondencia.append(funcionario)

    if commit and associados:
        db.session.commit()
    return {
        'associados': associados,
        'sem_correspondencia': sem_correspondencia,
    }
