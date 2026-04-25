"""Serviço unificado de cálculo de custos de receitas e produtos."""

from app.models import Receita, MateriaPrima, Produto

MAX_PASSES = 5  # máximo de passadas para resolver sub-receitas


def calcular_custos_receitas():
    """Calcula custo unitário de cada receita com suporte a sub-receitas.

    Retorna dict com:
        custos:     {nome_receita: custo_unitario}
        pesos:      {nome_receita: peso_unitario}
        fabricados: [lista de dicts com dados para exibição]
        mp_dict:    {nome_mp: custo_por_kg}
        mp_info:    {nome_mp: {custo_por_kg, unidade}}
        circulares: [nomes de receitas com dependência circular]
    """
    receitas = Receita.query.order_by(Receita.categoria, Receita.nome).all()
    mps = MateriaPrima.query.all()
    mp_dict = {mp.nome: mp.custo_por_kg for mp in mps}
    mp_info = {mp.nome: {'custo_por_kg': mp.custo_por_kg, 'unidade': mp.unidade,
                          'peso_unidade': mp.peso_unidade} for mp in mps}

    custos = {}
    pesos = {}
    fabricados = []

    remaining = list(receitas)
    for _ in range(MAX_PASSES):
        still_remaining = []
        for r in remaining:
            resultado = _calcular_receita(r, custos, mp_info)
            if resultado is None:
                still_remaining.append(r)
                continue

            custo_un, rendimento = resultado
            custos[r.nome] = custo_un
            pesos[r.nome] = r.peso_unitario or 0

            fabricados.append(_fabricado_dict(r, custo_un, rendimento))

        remaining = still_remaining
        if not remaining:
            break

    # Receitas com dependência circular ou faltante
    circulares = []
    for r in remaining:
        custos[r.nome] = 0
        pesos[r.nome] = r.peso_unitario or 0
        circulares.append(r.nome)
        fabricados.append(_fabricado_dict(r, 0, int(r.rendimento_qtd)))

    return {
        'custos': custos,
        'pesos': pesos,
        'fabricados': fabricados,
        'mp_dict': mp_dict,
        'mp_info': mp_info,
        'circulares': circulares,
    }


def calcular_custo_produto(produto, receita_custos, mp_info):
    """Calcula custo total de um produto/cesta.

    Para MPs com unidade 'g' ou 'ml', quantidade está em gramas/ml.
    Para MPs com unidade 'un', quantidade é unidades e custo é direto.
    """
    embalagem = produto.custo_embalagem or 0
    if produto.itens:
        custo = 0
        for item in produto.itens:
            if item.tipo == 'receita':
                custo += receita_custos.get(item.item_nome, 0) * item.quantidade
            else:
                info = mp_info.get(item.item_nome, {})
                custo_kg = info.get('custo_por_kg', 0)
                if info.get('unidade') in ('g', 'ml'):
                    custo += (custo_kg / 1000) * item.quantidade
                else:
                    custo += custo_kg * item.quantidade
        return custo + embalagem
    elif produto.custo_direto:
        return produto.custo_direto + embalagem
    return embalagem


def calcular_rendimento(receita, custos_dict=None):
    """Calcula rendimento de uma receita (número de unidades produzidas)."""
    sum_pct = sum(
        ing.porcentagem for ing in receita.ingredientes
        if (ing.tipo or 'mp') == 'mp'
    )
    qtd_direto = sum(
        ing.porcentagem for ing in receita.ingredientes
        if ing.tipo == 'mp_direto'
    )
    total_qtd = receita.peso_base * sum_pct / 100 + qtd_direto
    perda = receita.perda_percentual or 0
    peso_pos_perda = total_qtd * (1 - perda / 100)

    if receita.peso_unitario and receita.peso_unitario > 0 and peso_pos_perda > 0:
        return int(peso_pos_perda / receita.peso_unitario)
    return int(receita.rendimento_qtd)


# ── Funções internas ──

def _custo_por_grama(info):
    """Converte MP em 'R$ por grama' considerando como ela foi cadastrada.

    - g/ml: custo_por_kg / 1000
    - un + peso_unidade: custo_por_unidade / peso_unidade
    - un sem peso_unidade: 0 (nao da pra converter)
    """
    if not info:
        return 0
    custo = info.get('custo_por_kg') or 0
    unidade = info.get('unidade')
    if unidade in ('g', 'ml'):
        return custo / 1000
    if unidade == 'un':
        peso = info.get('peso_unidade') or 0
        return custo / peso if peso > 0 else 0
    return 0


def _calcular_receita(r, custos, mp_info):
    """Calcula custo de uma receita. Retorna (custo_un, rendimento) ou None se dependência faltante."""
    custo_total = 0
    sum_pct = 0
    qtd_direto = 0

    for ing in r.ingredientes:
        tipo = ing.tipo or 'mp'
        if tipo == 'receita':
            if ing.ingrediente_nome not in custos:
                return None  # dependência não resolvida ainda
            custo_total += custos[ing.ingrediente_nome] * ing.porcentagem
        elif tipo == 'mp_direto':
            qtd_g = ing.porcentagem
            info = mp_info.get(ing.ingrediente_nome, {})
            custo_total += qtd_g * _custo_por_grama(info)
            qtd_direto += qtd_g
        else:
            sum_pct += ing.porcentagem
            qtd_g = r.peso_base * ing.porcentagem / 100
            info = mp_info.get(ing.ingrediente_nome, {})
            custo_total += qtd_g * _custo_por_grama(info)

    total_qtd = r.peso_base * sum_pct / 100 + qtd_direto
    perda = r.perda_percentual or 0
    peso_pos_perda = total_qtd * (1 - perda / 100)

    if r.peso_unitario and r.peso_unitario > 0 and peso_pos_perda > 0:
        rendimento = int(peso_pos_perda / r.peso_unitario)
    else:
        rendimento = int(r.rendimento_qtd)

    embalagem = r.custo_embalagem or 0
    custo_un = (custo_total / rendimento + embalagem) if rendimento > 0 else 0

    return custo_un, rendimento


def _fabricado_dict(r, custo_un, rendimento):
    """Monta dict de exibição de uma receita fabricada."""
    return {
        'id': r.id,
        'nome': r.nome,
        'categoria': r.categoria or 'Outros',
        'peso_unitario': r.peso_unitario,
        'rendimento': rendimento,
        'custo_un': custo_un,
        'preco_atacado': r.preco_venda or 0,
        'preco_loja': r.preco_loja or 0,
        'preco_site': r.preco_site or 0,
        'vazia': len(r.ingredientes) == 0,
        'observacao': r.observacao or '',
    }
