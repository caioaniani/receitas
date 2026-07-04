"""Calculadora de compras (03/07/2026): "vou produzir X unidades disto —
quanto preciso comprar de mercadoria?"

Reusa o motor CANÔNICO de explosão da ficha técnica
(`producao.consolidar_lista_compras` → `ordem_compra_consolidada`) — as
mesmas contas do MRP e do botão produzir. Nada de fórmula duplicada.

Conversões de entrada:
- **Receita** (qtd em unidades): multiplicador = qtd ÷ rendimento_massa_crua
  (o MESMO helper do produzir — receita MONTADA de sub-receita cai no
  rendimento cadastrado; ver `massa_base.py::rendimento_massa_crua`).
- **Produto-cesta**: explode `componentes_de_cesta` — componente RECEITA
  entra na explosão de MP; componente PRODUTO ou MP é comprado PRONTO
  (seção `compras_diretas` — a padaria não fabrica o iogurte da cesta).
- **Produto simples** (sem componentes): compra direta dele mesmo.

Sub-receitas DENTRO da ficha (ex: Almond consome croissant pronto) NÃO
explodem em MP aqui — a produção real consome a sub-receita PRONTA do
estoque (`consumir_subreceitas_prontas`), então explodi-la em farinha
mentiria sobre a compra. Elas entram em `sub_receitas` como aviso.
"""
from app.extensions import db
from app.models import Produto, Receita

# Rendimento mínimo pra não dividir por zero em ficha incompleta.
_REND_MIN = 1e-9


def _mult_para(receita, qtd):
    """Multiplicador de batidas pra `qtd` unidades da receita."""
    from app.services.massa_base import rendimento_massa_crua
    rend = float(rendimento_massa_crua(receita) or 0)
    if rend <= _REND_MIN:
        rend = float(receita.rendimento_qtd or 1) or 1.0
    return qtd / rend


def calcular(entradas, considerar_estoque=True):
    """`entradas`: [{'tipo': 'receita'|'produto', 'id': int, 'qtd': int}].
    `considerar_estoque=False`: "a comprar" = necessário CHEIO (ignora o
    estoque de MP — útil pra orçar um evento sem mexer no que está reservado
    à operação do dia). Só re-rotula a saída do motor; a explosão é a mesma.

    Retorna {'compra': <ordem_compra_consolidada>, 'compras_diretas': [...],
             'sub_receitas': [...], 'itens_ok': [...], 'avisos': [...],
             'detalhes': [...] (rastro informativo — NÃO é problema)}.
    """
    from app.services.cestas import componentes_de_cesta
    from app.services.producao import ordem_compra_consolidada

    receita_itens = []      # [{receita_id, multiplicador}] pro motor
    compras_diretas = {}    # nome -> {'qtd', 'tipo'}
    sub_receitas = {}       # nome -> unidades-base que a consomem (informativo)
    itens_ok = []
    avisos = []
    detalhes = []           # rastro "como calculei" (neutro, não é aviso)

    def _add_receita(receita, qtd, origem=None):
        receita_itens.append({'receita_id': receita.id,
                              'multiplicador': _mult_para(receita, qtd)})
        for ing in receita.ingredientes:
            if (ing.tipo or '') == 'receita':
                nome_sub = ing.ingrediente_nome or '(sub-receita)'
                sub_receitas[nome_sub] = sub_receitas.get(nome_sub, 0) + qtd
        if origem:
            detalhes.append(f'{origem}: componente "{receita.nome}" '
                            f'({qtd} un) entrou na explosão de matéria-prima.')

    for e in entradas:
        try:
            qtd = int(e.get('qtd') or 0)
        except (TypeError, ValueError):
            qtd = 0
        if qtd <= 0:
            continue
        tipo, item_id = e.get('tipo'), e.get('id')
        if tipo == 'receita':
            r = db.session.get(Receita, item_id)
            if r is None:
                avisos.append(f'Receita id={item_id} não encontrada — pulada.')
                continue
            _add_receita(r, qtd)
            itens_ok.append({'nome': r.nome, 'qtd': qtd, 'tipo': 'receita'})
        elif tipo == 'produto':
            p = db.session.get(Produto, item_id)
            if p is None:
                avisos.append(f'Produto id={item_id} não encontrado — pulado.')
                continue
            itens_ok.append({'nome': p.nome, 'qtd': qtd, 'tipo': 'produto'})
            comps = componentes_de_cesta(p)
            if not comps:
                # Produto simples (geleia, bebida...): compra-se pronto.
                d = compras_diretas.setdefault(
                    p.nome, {'qtd': 0, 'tipo': 'produto'})
                d['qtd'] += qtd
                continue
            for col, cid, nome_comp, qtd_comp in comps:
                total = qtd * float(qtd_comp or 1)
                if total <= 0:
                    continue
                if col == 'receita_id':
                    r = db.session.get(Receita, cid)
                    if r is None:
                        avisos.append(f'Componente "{nome_comp}" da cesta '
                                      f'"{p.nome}" sem receita — pulado.')
                        continue
                    _add_receita(r, int(round(total)), origem=p.nome)
                else:
                    # Componente produto/MP: comprado pronto, não fabricado.
                    d = compras_diretas.setdefault(
                        nome_comp,
                        {'qtd': 0,
                         'tipo': 'produto' if col == 'produto_id' else 'mp'})
                    d['qtd'] += total

    compra = (ordem_compra_consolidada(receita_itens)
              if receita_itens else
              {'fornecedores': [], 'total_compra': 0.0,
               'total_necessario': 0.0})

    if not considerar_estoque:
        # Ignora o estoque: comprar = necessário cheio; custo da compra =
        # custo do necessário. Re-rotula a saída do motor (sem refazer conta).
        for f in compra['fornecedores']:
            for it in f['itens']:
                it['comprar'] = it['quantidade']
                it['custo_compra'] = it['custo_estimado']
            f['subtotal_compra'] = sum(i['custo_compra'] for i in f['itens'])
        compra['total_compra'] = compra['total_necessario']

    return {
        'compra': compra,
        'compras_diretas': [
            {'nome': n, 'qtd': d['qtd'], 'tipo': d['tipo']}
            for n, d in sorted(compras_diretas.items())],
        'sub_receitas': [
            {'nome': n, 'unidades_base': q}
            for n, q in sorted(sub_receitas.items())],
        'itens_ok': itens_ok,
        'avisos': avisos,
        'detalhes': detalhes,
        'considerar_estoque': considerar_estoque,
    }
