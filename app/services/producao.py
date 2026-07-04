from math import ceil

from app.extensions import db
from app.models import MateriaPrima, Receita
from app.services.custos import calcular_custos_receitas
from app.services.massa_base import rendimento_massa_crua


def _sync_itens_do_cronograma(plano, data_alvo, horizonte_dias, janela_semanas,
                              inicio_offset_dias, equilibrar):
    """(Re)constroi os itens de `plano` a partir do cronograma do dia (COM as
    edicoes manuais do grid aplicadas — overrides). Preserva o que ja foi
    produzido: nunca baixa qtd_alvo abaixo de produzido_qtd e NAO remove item
    que ja teve producao (trava no produzido). Nao commita. Retorna o nº de
    receitas-alvo com qtd>0 no dia.

    `inicio_offset_dias`/horizonte/janela/equilibrar TEM que ser os mesmos do
    cronograma exibido — senao as quantidades nao batem com o que esta na tela
    (a distribuicao por dia muda com a janela)."""
    from app.models import PlanejamentoItem
    from app.services.previsao_producao import cronograma_producao

    crono = cronograma_producao(horizonte_dias=horizonte_dias,
                                janela_semanas=janela_semanas,
                                inicio_offset_dias=inicio_offset_dias,
                                equilibrar=equilibrar)
    iso = data_alvo.isoformat()
    alvo = {}  # receita_id -> unidades no dia
    for rec in crono['receitas']:
        for c in rec['por_dia']:
            if c['data'] == iso and c['qtd'] > 0:
                alvo[rec['receita_id']] = c['qtd']

    receitas = {r.id: r for r in Receita.query.all()}
    existentes = {it.receita_id: it for it in plano.itens}

    for rid, qtd in alvo.items():
        rec = receitas.get(rid)
        # rendimento de massa CRUA (peso_unitario), sem perda — bate com a cascata
        rend = rendimento_massa_crua(rec)
        it = existentes.get(rid)
        if it is None:
            db.session.add(PlanejamentoItem(
                planejamento_id=plano.id, receita_id=rid,
                multiplicador=max(1, ceil(qtd / rend)), qtd_alvo=qtd))
        else:
            # A parcela EXTRA (reagendada da auditoria) SOMA ao alvo do grid —
            # re-enviar o plano nao pode apagar o que o admin mandou a mao.
            extra = int(it.qtd_extra or 0)
            it.qtd_alvo = max(qtd + extra, int(it.produzido_qtd or 0))
            it.multiplicador = max(1, ceil(it.qtd_alvo / rend))

    # Receitas que sairam do cronograma: remove, EXCETO as que ja produziram
    # (estoque/MP reais ja mexeram) ou que tem parcela EXTRA (reagendada) —
    # essas travam no maior entre produzido e extra.
    for rid, it in existentes.items():
        if rid in alvo:
            continue
        piso = max(int(it.produzido_qtd or 0), int(it.qtd_extra or 0))
        if piso > 0:
            it.qtd_alvo = piso
            rec = receitas.get(rid)
            rend = rendimento_massa_crua(rec) if rec else 1.0
            it.multiplicador = max(1, ceil(it.qtd_alvo / rend))
        else:
            db.session.delete(it)
    return len(alvo)


def _obter_ou_criar_plano(data_alvo, user_id):
    from app.models import PlanejamentoProducao
    plano = (PlanejamentoProducao.query
             .filter_by(data=data_alvo, origem='cronograma').first())
    if plano is None:
        plano = PlanejamentoProducao(
            data=data_alvo, origem='cronograma', status='aprovado',
            nome='Cronograma %s' % data_alvo.strftime('%d/%m'),
            criado_por=user_id, enviado_ao_padeiro=False)  # rascunho até "enviar"
        db.session.add(plano)
        db.session.flush()
    return plano


class PlanoJaEnviadoError(Exception):
    """Aprovar recusado: o dia ja foi ENVIADO ao padeiro.

    Garantia do dono (04/07/2026): ordem enviada NUNCA muda por caminho
    implicito (aba desatualizada com "so aprovar", POST repetido, limpar
    edicoes manuais). O UNICO caminho que altera uma ordem enviada e o
    "atualizar producao" explicito (enviar_plano_do_dia)."""


