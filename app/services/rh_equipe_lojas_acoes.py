"""Ações da tela Equipe por loja: adicionar, importar e remover pessoa.

Só o vínculo pessoa↔loja (`funcionario_loja`, com a marca de unidade
principal) e, quando pedido, o período mudam. Nenhuma função altera cargo,
salário, líder direto, conta de acesso ou os demais campos da ficha; remover
alguém de uma loja nunca desliga a pessoa do RH.

A regra da unidade principal é a de `treino_lideranca` (`escolher_principal`
/ `marcar_unidade_principal` / `validar_periodo`): este módulo não a copia.
Aqui, como na tela (`rh_equipe_lojas.carregar_lojas`), uma loja INATIVA
nunca conta como principal — quem está preso numa loja fechada entra por
"Adicionar" e a loja nova vira a principal.

Sem commit: a rota controla a transação. A tabela de associação não passa
pelo listener automático de auditoria, então cada gesto grava a própria
linha em `AuditLog` (tabela ``funcionario_loja``) com o estado antes/depois.
"""
from __future__ import annotations

import json

from sqlalchemy import delete
from sqlalchemy.orm import joinedload, load_only, selectinload

from app.extensions import db
from app.models import Cargo, Funcionario, Loja, Usuario
from app.models.rh import funcionario_loja
from app.services import treino_lideranca as lideranca
from app.services.rh_cargos import nome_cargo_exibicao

TABELA_AUDITORIA = 'funcionario_loja'
SEM_UNIDADE = 'Sem unidade principal'


class EquipeLojaError(ValueError):
    """Motivo legível para a tela; nada foi gravado quando é levantado."""


# ── Leitura para os formulários ───────────────────────────────────────────

def _pessoas_ativas():
    return (Funcionario.query
            .options(load_only(Funcionario.id, Funcionario.nome,
                               Funcionario.funcao, Funcionario.periodo,
                               Funcionario.ativo, Funcionario.cargo_id,
                               Funcionario.usuario_id),
                     joinedload(Funcionario.cargo).load_only(Cargo.id, Cargo.nome),
                     joinedload(Funcionario.usuario).load_only(
                         Usuario.id, Usuario.is_owner),
                     selectinload(Funcionario.lojas).load_only(
                         Loja.id, Loja.nome, Loja.ativa))
            .filter_by(ativo=True).order_by(Funcionario.nome, Funcionario.id).all())


def _exibicao(pessoa):
    return {
        'id': pessoa.id,
        'nome': pessoa.nome,
        'cargo': nome_cargo_exibicao(
            pessoa.cargo.nome if pessoa.cargo else pessoa.funcao) or 'Sem cargo',
        'periodo': pessoa.periodo,
    }


def _ordem(item):
    return item['nome'].casefold(), item['id']


def candidatos_por_loja(lojas_ids):
    """Listas dos formulários de cada loja: três consultas, qualquer que seja
    o número de lojas (pessoas, lojas delas e marcas de principal).

    ``adicionar``: pessoas ativas ainda sem vínculo com a loja, agrupadas
    pela unidade principal atual (sem unidade primeiro — inclui quem só
    tem loja inativa, mesma leitura da tela). ``importar``: pessoas cuja
    unidade principal é OUTRA loja ativa. Direção fica fora das duas
    listas — ela não integra a lotação de loja nenhuma.
    """
    pessoas = [p for p in _pessoas_ativas() if not lideranca.eh_direcao(p)]
    unidades = lideranca.unidades_principais(pessoas)
    nomes = {loja.id: loja.nome for p in pessoas for loja in p.lojas}
    ativas = {loja.id for p in pessoas for loja in p.lojas if loja.ativa}
    resultado = {}
    for loja_id in lojas_ids:
        adicionar, importar = {}, {}
        for pessoa in pessoas:
            vinculadas = {loja.id for loja in pessoa.lojas}
            principal = unidades.get(pessoa.id)
            titulo = (nomes.get(principal, SEM_UNIDADE) if principal in ativas
                      else SEM_UNIDADE)
            if loja_id not in vinculadas:
                adicionar.setdefault(titulo, []).append(_exibicao(pessoa))
            if principal in ativas and principal != loja_id:
                importar.setdefault(titulo, []).append(_exibicao(pessoa))

        def grupos(mapa):
            ordenados = sorted(mapa.items(), key=lambda item: (
                item[0] != SEM_UNIDADE, item[0].casefold()))
            return [{'titulo': titulo, 'pessoas': sorted(lista, key=_ordem)}
                    for titulo, lista in ordenados]
        resultado[loja_id] = {'adicionar': grupos(adicionar),
                              'importar': grupos(importar)}
    return resultado


