import logging
from math import ceil

from app.extensions import db
from app.models import MateriaPrima, Receita
from app.services.custos import calcular_custos_receitas
from app.services.massa_base import rendimento_massa_crua
from app.utils import SUB_RECEITA_TIPOS, unidades_subreceita

logger = logging.getLogger(__name__)


def _sync_itens_do_cronograma(plano, data_alvo, horizonte_dias, janela_semanas,
                              inicio_offset_dias, equilibrar,
                              motor='pedidos', automatico=False, crono=None):
    """(Re)constroi os itens de `plano` a partir do cronograma do dia (COM as
    edicoes manuais do grid aplicadas — overrides). Preserva o que ja foi
    produzido: nunca baixa qtd_alvo abaixo de produzido_qtd e NAO remove item
    que ja teve producao (trava no produzido). Nao commita. Retorna
    `(n_alvos, congelados)`.

    `inicio_offset_dias`/horizonte/janela/equilibrar/motor TEM que ser os
    mesmos do cronograma exibido — senao as quantidades nao batem com o que
    esta na tela (a distribuicao por dia muda com a janela e com o motor de
    previsao). `crono` pronto pula o recalculo (o job escreve varios dias da
    MESMA rodada — insumo e pai precisam sair do MESMO grid).

    `automatico=True` liga a TRAVA POR INSUMO (dono 20/08/2026): item cuja
    ficha tem sub-receita de lead (croissant/pain pela Massa para folhar;
    pao frances e sourdoughs pelo Levain) NAO muda mais quando falta
    `ant_insumo` dias ou menos pro dia alvo — a massa/levain dele ja foi
    batida com o numero antigo, entao mudar agora so gera ordem impossivel.
    Demanda que sobe depois disso NAO entra (decisao do dono) e volta em
    `congelados` pra tela avisar. Gesto humano (automatico=False) ignora a
    trava."""
    from app.models import PlanejamentoItem
    from app.services.previsao_producao import (
        cronograma_producao,
    )

    if crono is None:
        crono = cronograma_producao(horizonte_dias=horizonte_dias,
                                    janela_semanas=janela_semanas,
                                    inicio_offset_dias=inicio_offset_dias,
                                    equilibrar=equilibrar, motor=motor)
    iso = data_alvo.isoformat()
    alvo = {}  # receita_id -> unidades no dia
    for rec in crono['receitas']:
        # Receita de RETORNO nunca entra na ordem (dono, 13/07/2026): o
        # padeiro não produz devolução. Defesa em profundidade — o balanço
        # já zera a produção dela, mas um override legado re-injetaria a
        # linha; item já existente sem produção some no loop de remoção.
        if rec.get('retorno'):
            continue
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
                         inicio_offset_dias=0, equilibrar=False,
                         motor='pedidos'):
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
                                  janela_semanas, inicio_offset_dias,
                                  equilibrar, motor=motor)
    if n == 0 and not plano.itens:
        db.session.delete(plano)
        db.session.commit()
        return None
    db.session.commit()
    return plano


