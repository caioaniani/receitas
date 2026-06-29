from math import ceil

from app.extensions import db
from app.models import MateriaPrima, Receita
from app.services.custos import calcular_custos_receitas


def aprovar_plano_do_dia(data_alvo, user_id, horizonte_dias=7, janela_semanas=6,
                         inicio_offset_dias=0, equilibrar=False):
    """Aprova a coluna de UM dia do cronograma -> cria um PlanejamentoProducao
    (origem='cronograma', status='aprovado') desse dia, pronto pra descer pro
    padeiro. Itens = receitas com qtd>0 naquele dia; qtd_alvo = unidades,
    multiplicador = ceil(unidades / rendimento). Re-aprovar o mesmo dia
    substitui o plano-cronograma anterior. Retorna o plano (ou None se nada
    a produzir naquele dia).

    `inicio_offset_dias` TEM que ser o mesmo do cronograma exibido — senao as
    quantidades aprovadas nao batem com o que esta na tela (a distribuicao por
    dia muda com a janela).
    """
    from app.models import PlanejamentoItem, PlanejamentoProducao
    from app.services.previsao_producao import cronograma_producao

    crono = cronograma_producao(horizonte_dias=horizonte_dias,
                                janela_semanas=janela_semanas,
                                inicio_offset_dias=inicio_offset_dias)
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
        criado_por=user_id, enviado_ao_padeiro=False)  # rascunho até "enviar"
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


def enviar_plano_do_dia(data_alvo):
    """2º passo: libera a ordem do dia pro padeiro (enviado_ao_padeiro=True).
    Retorna o plano enviado, ou None se não há ordem aprovada nesse dia."""
    from app.models import PlanejamentoProducao

    plano = (PlanejamentoProducao.query
             .filter_by(data=data_alvo, origem='cronograma').first())
    if plano is None:
        return None
    plano.enviado_ao_padeiro = True
    db.session.commit()
    return plano


def excluir_plano_do_dia(data_alvo):
    """Exclui a ordem de producao (origem='cronograma') de um dia — pra desfazer
    um envio errado. SALVAGUARDA (estoque tem peso especial): se algum item ja
    teve producao (produzido_qtd > 0), NAO exclui — a producao real ja creditou
    estoque e baixou MP, e apagar a ordem orfanaria esses movimentos. Retorna
    {'ok': True} ou {'ok': False, 'erro': 'nao_encontrado'|'ja_produzido', ...}.
    """
    from app.models import PlanejamentoProducao

    plano = (PlanejamentoProducao.query
             .filter_by(data=data_alvo, origem='cronograma').first())
    if plano is None:
        return {'ok': False, 'erro': 'nao_encontrado'}
    produzido = sum(int(it.produzido_qtd or 0) for it in plano.itens)
    if produzido > 0:
        return {'ok': False, 'erro': 'ja_produzido', 'produzido': produzido}
    db.session.delete(plano)   # cascade apaga os itens
    db.session.commit()
    return {'ok': True}


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


def _fmt_dur(minutos):
    """Minutos -> '30 min' / '2h' / '2,5h' / '48h'."""
    m = int(minutos or 0)
    if m >= 60:
        h = m / 60.0
        return ('%dh' % int(h)) if h == int(h) else ('%.1fh' % h).replace('.', ',')
    return '%d min' % m


def seed_etapas_categoria(categoria):
    """Cria/substitui as etapas de producao de TODAS as receitas (nao
    arquivadas) da categoria com o padrao pesquisado. Tambem preenche o
    modo_preparo (texto) SE estiver vazio, gerado das etapas. Retorna o nº de
    receitas afetadas. Idempotente (re-aplicar substitui as etapas)."""
    from app.constants import etapas_padrao_categoria
    from app.models import ReceitaEtapa

    q = Receita.query.filter(Receita.arquivada_em.is_(None))
    if categoria:
        q = q.filter(Receita.categoria == categoria)
    else:
        q = q.filter((Receita.categoria.is_(None)) | (Receita.categoria == ''))
    padrao = etapas_padrao_categoria(categoria)
    n = 0
    for r in q.all():
        ReceitaEtapa.query.filter_by(receita_id=r.id).delete()
        for i, (nome, dur, equip, ativa) in enumerate(padrao):
            db.session.add(ReceitaEtapa(receita_id=r.id, ordem=i, nome=nome,
                                        duracao_min=dur, equipamento=equip,
                                        ativa=ativa))
        if not (r.modo_preparo or '').strip():
            r.modo_preparo = '\n\n'.join(
                '%s — %s' % (nome, _fmt_dur(dur))
                for nome, dur, equip, ativa in padrao)
        n += 1
    db.session.commit()
    return n


