"""Associação segura entre a função legada e o Cargo estruturado do RH.

`Funcionario.funcao` existe desde antes do cadastro estruturado de cargos.
Algumas fichas, portanto, têm uma função legível, mas `cargo_id` vazio. O
treinamento usa `cargo_id` para descobrir as trilhas obrigatórias.

Este módulo só associa nomes equivalentes depois de normalizar caixa, acentos
e espaços. Correspondências ausentes ou ambíguas ficam para revisão humana.
"""
import json
import unicodedata
from decimal import Decimal

from app.extensions import db
from app.models import Cargo, Funcionario

_ATENDENTE_NIVEL_1 = frozenset({'atendente', 'atendente 1'})


def nome_cargo_exibicao(nome):
    """Nome de exibição; a equivalência não altera cargo, salário ou nível."""
    if normalizar_nome_cargo(nome) in _ATENDENTE_NIVEL_1:
        return 'Atendente 1'
    return str(nome or '').strip()


def encontrar_cargo_equivalente(nome, cargos=None):
    """Lookup explícito para importações, sem mudar o backfill legado.

    Só resolve um nome equivalente quando há UM cadastro ativo. Antes da
    consolidação, dois cadastros ativos continuam exigindo a decisão do RH.
    Atendente 2 e Atendente chefe nunca fazem parte dessa equivalência.
    """
    chave = normalizar_nome_cargo(nome_cargo_exibicao(nome))
    candidatos = [
        cargo for cargo in (cargos if cargos is not None else Cargo.query.all())
        if cargo.ativo is not False
        and normalizar_nome_cargo(nome_cargo_exibicao(cargo.nome)) == chave
    ]
    return candidatos[0] if len(candidatos) == 1 else None


def normalizar_nome_cargo(valor):
    """Normaliza um nome sem transformar cargos diferentes em sinônimos."""
    texto = unicodedata.normalize('NFKD', str(valor or ''))
    texto = ''.join(c for c in texto if not unicodedata.combining(c))
    return ' '.join(texto.casefold().split())


def _indice_cargos(cargos=None):
    """Nomes exatos normalizados com um único cargo ativo correspondente.

    Cadastros inativos são mantidos para histórico, mas não podem voltar a
    receber funcionários por um formulário legado ou pelo backfill. Não
    aplica aliases neste caminho automático: isso poderia trocar o salário
    efetivo de uma ficha antiga que ainda não possui cargo estruturado.
    """
    por_nome = {}
    for cargo in cargos if cargos is not None else Cargo.query.all():
        if cargo.ativo is False:
            continue
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


def _dados_unificacao_atendentes(*, bloquear=False):
    from app.models import PlanoCarreiraCargoVinculo, TreinoTrilhaCargo

    consulta = Cargo.query.order_by(Cargo.id)
    if bloquear:
        consulta = consulta.with_for_update()
    cargos = [cargo for cargo in consulta.all()
              if normalizar_nome_cargo(cargo.nome) in _ATENDENTE_NIVEL_1]
    ids = [cargo.id for cargo in cargos]
    consultas = [
        Funcionario.query.filter(Funcionario.cargo_id.in_(ids)),
        PlanoCarreiraCargoVinculo.query.filter(
            PlanoCarreiraCargoVinculo.cargo_id.in_(ids)),
        TreinoTrilhaCargo.query.filter(TreinoTrilhaCargo.cargo_id.in_(ids)),
    ]
    funcionarios, vinculos, trilhas = [
        (consulta.with_for_update() if bloquear else consulta).all()
        for consulta in consultas
    ]
    contagens = {cargo.id: sum(f.cargo_id == cargo.id for f in funcionarios)
                 for cargo in cargos}
    ativos = [cargo for cargo in cargos if cargo.ativo is not False]
    # Mantém o cadastro estabelecido/populado. Em empate, prefere o nome
    # legado para não trocar contratos pela referência salarial da planilha.
    alvo = max(ativos, key=lambda cargo: (
        contagens[cargo.id], normalizar_nome_cargo(cargo.nome) == 'atendente',
        -cargo.id), default=None)
    origens = [cargo for cargo in cargos if cargo != alvo]
    origem_ids = {cargo.id for cargo in origens}
    afetados = [f for f in funcionarios if f.cargo_id in origem_ids]
    impedimentos = []
    if cargos and alvo is None:
        impedimentos.append('Nenhum cadastro equivalente está ativo.')
    if alvo:
        # salario_efetivo usa Cargo, não o cache Funcionario.salario_base.
        # Não é possível preservar salários diferentes apenas remapeando FK.
        salario_alvo = Decimal(str(alvo.salario_base or 0))
        por_id = {cargo.id: cargo for cargo in cargos}
        divergentes = [f.id for f in afetados
                       if Decimal(str(por_id[f.cargo_id].salario_base or 0))
                       != salario_alvo]
        if divergentes:
            impedimentos.append(
                'A unificação alteraria o salário efetivo de '
                f'{len(divergentes)} funcionário(s). Revise os cadastros antes.')

    trilhas_alvo = {t.trilha_id: t for t in trilhas
                   if alvo and t.cargo_id == alvo.id}
    trilhas_pendentes = [t for t in trilhas if t.cargo_id in origem_ids and (
        t.trilha_id not in trilhas_alvo
        or (t.obrigatoria and not trilhas_alvo[t.trilha_id].obrigatoria))]
    vinculos_origem = [v for v in vinculos if v.cargo_id in origem_ids]
    pendente = bool(alvo and (
        afetados or vinculos_origem or trilhas_pendentes
        or any(c.ativo is not False for c in origens)))
    return {
        'alvo': alvo,
        'origens': origens,
        'funcionarios': afetados,
        'vinculos': vinculos_origem,
        'trilhas': trilhas,
        'contagens': contagens,
        'impedimentos': impedimentos,
        'aplicavel': pendente and not impedimentos,
        'ja_unificado': bool(alvo and not pendente),
    }