def aprovar_plano_do_dia(data_alvo, user_id, horizonte_dias=7, janela_semanas=6,
                         inicio_offset_dias=0, equilibrar=False):
    """Aprova a coluna de UM dia do cronograma -> cria/atualiza o
    PlanejamentoProducao (origem='cronograma') desse dia como RASCUNHO
    (enviado_ao_padeiro=False), pronto pra revisar e enviar. Reconstroi os itens
    a partir do grid atual (com overrides), preservando o que ja foi produzido.
    Retorna o plano (ou None se nada a produzir naquele dia).

    Dia ja ENVIADO -> PlanoJaEnviadoError, sem tocar no plano: re-aprovar
    reconstruiria os itens da ordem que o padeiro ja esta executando. Pra
    aplicar o grid num dia enviado, use enviar_plano_do_dia ("atualizar
    producao"), que e o gesto explicito."""
    from app.models import PlanejamentoProducao

    existente = (PlanejamentoProducao.query
                 .filter_by(data=data_alvo, origem='cronograma').first())
    if existente is not None and existente.enviado_ao_padeiro is not False:
        raise PlanoJaEnviadoError(data_alvo.isoformat())
    plano = _obter_ou_criar_plano(data_alvo, user_id)
    n = _sync_itens_do_cronograma(plano, data_alvo, horizonte_dias,
                                  janela_semanas, inicio_offset_dias, equilibrar)
    if n == 0 and not plano.itens:
        db.session.delete(plano)
        db.session.commit()
        return None
    db.session.commit()
    return plano