def mise_en_place(receita, unidades):
    """Receita ESCALADA pra produzir `unidades`: cada ingrediente com a
    quantidade ja ajustada (farinha, agua, sal...) pro padeiro pesar, mais o
    modo de preparo em etapas. mult = unidades/rendimento (fracionario)."""
    from app.utils import dividir_etapas_preparo

    rend = float(receita.rendimento_qtd) if receita.rendimento_qtd else 1.0
    mult = (unidades / rend) if rend > 0 else 0.0
    peso_base = receita.peso_base or 0

    ingredientes = []
    for ing in receita.ingredientes:
        tipo = ing.tipo or 'mp'
        pct = ing.porcentagem or 0
        if tipo == 'mp_direto':
            qtd, unidade, mostra_pct = pct * mult, 'g', None
        elif tipo == 'mp_un':
            qtd, unidade, mostra_pct = pct * mult, 'un', None
        elif tipo == 'receita':
            qtd, unidade, mostra_pct = pct * mult, 'un', None
        else:  # 'mp' percentual: farinha (100%), agua, sal, fermento...
            qtd = pct / 100.0 * peso_base * mult
            unidade, mostra_pct = 'g', pct
        ingredientes.append({
            'nome': ing.ingrediente_nome,
            'qtd': round(qtd, 1),
            'unidade': unidade,
            'pct': mostra_pct,
        })

    # Processo estruturado (etapas cadastradas) pro fluxograma do padeiro:
    # nome, duracao formatada, equipamento e se e ativa (mao-de-obra) ou
    # passiva (fermentacao/descanso). Vazio quando a receita ainda nao tem
    # etapas cadastradas — o card cai no modo_preparo em texto.
    processo = [{
        'nome': e.nome,
        'duracao': _fmt_dur(e.duracao_min),
        'duracao_min': e.duracao_min,
        'equipamento': e.equipamento,
        'ativa': e.ativa,
    } for e in receita.etapas]

    return {
        'receita_id': receita.id,
        'nome': receita.nome,
        'unidades': int(unidades),
        'farinha_g': round(peso_base * mult, 1),
        'ingredientes': ingredientes,
        'etapas': dividir_etapas_preparo(receita.modo_preparo),
        'processo': processo,
    }


def consumir_subreceitas_prontas(rec, unidades, user_id):
    """Baixa do congelado as SUB-RECEITAS prontas consumidas ao produzir `rec`
    (ex: croissant almond consome croissant tradicional congelado). Liga por FK
    (`sub_receita_id`); cai pro nome só se a FK não foi resolvida. porcentagem =
    unidades da sub por batida; consumo = unidades x porcentagem / rendimento.
    NÃO commita. Retorna [{sub_id, baixado, falta}]."""
    from app.models import Receita
    from app.services.estoque_congelados import saida_producao

    rend = float(rec.rendimento_qtd) if rec and rec.rendimento_qtd else 1.0
    out = []
    for ing in (rec.ingredientes if rec else []):
        if (ing.tipo or 'mp') != 'receita':
            continue
        sub_id = ing.sub_receita_id
        if sub_id is None:
            sub = (Receita.query
                   .filter(Receita.nome.ilike((ing.ingrediente_nome or '').strip()))
                   .first())
            sub_id = sub.id if sub else None
        if sub_id is None:
            continue            # órfão (sem cadastro): não dá pra baixar
        qtd_sub = int(round(unidades * (ing.porcentagem or 0) / rend)) if rend else 0
        if qtd_sub > 0:
            res = saida_producao(
                receita_id=sub_id, quantidade=qtd_sub, usuario_id=user_id,
                referencia='Consumo p/ %s (%d un)' % (rec.nome, unidades))
            out.append({'sub_id': sub_id, **res})
    return out


def produzir_item_plano(item_id, unidades, user_id):
    """OPCAO B: o padeiro produz `unidades` de um item do plano aprovado.
    Numa unica transacao: (1) credita o produto pronto na industria
    (entrada_producao), (2) DESCONTA a MP da ficha tecnica proporcional as
    unidades (consumo real, sem arredondar pra batida cheia) e (3) avanca o
    produzido_qtd do item. Retorna {'ok': True, 'produzido': N} ou
    {'ok': False, 'erro': ...}.
    """
    from app.models import MovimentacaoEstoque, PlanejamentoItem
    from app.services.estoque_congelados import entrada_producao

    try:
        unidades = int(unidades or 0)
    except (TypeError, ValueError):
        unidades = 0
    if unidades <= 0:
        return {'ok': False, 'erro': 'Quantidade inválida.'}
    item = PlanejamentoItem.query.get(item_id)
    if item is None:
        return {'ok': False, 'erro': 'Item do plano não encontrado.'}
    rec = item.receita
    if rec is None:
        return {'ok': False, 'erro': 'Receita do item não encontrada.'}

    # 1) credita o produto pronto (nao commita — controlamos a transacao).
    entrada_producao(receita_id=rec.id, quantidade=unidades, usuario_id=user_id,
                     referencia='Produção (cronograma) %s' % rec.nome)

    # 2) baixa a MP proporcional as unidades (multiplicador fracionario =
    #    unidades/rendimento; segue o consumo REAL, nao a batida cheia).
    rend = float(rec.rendimento_qtd) if rec.rendimento_qtd else 1.0
    mult = unidades / rend if rend > 0 else 0
    lista = consolidar_lista_compras([{'receita_id': rec.id,
                                       'multiplicador': mult}])
    mps = {mp.nome: mp for mp in MateriaPrima.query.all()}
    for nome, dados in lista.items():
        mp = mps.get(nome)
        if not mp:
            continue
        qtd = dados['quantidade']
        db.session.add(MovimentacaoEstoque(
            materia_prima_id=mp.id, tipo='saida', quantidade=qtd,
            referencia='Produção %s (%d un)' % (rec.nome, unidades),
            usuario_id=user_id))
        mp.estoque_atual = max(0, (mp.estoque_atual or 0) - qtd)

    # 2b) consome SUB-RECEITAS prontas do congelado (ex: croissant almond usa
    #     croissant tradicional congelado).
    consumir_subreceitas_prontas(rec, unidades, user_id)

    # 3) avanca o produzido do item.
    item.produzido_qtd = int(item.produzido_qtd or 0) + unidades
    db.session.commit()
    return {'ok': True, 'produzido': item.produzido_qtd}


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