def resumo_unificacao_atendentes():
    """Prévia somente leitura da consolidação explicitamente pedida pelo RH."""
    return _dados_unificacao_atendentes()


def unificar_atendentes(autor_id, *, commit=False):
    """Consolida Atendente/Atendente 1 sem alterar remuneração nem históricos.

    Chamar somente por ação explícita e autorizada do RH, nunca no startup.
    Reaponta funcionários e vínculos do plano, conserva a união das trilhas
    obrigatórias, mantém cargos de origem inativos e não apaga seus vínculos
    de treinamento. A auditoria guarda IDs/nomes anteriores e o responsável.
    Não mexe nas folhas pagas, no enquadramento importado nem nas promoções.
    """
    from app.models import AuditLog, TreinoTrilhaCargo, Usuario

    if not autor_id or db.session.get(Usuario, autor_id) is None:
        raise ValueError('Informe o responsável pela unificação.')
    dados = _dados_unificacao_atendentes(bloquear=True)
    if dados['impedimentos']:
        raise ValueError(' '.join(dados['impedimentos']))
    if not dados['aplicavel']:
        return dados

    alvo = dados['alvo']
    origem_ids = {cargo.id for cargo in dados['origens']}
    antes = {
        'operacao': 'unificacao_atendente_nivel_1',
        'cargos': [{'id': c.id, 'nome': c.nome, 'ativo': c.ativo,
                    'salario_base': c.salario_base} for c in dados['origens']],
        'funcionarios': [{'id': f.id, 'cargo_id': f.cargo_id, 'funcao': f.funcao}
                         for f in dados['funcionarios']],
        'vinculos_plano': [{'id': v.id, 'cargo_id': v.cargo_id}
                          for v in dados['vinculos']],
        'trilhas_alvo': [{'id': t.id, 'trilha_id': t.trilha_id,
                          'obrigatoria': t.obrigatoria}
                         for t in dados['trilhas'] if t.cargo_id == alvo.id],
    }
    for funcionario in dados['funcionarios']:
        funcionario.cargo = alvo
        funcionario.cargo_id = alvo.id
        # Não reescreve uma função diferente que o RH tenha preenchido.
        if normalizar_nome_cargo(funcionario.funcao) in _ATENDENTE_NIVEL_1:
            funcionario.funcao = alvo.nome
    for vinculo in dados['vinculos']:
        vinculo.cargo = alvo
        vinculo.cargo_id = alvo.id
    trilhas_alvo = {t.trilha_id: t for t in dados['trilhas']
                   if t.cargo_id == alvo.id}
    for trilha in dados['trilhas']:
        if trilha.cargo_id not in origem_ids:
            continue
        destino = trilhas_alvo.get(trilha.trilha_id)
        if destino is None:
            destino = TreinoTrilhaCargo(
                trilha_id=trilha.trilha_id, cargo_id=alvo.id,
                obrigatoria=trilha.obrigatoria)
            db.session.add(destino)
            trilhas_alvo[trilha.trilha_id] = destino
        elif trilha.obrigatoria:
            destino.obrigatoria = True
    for cargo in dados['origens']:
        cargo.ativo = False
    db.session.add(AuditLog(
        usuario_id=autor_id, tabela='rh_cargo_unificacao', registro_id=alvo.id,
        acao='update', antes=json.dumps(antes, ensure_ascii=False),
        depois=json.dumps({
            'cargo_canonico_id': alvo.id, 'cargo_canonico_nome': alvo.nome,
            'origens_inativadas': sorted(origem_ids),
            'funcionarios_ids': [f.id for f in dados['funcionarios']],
            'trilhas_obrigatorias': sorted(
                t.trilha_id for t in trilhas_alvo.values() if t.obrigatoria),
            'remuneracao_alterada': False,
        }, ensure_ascii=False)))
    if commit:
        db.session.commit()
    dados['unificado'] = True
    return dados
