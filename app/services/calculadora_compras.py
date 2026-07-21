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
from app.models import MateriaPrima, Produto, Receita
from app.utils import SUB_RECEITA_TIPOS, unidades_subreceita

# Rendimento mínimo pra não dividir por zero em ficha incompleta.
_REND_MIN = 1e-9


def _mult_para(receita, qtd):
    """Multiplicador de batidas pra `qtd` unidades da receita.

    PRIORIDADE = rendimento DECLARADO na ficha (`rendimento_qtd`, o campo
    "Quantidade" que o dono mantém). O rendimento por massa crua (massa ÷
    peso_unitario) fica de fallback: em produto COZIDO (geleia: 1.030 g viram
    25 potes de 40 g) a massa ignora a perda de cozimento e dava 25,75/batida
    → 150 potes viravam 145,6 e o morango vinha 3% a menos (caso real
    04/07/2026). Pra COMPRA, vale o que a ficha declara que rende."""
    from app.services.massa_base import rendimento_massa_crua
    rend = float(receita.rendimento_qtd or 0)
    if rend <= _REND_MIN:
        rend = float(rendimento_massa_crua(receita) or 0)
    if rend <= _REND_MIN:
        rend = 1.0
    return qtd / rend


def calcular(entradas, considerar_estoque=True, explodir_retorno=True):
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
    producao = {}           # receita_id -> {nome, categoria, qtd} a PRODUZIR
    sub_receitas = {}       # nome -> unidades-base que a consomem (informativo)
    itens_ok = []
    avisos = []
    detalhes = []           # rastro "como calculei" (neutro, não é aviso)

    def _eh_retorno(sub_id):
        """Receita de RETORNO (destino de sobra, ex: 'Croissant — Retorno'):
        NUNCA vira compra — nasce das sobras devolvidas, não de mercadoria."""
        return db.session.query(Receita.id).filter(
            Receita.retorno_receita_id == sub_id).first() is not None

    def _add_receita(receita, qtd, origem=None, _visitados=None,
                     registrar_producao=True):
        _visitados = set(_visitados or ())
        if receita.id in _visitados:
            avisos.append(f'Ciclo de sub-receitas em "{receita.nome}" — '
                          'explosão interrompida nesse ramo.')
            return
        _visitados.add(receita.id)
        # "Para produção": unidades da receita a PRODUZIR (o que se passa pro
        # padeiro). Só os itens DIRETOS — entrada top-level + componente de
        # cesta/produto montado. A explosão de sub-receita/retorno é INSUMO,
        # não ordem de produção (massa/levain/mix saem no cronograma), então
        # entra com registrar_producao=False.
        if registrar_producao:
            d = producao.setdefault(receita.id, {'nome': receita.nome,
                                                 'categoria': receita.categoria,
                                                 'qtd': 0.0})
            d['qtd'] += qtd
        mult = _mult_para(receita, qtd)
        receita_itens.append({'receita_id': receita.id,
                              'multiplicador': mult})
        for ing in receita.ingredientes:
            if (ing.tipo or '') not in SUB_RECEITA_TIPOS:
                continue
            nome_sub = ing.ingrediente_nome or '(sub-receita)'
            sub = ing.sub_receita
            # Sub-receita NORMAL (ex: Massa para folhar) EXPLODE em MP —
            # a manteiga do folhado tem que aparecer na compra (03/07/2026,
            # caso real: 300 croissants mostravam 105 g de manteiga-folhar
            # porque a massa ficava só "consumida pronta"). RETORNO não
            # explode (vem de sobra, não de compra); órfã (sem FK) só avisa.
            if sub is None:
                sub_receitas[nome_sub] = sub_receitas.get(nome_sub, 0) + qtd
                avisos.append(f'"{receita.nome}": sub-receita "{nome_sub}" '
                              'sem vínculo (órfã) — NÃO entrou na compra. '
                              'Vincule na ficha.')
                continue
            if _eh_retorno(sub.id):
                sub_receitas[nome_sub] = sub_receitas.get(nome_sub, 0) + qtd
                if explodir_retorno:
                    # Pedido do dono (04/07/2026): comprar os insumos como se
                    # os retornos fossem produzidos FRESCOS — explode pela
                    # receita de ORIGEM (a ficha do retorno e vazia por
                    # design). ATENCAO: se houver sobras reais no estoque,
                    # isso compra em dobro — por isso e toggle na tela.
                    unidades_sub = unidades_subreceita(
                        ing.tipo, ing.porcentagem, receita.peso_base) * mult
                    origem_rec = Receita.query.filter_by(
                        retorno_receita_id=sub.id).first()
                    if origem_rec is not None and unidades_sub > 0:
                        detalhes.append(
                            f'{receita.nome}: retorno "{sub.nome}" '
                            f'({unidades_sub:g} un) explodido como '
                            f'"{origem_rec.nome}" FRESCO.')
                        _add_receita(origem_rec, unidades_sub,
                                     _visitados=_visitados,
                                     registrar_producao=False)
                continue
            unidades_sub = unidades_subreceita(
                ing.tipo, ing.porcentagem, receita.peso_base) * mult
            if unidades_sub <= 0:
                continue
            detalhes.append(f'{receita.nome}: sub-receita "{sub.nome}" '
                            f'({unidades_sub:g} un) explodida em '
                            'matéria-prima.')
            _add_receita(sub, unidades_sub, _visitados=_visitados,
                         registrar_producao=False)
        if origem:
            detalhes.append(f'{origem}: componente "{receita.nome}" '
                            f'({qtd:g} un) entrou na explosão de '
                            'matéria-prima.')

    def _add_produto(p, qtd, origem=None, _vistos=None):
        """Produto SEM composição = comprado pronto (Mini Manteigas).
        Produto COM composição (Iogurte 600ml = receita + pote; Granola
        500g) é MONTADO pela padaria → explode recursivamente (04/07/2026,
        pedido do dono: 'o resto eu produzo, deveria explodir')."""
        _vistos = set(_vistos or ())
        if p.id in _vistos:
            avisos.append(f'Ciclo de produtos em "{p.nome}" — parado.')
            return
        _vistos.add(p.id)
        comps = componentes_de_cesta(p)
        if not comps:
            d = compras_diretas.setdefault(p.nome, {'qtd': 0,
                                                    'tipo': 'produto',
                                                    'unidade': 'un'})
            d['qtd'] += qtd
            return
        if origem:
            detalhes.append(f'{origem}: "{p.nome}" ({qtd:g} un) é montado — '
                            'explodido nos componentes.')
        for col, cid, nome_comp, qtd_comp in comps:
            total = qtd * float(qtd_comp or 1)
            if total <= 0:
                continue
            if col == 'receita_id':
                r = db.session.get(Receita, cid)
                if r is None:
                    avisos.append(f'Componente "{nome_comp}" de '
                                  f'"{p.nome}" sem receita — pulado.')
                    continue
                _add_receita(r, total, origem=p.nome)
            elif col == 'produto_id':
                sub = db.session.get(Produto, cid)
                if sub is None:
                    avisos.append(f'Componente "{nome_comp}" de '
                                  f'"{p.nome}" sem produto — pulado.')
                    continue
                _add_produto(sub, total, origem=p.nome, _vistos=_vistos)
            else:
                # MP componente (pote, embalagem, RECHEIO): comprada como
                # está, na unidade REAL da MP (g/ml/un). Antes tudo caía como
                # 'un' e recheio em gramas (Nutella, mussarela, peito de peru)
                # aparecia "15000 un" em vez de "15.000 g".
                mp = db.session.get(MateriaPrima, cid)
                unidade = (mp.unidade if mp else None) or 'un'
                d = compras_diretas.setdefault(
                    nome_comp, {'qtd': 0, 'tipo': 'mp', 'unidade': unidade})
                d['qtd'] += total

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
            _add_produto(p, qtd)

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
            {'nome': n, 'qtd': d['qtd'], 'tipo': d['tipo'],
             'unidade': d.get('unidade', 'un')}
            for n, d in sorted(compras_diretas.items())],
        'sub_receitas': [
            {'nome': n, 'unidades_base': q}
            for n, q in sorted(sub_receitas.items())],
        'itens_ok': itens_ok,
        'avisos': avisos,
        'detalhes': detalhes,
        'considerar_estoque': considerar_estoque,
    }