# ── Escrita ───────────────────────────────────────────────────────────────

def _loja_ativa(loja):
    if loja is None or not loja.ativa:
        raise EquipeLojaError('Esta loja não está ativa; a equipe dela não pode ser alterada.')
    return loja


def _pessoa(funcionario_id):
    """Relê a ficha sob lock (no Postgres) para dois gestos não se cruzarem."""
    pessoa = None
    if funcionario_id:
        pessoa = (Funcionario.query.filter_by(id=funcionario_id)
                  .with_for_update().first())
    if pessoa is None:
        raise EquipeLojaError('Escolha uma pessoa da lista.')
    if not pessoa.ativo:
        raise EquipeLojaError(f'{pessoa.nome} está desligado(a) do RH; reative a ficha antes.')
    if lideranca.eh_direcao(pessoa):
        raise EquipeLojaError('A direção não entra na lotação das lojas.')
    return pessoa


def _principal_ativa(vinculos):
    """Unidade principal pela regra única, mas só se a loja estiver ativa.

    A tela mostra quem tem marca numa loja fechada como "unidade principal
    a definir"; o gesto precisa ler igual, senão a loja nova entraria como
    vínculo adicional de uma loja que não existe mais na operação.
    """
    escolhida = lideranca.escolher_principal(
        {loja_id: dados['principal'] for loja_id, dados in vinculos.items()})
    if escolhida is None or not vinculos[escolhida]['ativa']:
        return None
    return escolhida


def _periodo_validado(periodo):
    try:
        return lideranca.validar_periodo(periodo)
    except lideranca.LiderancaError as exc:
        raise EquipeLojaError(str(exc)) from None


def _auditar(acao, pessoa, loja, antes, depois, actor_id):
    from app.models import AuditLog
    from app.services.audit import _request_meta
    ip, user_agent = _request_meta()

    def estado(valor):
        if not valor:
            return None
        return json.dumps({'funcionario_id': pessoa.id, 'funcionario': pessoa.nome,
                           'loja_id': loja.id, 'loja': loja.nome, **valor},
                          ensure_ascii=False)
    db.session.add(AuditLog(
        usuario_id=actor_id, tabela=TABELA_AUDITORIA, registro_id=pessoa.id,
        acao=acao, antes=estado(antes), depois=estado(depois),
        ip=ip, user_agent=user_agent))


def definir_principal(pessoa, loja_id):
    """Deixa uma única unidade principal; a loja precisa já estar vinculada."""
    try:
        lideranca.marcar_unidade_principal(pessoa.id, loja_id, inserir=False)
    except lideranca.LiderancaError as exc:
        raise EquipeLojaError(str(exc)) from None


def _inserir_vinculo(pessoa, loja, principal):
    db.session.execute(funcionario_loja.insert().values(
        funcionario_id=pessoa.id, loja_id=loja.id, loja_principal=principal))


def _aplicar_periodo(pessoa, periodo):
    periodo = _periodo_validado(periodo)
    if periodo and pessoa.periodo != periodo:
        pessoa.periodo = periodo
        return True
    return False


def _loja_leve(loja_id):
    """Só id/nome/ativa — a planta da loja (LargeBinary) fica fora."""
    if not loja_id:
        return None
    return (Loja.query.options(load_only(Loja.id, Loja.nome, Loja.ativa))
            .filter_by(id=loja_id).first())


def _nome_loja(loja_id):
    loja = _loja_leve(loja_id)
    return loja.nome if loja else None


def adicionar_pessoa(loja, funcionario_id, *, principal=False, periodo=None,
                     actor_id=None):
    """Vincula uma pessoa ativa à loja.

    Quem não tinha unidade principal ATIVA passa a ter esta (inclusive quem
    só tinha loja fechada — a marca antiga é limpa). Quem já tinha fica
    com um vínculo adicional, a não ser que ``principal`` seja pedido. Uma
    pessoa de loja única que ganha o segundo vínculo tem a loja antiga
    marcada explicitamente como principal, para não cair em "várias
    unidades, sem principal definida" por causa do novo vínculo.
    """
    _loja_ativa(loja)
    pessoa = _pessoa(funcionario_id)
    vinculos = lideranca.vinculos_de(pessoa.id)
    if loja.id in vinculos:
        raise EquipeLojaError(f'{pessoa.nome} já está vinculado(a) a {loja.nome}.')
    principal_antes = _principal_ativa(vinculos)
    vira_principal = bool(principal) or principal_antes is None
    _inserir_vinculo(pessoa, loja, vira_principal)
    if vira_principal:
        definir_principal(pessoa, loja.id)
    else:
        definir_principal(pessoa, principal_antes)
    periodo_mudou = _aplicar_periodo(pessoa, periodo)
    db.session.expire(pessoa, ['lojas'])
    _auditar('insert', pessoa, loja, None,
             {'loja_principal': vira_principal,
              'principal_anterior': _nome_loja(principal_antes),
              'periodo': pessoa.periodo}, actor_id)
    return {'pessoa': pessoa, 'principal': vira_principal,
            'principal_anterior': _nome_loja(principal_antes),
            'periodo_mudou': periodo_mudou}