def enviar_plano_do_dia(data_alvo, user_id=None, horizonte_dias=7,
                        janela_semanas=6, inicio_offset_dias=0,
                        equilibrar=False, motor='pedidos'):
    """Empurra o cronograma ATUAL do dia (com as edicoes do grid) pro padeiro:
    (re)constroi os itens a partir do grid e marca enviado_ao_padeiro=True.

    RE-PRESSAVEL: serve tanto pro 1º envio quanto pra ATUALIZAR a producao
    depois de editar o grid (decisao do dono — a edicao do grid so chega no
    padeiro quando se aperta 'enviar' de novo). Cria a ordem se nao existir.
    Preserva o que ja foi produzido. Retorna o plano, ou None se nada a
    produzir no dia.

    TRAVA DO DIA CORRENTE (dono 20/08/2026, caso "o padeiro ia fazer 300 de
    pao frances e virou 400" — o 🔄 automatico das 19:05 reescreveu a ordem
    com ele ja em producao): "Na data de hoje, nunca que deveriamos ter
    trocado ou feito alguma mudanca no que o padeiro esta produzindo hoje.
    Qualquer mudanca deveria ter sido feita ontem." Entao caminho
    AUTOMATICO (`user_id is None`) NAO reescreve ordem JA ENVIADA de HOJE
    ou do passado — devolve o plano intacto. Gesto HUMANO (🔄 na tela, com
    user_id) segue livre: o dono pode corrigir a ordem do dia se quiser,
    conscientemente. Criar ordem que ainda NAO existe continua permitido
    (dia sem ordem nenhuma e pior que ordem tardia)."""
    from app.models import PlanejamentoProducao
    from app.utils import hoje as _hoje

    plano = (PlanejamentoProducao.query
             .filter_by(data=data_alvo, origem='cronograma').first())
    if (user_id is None and plano is not None
            and plano.enviado_ao_padeiro and data_alvo <= _hoje()):
        logger.info('enviar_plano_do_dia: ordem de %s JA ENVIADA e o dia ja '
                    'chegou — caminho automatico nao reescreve (regra do '
                    'dono 20/08/2026)', data_alvo.isoformat())
        return plano
    novo = plano is None
    if novo:
        plano = _obter_ou_criar_plano(data_alvo, user_id)
    n = _sync_itens_do_cronograma(plano, data_alvo, horizonte_dias,
                                  janela_semanas, inicio_offset_dias,
                                  equilibrar, motor=motor)
    if n == 0 and not plano.itens:
        if novo:
            db.session.delete(plano)
        else:
            # Plano existente que ficou vazio: libera a MP reservada.
            sincronizar_pre_baixa_mp(plano, user_id)
        db.session.commit()
        return None
    plano.enviado_ao_padeiro = True
    # Enviar RESERVA a MP da falta (pré-baixa) — o gesto explícito inicia o
    # regime (criar=True); re-enviar só ajusta o delta.
    sincronizar_pre_baixa_mp(plano, user_id, criar=True)
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
    estornar_pre_baixa_plano(plano)   # devolve a MP reservada antes de sumir
    db.session.delete(plano)   # cascade apaga os itens
    db.session.commit()
    return {'ok': True}


# ── Pré-baixa de MP da ordem enviada (pedido do dono 07/07/2026) ─────────
# Enviar o plano ao padeiro RESERVA a MP da falta (baixa provisória);
# confirmar a produção converte em baixa real e libera a reserva. Tudo
# passa pelo reconciliador idempotente abaixo — os caminhos que mudam a
# falta (enviar, produzir, dispensar/reverter, reagendar, excluir) só o
# chamam de novo.

_PRE_BAIXA_EPS = 1e-6   # delta menor que isso = ruído de float, não gera mov


def _explosao_mp_falta(plano):
    """{mp_id: quantidade} de MP da FALTA do plano (alvo − produzido dos
    itens não dispensados). MESMO motor de explosão da baixa real e da
    calculadora de compras (`consolidar_lista_compras`, que trata mp_un/
    mp_direto/percentual) e MESMO rendimento do produzir
    (`rendimento_massa_crua`) — assim a pré-baixa casa exata com a baixa
    real na confirmação."""
    from app.models import PlanejamentoItem
    itens = PlanejamentoItem.query.filter_by(planejamento_id=plano.id).all()
    itens_motor = []
    for it in itens:
        if it.dispensada_em is not None:
            continue
        falta = max(0, int(it.qtd_alvo or 0) - int(it.produzido_qtd or 0))
        if falta <= 0:
            continue
        rend = rendimento_massa_crua(it.receita)
        if not rend or rend <= 0:
            continue
        itens_motor.append({'receita_id': it.receita_id,
                            'multiplicador': falta / rend})
    if not itens_motor:
        return {}
    lista = consolidar_lista_compras(itens_motor)
    ids = {mp.nome: mp.id for mp in MateriaPrima.query.all()}
    out = {}
    for nome, dados in lista.items():
        mp_id = ids.get(nome)
        if mp_id and dados['quantidade'] > _PRE_BAIXA_EPS:
            out[mp_id] = out.get(mp_id, 0.0) + dados['quantidade']
    return out


