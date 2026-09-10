"""Equipe atual por unidade, com liderança direta e compartilhada.

Esta consulta não altera vínculos nem concede acesso. Contagens usam apenas
a unidade principal de cada pessoa; os demais vínculos são contexto. A saída
contém somente campos de exibição, sem modelos ORM ou dados privados do RH.
"""
from collections import defaultdict

from sqlalchemy.orm import joinedload, load_only, selectinload

from app.models import Cargo, Funcionario, Loja, Usuario
from app.services import treino_lideranca as lideranca
from app.services.rh_cargos import nome_cargo_exibicao

PERIODO_NAO_DEFINIDO = 'Período não definido'


def carregar_lojas():
    """Agrupa pessoas ativas sem duplicar a lotação ou inventar liderança.

    Uma liderança que também responde a outra aparece como cabeçalho no seu
    próprio turno; seu superior continua indicado em ``lider_nome``. Líderes
    externos podem contextualizar equipes, mas não aumentam a lotação local.
    """
    pessoas = (Funcionario.query
        .options(
            load_only(Funcionario.id, Funcionario.nome, Funcionario.funcao,
                      Funcionario.periodo, Funcionario.ativo,
                      Funcionario.cargo_id, Funcionario.usuario_id,
                      Funcionario.lider_id),
            joinedload(Funcionario.cargo).load_only(Cargo.id, Cargo.nome),
            joinedload(Funcionario.usuario).load_only(Usuario.id, Usuario.is_owner),
            joinedload(Funcionario.lider).load_only(
                Funcionario.id, Funcionario.nome, Funcionario.ativo),
            selectinload(Funcionario.lojas).load_only(
                Loja.id, Loja.nome, Loja.ativa))
        .filter_by(ativo=True).order_by(Funcionario.nome, Funcionario.id).all())
    lojas = (Loja.query.options(load_only(Loja.id, Loja.nome, Loja.ativa))
             .filter_by(ativa=True).order_by(Loja.nome, Loja.id).all())
    lojas.sort(key=lambda loja: (loja.nome.casefold(), loja.id))
    por_id = {pessoa.id: pessoa for pessoa in pessoas}
    lojas_ativas = {loja.id: loja for loja in lojas}
    unidades = lideranca.unidades_principais(pessoas)
    unidades_por_id = {
        loja.id: loja for pessoa in pessoas for loja in pessoa.lojas
    }
    direcao_ids = {pessoa.id for pessoa in pessoas if lideranca.eh_direcao(pessoa)}

    def periodo(pessoa):
        return (pessoa.periodo if pessoa.periodo in lideranca.PERIODOS_EQUIPE
                else PERIODO_NAO_DEFINIDO)

    def pessoa_exibicao(pessoa, alerta=None):
        principal = unidades_por_id.get(unidades.get(pessoa.id))
        resultado = {
            'id': pessoa.id,
            'nome': pessoa.nome,
            'cargo': ('Proprietário / Direção' if pessoa.id in direcao_ids else
                      nome_cargo_exibicao(
                          pessoa.cargo.nome if pessoa.cargo else pessoa.funcao)
                      or 'Sem cargo'),
            'periodo': pessoa.periodo,
            'unidade': principal.nome if principal else None,
            'lider_nome': pessoa.lider.nome if pessoa.lider else None,
        }
        if alerta:
            resultado['alerta'] = alerta
        return resultado

    def lider_valido(pessoa):
        if pessoa.lider_id == pessoa.id:
            return None
        return por_id.get(pessoa.lider_id)

    def alerta_lider(pessoa, *, cabecalho=False):
        if not pessoa.lider_id:
            return None if cabecalho else 'Sem líder cadastrado.'
        if pessoa.lider_id == pessoa.id:
            return 'Liderança aponta para a própria pessoa.'
        if pessoa.lider is None:
            return 'Líder cadastrado não encontrado.'
        if not pessoa.lider.ativo:
            return 'Líder cadastrado está inativo.'
        return None

    principais_por_loja = defaultdict(list)
    outros_por_loja = defaultdict(list)
    sem_unidade, direcao = [], []
    for pessoa in pessoas:
        if pessoa.id in direcao_ids:
            direcao.append(pessoa_exibicao(pessoa))
            continue
        principal_id = unidades.get(pessoa.id)
        if principal_id in lojas_ativas:
            principais_por_loja[principal_id].append(pessoa)
        else:
            if principal_id:
                alerta = 'Unidade principal inativa.'
            elif len(pessoa.lojas) > 1:
                alerta = 'Várias unidades vinculadas, sem principal definida.'
            else:
                alerta = 'Sem unidade vinculada.'
            sem_unidade.append(pessoa_exibicao(pessoa, alerta))
        for loja in pessoa.lojas:
            if loja.id == principal_id or loja.id not in lojas_ativas:
                continue
            alerta = ('Vínculo adicional; lotação na unidade principal.'
                      if principal_id else
                      'Vínculo cadastrado; unidade principal não definida.')
            outros_por_loja[loja.id].append(pessoa_exibicao(pessoa, alerta))

    # Um compartilhamento só representa liderança operacional enquanto houver
    # equipe direta ativa nessa unidade/turno. O parceiro não acompanha a si
    # próprio, e uma concessão antiga sem equipe não cria líderes fictícios.
    equipes_diretas = defaultdict(set)
    for pessoa in pessoas:
        principal_id = unidades.get(pessoa.id)
        lider = lider_valido(pessoa)
        if (pessoa.id not in direcao_ids and principal_id in lojas_ativas
                and pessoa.periodo in lideranca.PERIODOS_EQUIPE and lider):
            equipes_diretas[(principal_id, pessoa.periodo, lider.id)].add(pessoa.id)

    # Resolver uma única vez evita consultar toda a hierarquia para cada líder.
    compartilhados = defaultdict(dict)
    parceiros_por_loja = defaultdict(set)
    for vinculo in lideranca.compartilhamentos_validos():
        chave = (vinculo.loja_id, vinculo.periodo, vinculo.lider_id)
        equipe = equipes_diretas.get(chave, set())
        if (vinculo.parceiro_id not in por_id
                or not equipe - {vinculo.lider_id, vinculo.parceiro_id}):
            continue
        compartilhados[chave][vinculo.parceiro_id] = por_id[vinculo.parceiro_id]
        parceiros_por_loja[vinculo.loja_id].add(vinculo.parceiro_id)

    def ordem(item):
        return item['nome'].casefold(), item['id']

    def card_da_loja(loja):
        principais = principais_por_loja[loja.id]
        principais_ids = {pessoa.id for pessoa in principais}
        lideres_locais = {
            lider.id for pessoa in principais
            if (lider := lider_valido(pessoa)) is not None
        } | parceiros_por_loja[loja.id]
        turnos = {
            nome: {'nome': nome, 'total': 0, 'grupos': [], 'sem_lider': []}
            for nome in (*lideranca.PERIODOS_EQUIPE, PERIODO_NAO_DEFINIDO)
        }
        grupos = {}

        def grupo_para(lider, nome_periodo):
            chave = (nome_periodo, lider.id)
            if chave not in grupos:
                alerta = alerta_lider(lider, cabecalho=True)
                if lider.id not in principais_ids:
                    if lider.id in direcao_ids:
                        alerta = 'Direção; não integra a lotação desta unidade.'
                    elif unidades.get(lider.id):
                        alerta = 'Liderança de outra unidade.'
                    else:
                        alerta = 'Liderança sem unidade principal definida.'
                parceiros = compartilhados.get((loja.id, nome_periodo, lider.id), {})
                grupo = {
                    'lider': pessoa_exibicao(lider, alerta),
                    'pessoas': [],
                    'compartilhados': sorted(
                        (pessoa_exibicao(p) for p in parceiros.values()), key=ordem),
                }
                grupos[chave] = grupo
                turnos[nome_periodo]['grupos'].append(grupo)
            return grupos[chave]

        for pessoa in principais:
            nome_periodo = periodo(pessoa)
            turno = turnos[nome_periodo]
            turno['total'] += 1
            if pessoa.id in lideres_locais:
                grupo_para(pessoa, nome_periodo)
            elif (lider := lider_valido(pessoa)) is not None:
                grupo_para(lider, nome_periodo)['pessoas'].append(
                    pessoa_exibicao(pessoa))
            else:
                turno['sem_lider'].append(pessoa_exibicao(pessoa, alerta_lider(pessoa)))

        for turno in turnos.values():
            turno['grupos'].sort(key=lambda grupo: ordem(grupo['lider']))
            turno['sem_lider'].sort(key=ordem)
            for grupo in turno['grupos']:
                grupo['pessoas'].sort(key=ordem)
        return {
            'id': loja.id,
            'nome': loja.nome,
            'total': len(principais),
            'total_lideres': len(
                {grupo['lider']['id'] for grupo in grupos.values()}
                | {parceiro['id'] for grupo in grupos.values()
                   for parceiro in grupo['compartilhados']}),
            'turnos': list(turnos.values()),
            'outros_vinculos': sorted(outros_por_loja[loja.id], key=ordem),
        }

    return {'lojas': [card_da_loja(loja) for loja in lojas],
            'sem_unidade': sorted(sem_unidade, key=ordem),
            'direcao': sorted(direcao, key=ordem)}
