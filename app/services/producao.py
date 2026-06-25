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
                    'fornecedor': mp.fornecedor,
                }
            lista[ing.ingrediente_nome]['quantidade'] += gramas

    for nome, dados in lista.items():
        qtd_kg = dados['quantidade'] / 1000.0
        dados['custo_estimado'] = qtd_kg * dados['custo_por_kg']
        dados['deficit'] = max(0, dados['quantidade'] - dados['estoque_atual'])

    return lista


def ordem_compra_consolidada(itens):
    """Ordem de compra de materia-prima a partir dos itens do plano
    ([{receita_id, multiplicador}]), AGRUPADA POR FORNECEDOR. Reusa
    consolidar_lista_compras — nao reimplementa a explosao da ficha tecnica.

    Por MP: necessario (g), em estoque, A COMPRAR (= deficit, considerando o
    estoque atual) e o custo dessa compra (deficit em kg x custo_por_kg).
    Agrupa por MateriaPrima.fornecedor (campo texto legado); vazio ->
    'Sem fornecedor', que vai por ultimo. Como ainda nao ha controle fino de
    estoque de MP, o 'a comprar' pode coincidir com o necessario — e esperado.

    Retorna {fornecedores: [{nome, itens: [...], subtotal_compra}],
             total_compra, total_necessario}.
    """
    lista = consolidar_lista_compras(itens)
    grupos = {}
    total_compra = 0.0
    total_necessario = 0.0
    for nome, d in lista.items():
        forn = (d.get('fornecedor') or '').strip() or 'Sem fornecedor'
        comprar_g = d.get('deficit', 0) or 0
        custo_compra = (comprar_g / 1000.0) * (d.get('custo_por_kg') or 0)
        grupos.setdefault(forn, []).append({
            'nome': nome,
            'quantidade': d['quantidade'],
            'estoque_atual': d.get('estoque_atual', 0),
            'comprar': comprar_g,
            'custo_compra': custo_compra,
            'custo_estimado': d.get('custo_estimado', 0),
        })
        total_compra += custo_compra
        total_necessario += d.get('custo_estimado', 0)

    # Fornecedores nomeados em ordem alfabetica; 'Sem fornecedor' por ultimo.
    fornecedores = []
    for forn in sorted(grupos, key=lambda f: (f == 'Sem fornecedor', f.lower())):
        itens_ord = sorted(grupos[forn], key=lambda x: x['nome'])
        fornecedores.append({
            'nome': forn,
            'itens': itens_ord,
            'subtotal_compra': sum(i['custo_compra'] for i in itens_ord),
        })

    return {'fornecedores': fornecedores, 'total_compra': total_compra,
            'total_necessario': total_necessario}