def sincronizar_pre_baixa_mp(plano, user_id=None, criar=False):
    """Reconcilia a PRÉ-BAIXA de MP do plano com o estado atual.

    Alvo da reconciliação = explosão da falta se `enviado_ao_padeiro`;
    vazio se rascunho. Aplica só o DELTA contra as linhas `PreBaixaMP`,
    como `MovimentacaoEstoque` 'saida' (referência "Pré-baixa produção…")
    ou 'entrada' ("Estorno pré-baixa produção…") + ajuste do denormalizado
    `estoque_atual`. Idempotente: rodar de novo sem mudança não gera
    movimento.

    `criar=False`: plano sem NENHUMA linha fica FORA do regime (ordem
    enviada antes da feature — não se pré-baixa retroativo em cima de
    ordem antiga). Só o enviar/reagendar (gestos explícitos) passam
    `criar=True`. Linhas zeradas ficam como marcador de regime; só somem
    com o plano (`estornar_pre_baixa_plano`). NÃO commita."""
    from app.models import MovimentacaoEstoque, PreBaixaMP

    db.session.flush()   # deletes/updates pendentes visíveis nas queries
    linhas = {pb.materia_prima_id: pb for pb in
              PreBaixaMP.query.filter_by(plano_id=plano.id).all()}
    if not linhas and not criar:
        return {'em_regime': False, 'movs': 0}

    desejado = ({} if not plano.enviado_ao_padeiro
                else _explosao_mp_falta(plano))
    mp_ids = set(desejado) | set(linhas)
    if not mp_ids:
        return {'em_regime': bool(linhas), 'movs': 0}
    rotulo = plano.data.strftime('%d/%m') if plano.data else '#%s' % plano.id
    mps = {m.id: m for m in
           MateriaPrima.query.filter(MateriaPrima.id.in_(mp_ids)).all()}
    movs = 0
    for mp_id in sorted(mp_ids):
        mp = mps.get(mp_id)
        if mp is None:
            continue
        alvo = float(desejado.get(mp_id, 0.0))
        linha = linhas.get(mp_id)
        atual = float(linha.quantidade or 0.0) if linha else 0.0
        delta = alvo - atual
        if abs(delta) <= _PRE_BAIXA_EPS:
            continue
        if delta > 0:
            db.session.add(MovimentacaoEstoque(
                materia_prima_id=mp_id, tipo='saida', quantidade=delta,
                referencia='Pré-baixa produção %s' % rotulo,
                usuario_id=user_id))
            mp.estoque_atual = max(0, (mp.estoque_atual or 0) - delta)
        else:
            db.session.add(MovimentacaoEstoque(
                materia_prima_id=mp_id, tipo='entrada', quantidade=-delta,
                referencia='Estorno pré-baixa produção %s' % rotulo,
                usuario_id=user_id))
            mp.estoque_atual = (mp.estoque_atual or 0) - delta
        if linha is None:
            linha = PreBaixaMP(plano_id=plano.id, materia_prima_id=mp_id,
                               quantidade=0.0)
            db.session.add(linha)
        linha.quantidade = alvo
        movs += 1
    return {'em_regime': True, 'movs': movs}


