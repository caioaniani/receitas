from math import ceil

from app.extensions import db
from app.models import MateriaPrima, Receita
from app.services.custos import calcular_custos_receitas


def aprovar_plano_do_dia(data_alvo, user_id, horizonte_dias=7, janela_semanas=6):
    """Aprova a coluna de UM dia do cronograma -> cria um PlanejamentoProducao
    (origem='cronograma', status='aprovado') desse dia, pronto pra descer pro
    padeiro. Itens = receitas com qtd>0 naquele dia; qtd_alvo = unidades,
    multiplicador = ceil(unidades / rendimento). Re-aprovar o mesmo dia
    substitui o plano-cronograma anterior. Retorna o plano (ou None se nada
    a produzir naquele dia).
    """
    from app.models import PlanejamentoItem, PlanejamentoProducao
    from app.services.previsao_producao import cronograma_producao

    crono = cronograma_producao(horizonte_dias=horizonte_dias,
                                janela_semanas=janela_semanas)
    iso = data_alvo.isoformat()
    itens_dia = []
    for rec in crono['receitas']:
        for c in rec['por_dia']:
            if c['data'] == iso and c['qtd'] > 0:
                itens_dia.append((rec['receita_id'], c['qtd']))
    if not itens_dia:
        return None

    # Re-aprovar o mesmo dia substitui o plano-cronograma anterior.
    antigo = (PlanejamentoProducao.query
              .filter_by(data=data_alvo, origem='cronograma').first())
    if antigo is not None:
        db.session.delete(antigo)
        db.session.flush()

    plano = PlanejamentoProducao(
        data=data_alvo, origem='cronograma', status='aprovado',
        nome='Cronograma %s' % data_alvo.strftime('%d/%m'),
        criado_por=user_id)
    db.session.add(plano)
    db.session.flush()

    receitas = {r.id: r for r in Receita.query.all()}
    for rid, qtd in itens_dia:
        rec = receitas.get(rid)
        rend = int(rec.rendimento_qtd) if rec and rec.rendimento_qtd else 1
        db.session.add(PlanejamentoItem(
            planejamento_id=plano.id, receita_id=rid,
            multiplicador=max(1, ceil(qtd / rend)), qtd_alvo=qtd))
    db.session.commit()
    return plano


def massa_receita_base(receita):
    """Massa (g) de UMA fornada-base da receita = soma de TODOS os ingredientes
    (a 'receita final', nao so a farinha). Mesma conta de custos.py:
    peso_base x sum_pct/100 (ingredientes em % do padeiro: farinha, agua, sal...)
    + qtd_direto (mp_direto em gramas).

    Add-ins de montagem (sub-receita 'receita' e 'mp_un' tipo baton) NAO entram
    — nao vao na amassadeira; sao agregados depois. Receitas de pao (as que
    usam a amassadeira) sao 100% percentuais, entao a conta cobre o caso real.
    """
    if not receita:
        return 0.0
    sum_pct = 0.0
    qtd_direto = 0.0
    for ing in receita.ingredientes:
        tipo = ing.tipo or 'mp'
        if tipo == 'mp_direto':
            qtd_direto += ing.porcentagem or 0
        elif tipo not in ('receita', 'mp_un'):
            sum_pct += ing.porcentagem or 0
    return (receita.peso_base or 0) * sum_pct / 100 + qtd_direto


def fornadas_amassadeira(receita, multiplicador):
    """Quantas BATIDAS da amassadeira o plano representa pra essa receita.

    A amassadeira e limitada pela MASSA final (ex: 50kg/50L), nao pela farinha.
    massa_total = massa_receita_base(receita) x multiplicador. fornadas =
    ceil(massa_total / capacidade) = numero de vezes que se carrega a
    amassadeira (a ultima batida pode ser parcial; por isso o consumo de MP
    segue a massa REAL, nao as batidas cheias). Capacidade 0 = a receita NAO
    passa pela amassadeira -> retorna None (o plano mostra unidades).
    """
    mult = int(multiplicador or 0)
    if not receita or mult <= 0:
        return None
    cap = int(getattr(receita, 'capacidade_amassadeira_g', 0) or 0)
    if cap <= 0:
        return None
    massa_total = massa_receita_base(receita) * mult
    if massa_total <= 0:
        return None
    return max(1, ceil(massa_total / cap))


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