def importar_pessoa(loja, funcionario_id, *, manter_origem=False, periodo=None,
                    actor_id=None):
    """Transfere a unidade principal de outra loja ativa para esta.

    Por padrão a pessoa sai da loja de origem; ``manter_origem`` conserva
    aquele vínculo como adicional. Outros vínculos secundários ficam como
    estão. Quem não tem unidade principal ativa deve entrar por ``adicionar``.
    """
    _loja_ativa(loja)
    pessoa = _pessoa(funcionario_id)
    vinculos = lideranca.vinculos_de(pessoa.id)
    origem_id = _principal_ativa(vinculos)
    if origem_id is None:
        raise EquipeLojaError(
            f'{pessoa.nome} não tem unidade principal; use "Adicionar pessoa".')
    if origem_id == loja.id:
        raise EquipeLojaError(f'{loja.nome} já é a unidade principal de {pessoa.nome}.')
    origem = _loja_leve(origem_id)
    ja_vinculada = loja.id in vinculos
    if not ja_vinculada:
        _inserir_vinculo(pessoa, loja, True)
    definir_principal(pessoa, loja.id)
    if not manter_origem:
        db.session.execute(delete(funcionario_loja).where(
            funcionario_loja.c.funcionario_id == pessoa.id,
            funcionario_loja.c.loja_id == origem_id))
        _auditar('delete', pessoa, origem,
                 {'loja_principal': True, 'motivo': f'transferência para {loja.nome}'},
                 None, actor_id)
    periodo_mudou = _aplicar_periodo(pessoa, periodo)
    db.session.expire(pessoa, ['lojas'])
    _auditar('update' if ja_vinculada else 'insert', pessoa, loja,
             {'loja_principal': False} if ja_vinculada else None,
             {'loja_principal': True, 'origem': origem.nome if origem else None,
              'origem_mantida': bool(manter_origem), 'periodo': pessoa.periodo},
             actor_id)
    return {'pessoa': pessoa, 'origem': origem.nome if origem else None,
            'origem_mantida': bool(manter_origem), 'periodo_mudou': periodo_mudou}


def remover_pessoa(loja, funcionario_id, *, actor_id=None):
    """Retira só o vínculo com esta loja; a ficha continua ativa no RH.

    Devolve ``nova_principal`` sempre que a unidade principal ATIVA mudar
    com o gesto — tanto quando a loja removida era a principal quanto
    quando a pessoa tinha várias lojas sem principal e a única restante
    passa a contar pela regra de loja única. Líder direto e período não
    mudam.
    """
    pessoa = _pessoa(funcionario_id)
    vinculos = lideranca.vinculos_de(pessoa.id)
    if loja.id not in vinculos:
        raise EquipeLojaError(f'{pessoa.nome} não está vinculado(a) a {loja.nome}.')
    principal_antes = _principal_ativa(vinculos)
    era_principal = principal_antes == loja.id
    db.session.execute(delete(funcionario_loja).where(
        funcionario_loja.c.funcionario_id == pessoa.id,
        funcionario_loja.c.loja_id == loja.id))
    db.session.expire(pessoa, ['lojas'])
    restantes = lideranca.vinculos_de(pessoa.id)
    principal_depois = _principal_ativa(restantes)
    mudou = principal_depois is not None and principal_depois != principal_antes
    _auditar('delete', pessoa, loja,
             {'loja_principal': era_principal,
              'marca_explicita': bool(vinculos[loja.id]['principal'])},
             None, actor_id)
    return {'pessoa': pessoa, 'era_principal': era_principal,
            'restantes': len(restantes),
            'nova_principal': _nome_loja(principal_depois) if mudou else None}