def estornar_pre_baixa_plano(plano, user_id=None):
    """Estorna TODA a pré-baixa pendente do plano e apaga as linhas — usado
    quando a ordem é EXCLUÍDA (o marcador de regime vai junto). NÃO
    commita. Retorna o nº de linhas removidas."""
    from app.models import MovimentacaoEstoque, PreBaixaMP

    linhas = PreBaixaMP.query.filter_by(plano_id=plano.id).all()
    rotulo = plano.data.strftime('%d/%m') if plano.data else '#%s' % plano.id
    for pb in linhas:
        q = float(pb.quantidade or 0.0)
        if q > _PRE_BAIXA_EPS:
            db.session.add(MovimentacaoEstoque(
                materia_prima_id=pb.materia_prima_id, tipo='entrada',
                quantidade=q,
                referencia='Estorno pré-baixa produção %s (ordem excluída)'
                           % rotulo,
                usuario_id=user_id))
            mp = pb.materia_prima
            if mp is not None:
                mp.estoque_atual = (mp.estoque_atual or 0) + q
        db.session.delete(pb)
    return len(linhas)


def massa_receita_base(receita):
    """Massa (g) de UMA fornada-base da receita = soma de TODOS os ingredientes
    (a 'receita final', nao so a farinha). Mesma conta de custos.py:
    peso_base x sum_pct/100 (ingredientes em % do padeiro: farinha, agua, sal...)
    + qtd_direto (mp_direto em gramas).

    Add-ins de montagem (sub-receita 'receita'/'sub_pct' e 'mp_un' tipo baton)
    NAO entram — nao vao na amassadeira; sao agregados depois. Receitas de pao
    (as que usam a amassadeira) sao 100% percentuais, entao a conta cobre o
    caso real.
    """
    if not receita:
        return 0.0
    sum_pct = 0.0
    qtd_direto = 0.0
    for ing in receita.ingredientes:
        tipo = ing.tipo or 'mp'
        if tipo == 'mp_direto':
            qtd_direto += ing.porcentagem or 0
        elif tipo not in SUB_RECEITA_TIPOS and tipo != 'mp_un':
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

    from app.services.massa_base import _sub_amassadeira

    ingredientes = []
    for ing in receita.ingredientes:
        tipo = ing.tipo or 'mp'
        pct = ing.porcentagem or 0
        nome = ing.ingrediente_nome
        if tipo == 'mp_direto':
            qtd, unidade, mostra_pct = pct * mult, 'g', None
        elif tipo == 'mp_un':
            qtd, unidade, mostra_pct = pct * mult, 'un', None
        elif _sub_amassadeira(ing):
            # Sub-receita DE AMASSADEIRA (Levain (pé)): o padeiro pesa em
            # GRAMAS (qtd em unidades de 1 g × peso_unitario), não "200 un".
            nome = ing.sub_receita.nome
            qtd = (unidades_subreceita(tipo, pct, peso_base)
                   * (ing.sub_receita.peso_unitario or 0) * mult)
            unidade, mostra_pct = 'g', None
        elif tipo in SUB_RECEITA_TIPOS:
            qtd = unidades_subreceita(tipo, pct, peso_base) * mult
            unidade, mostra_pct = 'un', None
        else:  # 'mp' percentual: farinha (100%), agua, sal, fermento...
            qtd = pct / 100.0 * peso_base * mult
            unidade, mostra_pct = 'g', pct
        ingredientes.append({
            'nome': nome,
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
        'descricao': e.descricao,
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
        if (ing.tipo or 'mp') not in SUB_RECEITA_TIPOS:
            continue
        sub_id = ing.sub_receita_id
        if sub_id is None:
            sub = (Receita.query
                   .filter(Receita.nome.ilike((ing.ingrediente_nome or '').strip()))
                   .first())
            sub_id = sub.id if sub else None
        if sub_id is None:
            continue            # órfão (sem cadastro): não dá pra baixar
        consumo = ((unidades * unidades_subreceita(
            ing.tipo, ing.porcentagem, rec.peso_base) / rend)
            if rend else 0.0)
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


def consumir_ficha(rec, unidades, user_id, referencia_mp):
    """Consome da ficha técnica o que PRODUZIR `unidades` da receita consome:
    MP proporcional (multiplicador fracionário = unidades/rendimento — consumo
    REAL, não batida cheia) + sub-receitas prontas do congelado (fração
    acumulada). NÃO credita estoque, NÃO commita — quem chama controla a
    transação.

    Extraído de `produzir_item_plano` em 13/08/2026 pra ser compartilhado com
    a FORNADA QUEIMADA da tela de perdas do padeiro (consome a ficha como se
    tivesse produzido, sem creditar — o produto queimou).

    Retorna o resultado de `consumir_subreceitas_prontas` (lista com
    baixado/falta por sub) — a tela de perdas avisa quando o congelado não
    cobriu a sub; o produzir ignora (comportamento de sempre)."""
    from app.models import MateriaPrima, MovimentacaoEstoque

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
            referencia=referencia_mp, usuario_id=user_id))
        mp.estoque_atual = max(0, (mp.estoque_atual or 0) - qtd)

    return consumir_subreceitas_prontas(rec, unidades, user_id)


