"""Visão organizacional: pessoas reais do RH, não cenários de promoção."""
from collections import defaultdict

from sqlalchemy.orm import defer, joinedload, selectinload

from app.models import Cargo, Funcionario, Loja, PlanoCarreiraCargoVinculo
from app.services.rh_cargos import nome_cargo_exibicao, normalizar_nome_cargo
from app.services.treino_lideranca import eh_direcao


def chave_cargo(nome):
    return normalizar_nome_cargo(nome_cargo_exibicao(nome))


def carregar_visao(filtros):
    from app.models import RhMovimentacao

    query = Funcionario.query.options(
        joinedload(Funcionario.cargo), joinedload(Funcionario.lider),
        joinedload(Funcionario.usuario),
        selectinload(Funcionario.lojas).defer(Loja.planta_imagem))
    if filtros.get('ativos', '1') != '0':
        query = query.filter_by(ativo=True)
    pessoas = query.order_by(Funcionario.nome, Funcionario.id).all()
    cargos = Cargo.query.all()
    faixas = defaultdict(set)
    vinculos = PlanoCarreiraCargoVinculo.query.options(
        joinedload(PlanoCarreiraCargoVinculo.faixa)).all()
    por_id = {c.id: c for c in cargos}
    for v in vinculos:
        cargo = por_id.get(v.cargo_id)
        if cargo and v.faixa:
            faixas[chave_cargo(cargo.nome)].add((v.faixa.familia, v.faixa.nivel))

    # Uma promoção só é datada quando alguém registrou sua data efetiva.
    ultimas = {}
    if pessoas:
        registros = (RhMovimentacao.query.filter(
            RhMovimentacao.funcionario_id.in_([p.id for p in pessoas]),
            RhMovimentacao.tipo == 'promocao',
            RhMovimentacao.data_efetiva.isnot(None))
            .order_by(RhMovimentacao.data_efetiva.desc(),
                      RhMovimentacao.registrado_em.desc(),
                      RhMovimentacao.id.desc()).all())
        for registro in registros:
            ultimas.setdefault(registro.funcionario_id, registro)

    linhas, distribuicao, lideres = [], {}, {}
    for pessoa in pessoas:
        nome = nome_cargo_exibicao(
            pessoa.cargo.nome if pessoa.cargo else pessoa.funcao) or 'Sem cargo'
        chave = chave_cargo(nome)
        candidatas = faixas.get(chave, set()) if pessoa.cargo else set()
        familia, nivel = next(iter(candidatas)) if len(candidatas) == 1 else (None, None)
        if pessoa.cargo and chave == 'atendente 1' and not candidatas:
            familia, nivel = 'Atendimento', 1
        direcao = eh_direcao(pessoa)
        if direcao:
            nome, chave, familia, nivel = 'Proprietário / Direção', 'direcao', None, None
        lider = pessoa.lider
        if lider:
            lideres[lider.id] = lider
        linha = {
            'pessoa': pessoa, 'cargo_nome': nome, 'cargo_chave': chave,
            'familia': familia, 'nivel': nivel,
            'lider_nome': ('Direção' if direcao else lider.nome if lider else None),
            'direcao': direcao,
            'unidades': sorted(pessoa.lojas, key=lambda l: l.nome),
            'ultima_promocao': ultimas.get(pessoa.id),
        }
        linhas.append(linha)
        grupo = distribuicao.setdefault(chave, {'chave': chave, 'nome': nome,
                                                'total': 0, 'niveis': set()})
        grupo['total'] += 1
        if nivel is not None:
            grupo['niveis'].add(nivel)
    resumo = {
        'total': len(linhas),
        'cargos': sum(chave != 'sem cargo' for chave in distribuicao),
        'lideres': len(lideres),
        'sem_lider': sum(not r['pessoa'].lider_id and not r['direcao'] for r in linhas),
        'sem_nivel': sum(r['nivel'] is None and not r['direcao'] for r in linhas),
    }
    niveis = sorted({r['nivel'] for r in linhas if r['nivel'] is not None})
    for grupo in distribuicao.values():
        grupo['niveis'] = sorted(grupo['niveis'])
    busca = normalizar_nome_cargo(filtros.get('q', ''))

    def corresponde(r):
        p = r['pessoa']
        if busca and busca not in normalizar_nome_cargo(p.nome + ' ' + r['cargo_nome']):
            return False
        if filtros.get('cargo') and filtros['cargo'] != r['cargo_chave']:
            return False
        if filtros.get('loja') and not any(str(l.id) == filtros['loja'] for l in r['unidades']):
            return False
        if filtros.get('lider') and str(p.lider_id or '') != filtros['lider']:
            return False
        if filtros.get('nivel') and str(r['nivel'] or '') != filtros['nivel']:
            return False
        if filtros.get('pendencia') == 'sem_lider' and (p.lider_id or r['direcao']):
            return False
        if filtros.get('pendencia') == 'sem_nivel' and (r['nivel'] is not None or r['direcao']):
            return False
        return True

    filtradas = [r for r in linhas if corresponde(r)]
    return {
        'resumo': resumo, 'linhas': filtradas, 'filtros': filtros,
        'total_filtrado': len(filtradas), 'niveis': niveis,
        'distribuicao': sorted(distribuicao.values(), key=lambda r: r['nome'].casefold()),
        'lideres': sorted(lideres.values(), key=lambda p: p.nome.casefold()),
        'lojas': Loja.query.options(defer(Loja.planta_imagem)).order_by(Loja.nome).all(),
    }