def enviar_plano_do_dia(data_alvo, user_id=None, horizonte_dias=7,
                        janela_semanas=6, inicio_offset_dias=0,
                        equilibrar=False):
    """Empurra o cronograma ATUAL do dia (com as edicoes do grid) pro padeiro:
    (re)constroi os itens a partir do grid e marca enviado_ao_padeiro=True.

    RE-PRESSAVEL: serve tanto pro 1º envio quanto pra ATUALIZAR a producao
    depois de editar o grid (decisao do dono — a edicao do grid so chega no
    padeiro quando se aperta 'enviar' de novo). Cria a ordem se nao existir.
    Preserva o que ja foi produzido. Retorna o plano, ou None se nada a
    produzir no dia."""
    from app.models import PlanejamentoProducao

    plano = (PlanejamentoProducao.query
             .filter_by(data=data_alvo, origem='cronograma').first())
    novo = plano is None
    if novo:
        plano = _obter_ou_criar_plano(data_alvo, user_id)
    n = _sync_itens_do_cronograma(plano, data_alvo, horizonte_dias,
                                  janela_semanas, inicio_offset_dias, equilibrar)
    if n == 0 and not plano.itens:
        if novo:
            db.session.delete(plano)
        db.session.commit()
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

    rend = rendimento_massa_crua(receita)
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
    (ex: croissant almond consome croissant tradicional congelado; croissant
    consome bolas de massa para folhar). Liga por FK (`sub_receita_id`); cai
    pro nome só se a FK não foi resolvida. porcentagem = unidades da sub por
    batida; consumo = unidades x porcentagem / rendimento.

    FRAÇÃO ACUMULADA (decisão do dono 03/07/2026, caso massa para folhar):
    o congelado é inteiro mas o consumo por lote é fracionário (batida de 50
    croissants = 1,26 bola). O `round()` por lote sumia/sobrava ~meia bola
    por dia; agora floor(consumo + acumulado) baixa inteiros e o resto fica
    em `ConsumoSubFracao` pra próxima produção — exato no longo prazo.
    Consumo inteiro exato (ex: almond 1:1) nunca cria fração.
    NÃO commita. Retorna [{sub_id, baixado, falta}]."""
    from app.models import ConsumoSubFracao, Receita
    from app.services.estoque_congelados import saida_producao

    rend = rendimento_massa_crua(rec)
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
        consumo = (unidades * (ing.porcentagem or 0) / rend) if rend else 0.0
        if consumo <= 0:
            continue
        frac = ConsumoSubFracao.query.filter_by(receita_id=sub_id).first()
        if frac is None:
            frac = ConsumoSubFracao(receita_id=sub_id, fracao_pendente=0.0)
            db.session.add(frac)
            db.session.flush()
        total = consumo + (frac.fracao_pendente or 0.0)
        qtd_sub = int(total)                     # floor (total sempre >= 0)
        # round(6) segura ruído de float; nunca negativa.
        frac.fracao_pendente = max(0.0, round(total - qtd_sub, 6))
        if qtd_sub > 0:
            res = saida_producao(
                receita_id=sub_id, quantidade=qtd_sub, usuario_id=user_id,
                referencia='Consumo p/ %s (%d un; acum %.2f)'
                           % (rec.nome, unidades, frac.fracao_pendente))
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
    # Item DISPENSADO pelo admin (auditoria: "OK, não vai produzir") não pode ser
    # produzido — o admin fechou a pendência. Pra produzir, reverta a dispensa na
    # tela de auditoria. (Sem isso, produzir creditava estoque de algo que o admin
    # já tinha dado baixa na projeção.)
    if item.dispensada_em is not None:
        return {'ok': False, 'erro': 'Item dispensado pelo admin — reverta a '
                'dispensa na auditoria pra poder produzir.'}
    rec = item.receita
    if rec is None:
        return {'ok': False, 'erro': 'Receita do item não encontrada.'}

    # 1) credita o produto pronto (nao commita — controlamos a transacao).
    entrada_producao(receita_id=rec.id, quantidade=unidades, usuario_id=user_id,
                     referencia='Produção (cronograma) %s' % rec.nome)

    # 2) baixa a MP proporcional as unidades (multiplicador fracionario =
    #    unidades/rendimento; segue o consumo REAL, nao a batida cheia).
    rend = rendimento_massa_crua(rec)
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

            mp = mps.get(ing.ingrediente_nome)
            if not mp:
                continue

            # mp_un: porcentagem = UNIDADES por batida (espelha custos.py:292
            # — 'MP cobrada por unidade'; o custo_por_kg dessas MPs guarda o
            # custo POR UNIDADE). Antes caía no ramo de %, virando 1% do
            # peso_base (bug pego pelo dono 04/07: 1 bloco de manteiga-folhar
            # por batida contava 10 g em vez de 1 un).
            em_unidades = (ing.tipo == 'mp_un')
            if em_unidades:
                qtd = (ing.porcentagem or 0) * multiplicador
            elif ing.tipo == 'mp_direto':
                qtd = (ing.porcentagem or 0) * multiplicador
            else:
                qtd = (ing.porcentagem or 0) / 100.0 * peso_base * multiplicador

            if ing.ingrediente_nome not in lista:
                lista[ing.ingrediente_nome] = {
                    'quantidade': 0,
                    'unidade': mp.unidade,
                    'custo_por_kg': mp.custo_por_kg,
                    'estoque_atual': mp.estoque_atual or 0,
                    'fornecedor': mp.fornecedor,
                    'em_unidades': em_unidades,
                }
            d = lista[ing.ingrediente_nome]
            if em_unidades and not d['em_unidades'] and (mp.peso_unidade or 0):
                # MESMA MP usada em % numa ficha e em un noutra: converte
                # a parcela unitaria pra gramas pra somar coerente.
                qtd = qtd * mp.peso_unidade
                em_unidades = False
            d['quantidade'] += qtd
            # Rastro por receita (expandir na calculadora: "usado por quem").
            org = d.setdefault('origens', {})
            org[receita.nome] = org.get(receita.nome, 0) + qtd

    for nome, dados in lista.items():
        if dados.get('em_unidades'):
            # Unidades: custo = un × custo POR UNIDADE (sem /1000).
            dados['custo_estimado'] = dados['quantidade'] * dados['custo_por_kg']
        else:
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
        if d.get('em_unidades'):
            # MP unitaria: compra so em INTEIROS (ninguem compra 0,6 pote) —
            # deficit fracionario arredonda pra CIMA. custo_por_kg = custo
            # POR UNIDADE (custos.py:292).
            from math import ceil
            comprar_g = ceil(comprar_g - 1e-9) if comprar_g > 0 else 0
            custo_compra = comprar_g * (d.get('custo_por_kg') or 0)
        else:
            custo_compra = (comprar_g / 1000.0) * (d.get('custo_por_kg') or 0)
        grupos.setdefault(forn, []).append({
            'nome': nome,
            'em_unidades': bool(d.get('em_unidades')),
            # Quem usa esta MP e quanto (maior consumidor primeiro).
            'origens': sorted((d.get('origens') or {}).items(),
                              key=lambda kv: -kv[1]),
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