def produzir_item_plano(item_id, unidades, user_id, encerrar=False):
    """OPCAO B: o padeiro produz `unidades` de um item do plano aprovado.
    Numa unica transacao: (1) credita o produto pronto na industria
    (entrada_producao), (2) DESCONTA a MP da ficha tecnica proporcional as
    unidades (consumo real, sem arredondar pra batida cheia) e (3) avanca o
    produzido_qtd do item. Retorna {'ok': True, 'produzido': N} ou
    {'ok': False, 'erro': ...}.

    `encerrar=True` (17/07/2026, decisao do dono): o padeiro produziu MENOS
    que o alvo e da o item por FEITO — marca `falta_encerrada_em`, o item
    some das telas dele e a diferenca fica so na auditoria (admin decide:
    OK/dispensar ou reagendar de volta). So marca se ainda restar falta;
    estoque credita apenas o produzido de verdade.
    """
    from app.models import PlanejamentoItem
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

    # 2 + 2b) baixa MP + sub-receitas prontas da ficha (helper compartilhado
    #         com a fornada queimada da tela de perdas — 13/08/2026).
    consumir_ficha(rec, unidades, user_id,
                   referencia_mp='Produção %s (%d un)' % (rec.nome, unidades))

    # 3) avanca o produzido do item.
    item.produzido_qtd = int(item.produzido_qtd or 0) + unidades

    # 3b) padeiro deu por encerrado com falta restante: marca — some das
    #     telas dele, fica na auditoria. Completou o alvo? Nada a marcar.
    falta_restante = max(0, int(item.qtd_alvo or 0) - item.produzido_qtd)
    encerrado = False
    if encerrar and falta_restante > 0:
        from app.utils import agora
        item.falta_encerrada_em = agora()
        encerrado = True

    # 4) a parte confirmada virou baixa REAL — o reconciliador libera a
    #    pré-baixa correspondente (plano fora do regime = no-op).
    sincronizar_pre_baixa_mp(item.planejamento, user_id)
    db.session.commit()
    return {'ok': True, 'produzido': item.produzido_qtd,
            'encerrado': encerrado, 'falta_restante': falta_restante}


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
            if ing.tipo in SUB_RECEITA_TIPOS:
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


