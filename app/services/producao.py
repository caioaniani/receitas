from app.models import MateriaPrima, Receita
from app.services.custos import calcular_custos_receitas


def consolidar_lista_compras(itens):
    """
    Recebe lista de dicts [{receita_id, multiplicador}].
    Retorna dict {mp_nome: {quantidade, unidade, custo_estimado, estoque_atual}}.
    """
    resultado = calcular_custos_receitas()
    pesos = resultado.get('pesos', {})
    mps = {mp.nome: mp for mp in MateriaPrima.query.all()}
    receitas = {r.id: r for r in Receita.query.all()}

    lista = {}

    for item in itens:
        receita = receitas.get(item['receita_id'])
        if not receita:
            continue
        multiplicador = item.get('multiplicador', 1)
        peso_base = receita.peso_base or 1000

        for ing in receita.ingredientes:
            if ing.tipo == 'receita':
                continue

            if ing.tipo == 'mp_direto':
                gramas = (ing.porcentagem or 0) * multiplicador
            else:
                gramas = (ing.porcentagem or 0) / 100.0 * peso_base * multiplicador

            mp = mps.get(ing.ingrediente_nome)
            if not mp:
                continue

            if ing.ingrediente_nome not in lista:
                lista[ing.ingrediente_nome] = {
                    'quantidade': 0,
                    'unidade': mp.unidade,
                    'custo_por_kg': mp.custo_por_kg,
                    'estoque_atual': mp.estoque_atual or 0,
                }
            lista[ing.ingrediente_nome]['quantidade'] += gramas

    for nome, dados in lista.items():
        qtd_kg = dados['quantidade'] / 1000.0
        dados['custo_estimado'] = qtd_kg * dados['custo_por_kg']
        dados['deficit'] = max(0, dados['quantidade'] - dados['estoque_atual'])

    return lista