def mp_necessaria_do_dia(data_alvo, horizonte_dias=7, janela_semanas=6,
                         inicio_offset_dias=0, equilibrar=False,
                         motor='pedidos'):
    """Matéria-prima necessária pra produzir o GRID de um dia do cronograma
    (com as edições/overrides aplicados), comparada com o estoque atual de MP.
    Responde "tenho insumo pra essa produção?" ANTES de enviar ao padeiro.

    MESMO motor de explosão da pré-baixa e da baixa real
    (`consolidar_lista_compras` + `rendimento_massa_crua`, multiplicador
    fracionário = qtd/rendimento) — os números batem com o que a confirmação
    do padeiro vai baixar de verdade. Read-only: não mexe em estoque nem
    cria movimento.

    Dia já ENVIADO no regime de pré-baixa: a reserva DESTE dia já saiu do
    `estoque_atual` — sem creditá-la de volta, o insumo que está na
    prateleira reservado pra esta mesma produção apareceria como "falta"
    (falso alarme exatamente no fluxo "🔄 atualizar produção"). Reservas de
    OUTROS dias continuam debitadas: estão comprometidas em outra ordem.

    Retorna {'data': iso, 'receitas_n': n, 'itens': [{nome, necessario,
    unidade ('un'|'g'), estoque, falta, reservado, fornecedor}],
    'faltam_n': n, 'reservado_total_n': n, 'sem_cadastro': [nomes]} com os
    itens em falta primeiro; None se a data está fora do grid.
    `sem_cadastro` lista ingrediente de ficha sem MP correspondente no Banco
    de MPs — fica FORA da conta (mesma semântica da calculadora de compras),
    e o aviso existe pra falta não passar em silêncio."""
    from app.models import PlanejamentoProducao, PreBaixaMP
    from app.services.previsao_producao import cronograma_producao

    crono = cronograma_producao(horizonte_dias=horizonte_dias,
                                janela_semanas=janela_semanas,
                                inicio_offset_dias=inicio_offset_dias,
                                equilibrar=equilibrar, motor=motor)
    iso = data_alvo.isoformat()
    if iso not in {d['data'] for d in crono['dias']}:
        return None
    receitas = {r.id: r for r in Receita.query.all()}
    mps_nome = {mp.id: mp.nome for mp in MateriaPrima.query.all()}
    nomes_mp = set(mps_nome.values())
    itens_motor = []
    sem_cadastro = set()
    for rec in crono['receitas']:
        qtd = next((c['qtd'] for c in rec['por_dia'] if c['data'] == iso), 0)
        if not qtd or qtd <= 0:
            continue
        r = receitas.get(rec['receita_id'])
        rend = rendimento_massa_crua(r) if r else 0
        if not rend or rend <= 0:
            continue
        itens_motor.append({'receita_id': rec['receita_id'],
                            'multiplicador': qtd / rend})
        for ing in r.ingredientes:
            if ing.tipo not in SUB_RECEITA_TIPOS and ing.ingrediente_nome not in nomes_mp:
                sem_cadastro.add(ing.ingrediente_nome)
    lista = consolidar_lista_compras(itens_motor) if itens_motor else {}

    # Reserva (pré-baixa) da ordem enviada DESTE dia, por nome de MP.
    reserva = {}
    plano = (PlanejamentoProducao.query
             .filter_by(data=data_alvo, origem='cronograma').first())
    if plano is not None:
        for lin in PreBaixaMP.query.filter_by(plano_id=plano.id).all():
            nome = mps_nome.get(lin.materia_prima_id)
            if nome and (lin.quantidade or 0) > 0:
                reserva[nome] = reserva.get(nome, 0.0) + float(lin.quantidade)

    itens = []
    for nome, d in lista.items():
        necessario = d['quantidade'] or 0
        reservado = reserva.get(nome, 0.0)
        estoque = (d.get('estoque_atual') or 0) + reservado
        itens.append({
            'nome': nome,
            'necessario': round(necessario, 1),
            'unidade': 'un' if d.get('em_unidades') else 'g',
            'estoque': round(estoque, 1),
            'falta': round(max(0, necessario - estoque), 1),
            'reservado': round(reservado, 1),
            'fornecedor': (d.get('fornecedor') or '').strip(),
        })
    itens.sort(key=lambda x: (-x['falta'], x['nome'].lower()))
    return {'data': iso, 'receitas_n': len(itens_motor), 'itens': itens,
            'faltam_n': sum(1 for x in itens if x['falta'] > 0),
            'reservado_total_n': sum(1 for x in itens if x['reservado'] > 0),
            'sem_cadastro': sorted(sem_cadastro)}
