"""Gantt da produção do dia (fluxograma do padeiro).

Pega o plano aprovado do cronograma (`PlanejamentoProducao origem='cronograma'`)
e agenda as etapas de cada receita numa linha do tempo, respeitando:

- **1 amassadeira e 1 forno** (a padaria tem 1 de cada): as etapas que usam
  esses equipamentos SERIALIZAM — duas receitas não amassam/assam ao mesmo tempo.
- **2 padeiros em filas independentes**: um para pães (incluindo Brioche) e
  outro para viennoiserie. As etapas manuais de uma fila não bloqueiam a outra.
  Enquanto a amassadeira/forno trabalha sozinha, cada padeiro também pode
  adiantar a etapa manual de outra receita da própria fila.
- **Descansos curtos** (< 4h: caixa, fermentação de pão de mesmo dia) ficam
  inline na linha — não ocupam ninguém, só seguram a receita.
- **Fermentação longa** (≥ 4h: lead time de pão de fermentação natural) NÃO
  cabe no dia: vira um marcador "→ câmara fria 48h" no fim da receita. O
  cronograma já antecipa a produção em N dias por causa disso; aqui mostramos só
  o trabalho que ARRANCA o ciclo no dia.

A escala do eixo é REAL (minutos), origem 06:00. Greedy list-scheduling: a cada
passo escolhe a etapa pronta que começa mais cedo (desempate: menor duração).
"""

from app.services.centros_producao import (
    CENTRO_PAES,
    CENTRO_VIENNOISERIE,
    centro_trabalho_receita,
    rotulo_centro,
)
from app.services.producao import fornadas_amassadeira

DIA_INI = 6 * 60          # 06:00 — origem do eixo (minutos desde a meia-noite)

# Quantos dias pra trás procurar pães em fermentação que são ASSADOS hoje.
MAX_LEAD_DIAS = 3
TURNOS = [
    {'nome': 'Manhã', 'ini': 6 * 60, 'fim': 14 * 60},
    {'nome': 'Tarde', 'ini': 13 * 60, 'fim': 21 * 60},
]

# Etapa passiva acima disso = lead time (carrega pra fora do dia).
PASSIVA_LONGA_MIN = 240   # 4h

# Escala do eixo: pixels por minuto. Largura fixa por minuto (a página rola na
# horizontal) pra cada etapa ter espaço real e o rótulo ser legível.
PX_POR_MIN = 6

# Massa-base: passos manuais curtos da cascata (tirar a receita / acrescentar).
TIRAR_MIN = 4
ACRESCENTAR_MIN = 3


def _g_label(gramas):
    g = round(gramas or 0)
    if g >= 1000:
        return ('%.1f kg' % (g / 1000.0)).replace('.0 kg', ' kg').replace('.', ',')
    return '%d g' % g


def _step_acrescentar(acr):
    """Passo "+ ingredientes" da cascata (na amassadeira), ou None se nada a
    acrescentar. Ocupa a amassadeira — a base está nela sendo trabalhada, então
    isso encadeia logo após o amassamento e segura a máquina pra outra massa."""
    if not acr:
        return None
    txt = ', '.join('%s %s' % (n, _g_label(g)) for n, g in acr.items())
    return {'nome': '+ ' + txt, 'equip': 'amassadeira', 'ativa': True,
            'dur': ACRESCENTAR_MIN}


def _split_long_passiva(etapas):
    """Divide as etapas no primeiro descanso LONGO (≥ PASSIVA_LONGA_MIN). O que
    vem ANTES é o trabalho do dia da amassada; o que vem DEPOIS (laminar, modelar,
    assar, congelar) é a FINALIZAÇÃO, que cai `dias_producao` dias adiante. Sem
    descanso longo: tudo é do mesmo dia (post vazio)."""
    for i, e in enumerate(etapas):
        # congelar é passo final (freezer), não fermentação — não divide a cascata.
        if (not bool(e.ativa) and e.equipamento != 'congelar'
                and int(e.duracao_min or 0) >= PASSIVA_LONGA_MIN):
            return etapas[:i], e, etapas[i + 1:]
    return etapas, None, []

# Paleta estável por índice de produto.
_CORES = ['#0d6efd', '#198754', '#fd7e14', '#6f42c1', '#d63384', '#20c997',
          '#dc3545', '#0dcaf0', '#caa300', '#6610f2', '#0a8f6c', '#495057']


def _recurso(equip, ativa, centro=CENTRO_PAES):
    """Recurso (capacidade 1) que a etapa ocupa, ou None se passiva.

    Etapas de MÁQUINA (amassadeira/forno) ocupam o EQUIPAMENTO, não o padeiro —
    a máquina trabalha sozinha e a pessoa fica livre pra adiantar outra receita.
    Só o trabalho manual (mise en place, modelagem) ocupa o padeiro do centro
    da receita: pães ou viennoiserie."""
    if not ativa:
        return None
    if equip in ('amassadeira', 'forno'):
        return equip          # máquina trabalha sozinha (padeiro livre)
    return 'padeiro_' + centro


def _icone(equip, ativa):
    if equip == 'amassadeira':
        return '🥣'
    if equip == 'forno':
        return '🔥'
    if equip == 'camara_fria':
        return '❄️'
    if equip == 'congelar':
        return '🧊'           # freezer (passo final)
    if not ativa:
        return '⏳'           # descanso / fermentação
    return '✋'               # mão de obra do padeiro


def _hhmm(minuto):
    h, m = divmod(int(minuto), 60)
    return '%02d:%02d' % (h % 24, m)


def _dur_label(minutos):
    m = int(minutos or 0)
    if m >= 60:
        h = m / 60.0
        return ('%dh' % int(h)) if h == int(h) else ('%.1fh' % h).replace('.', ',')
    return '%d min' % m


def montar_gantt(dia):
    """Agenda a produção do `dia`. Retorna o dict do Gantt, ou None se não há
    plano aprovado pra esse dia."""
    from app.models import PlanejamentoProducao

    # só ordens ENVIADAS ao padeiro (fluxo de 2 passos: aprovar -> enviar).
    plano = (PlanejamentoProducao.query
             .filter_by(data=dia, origem='cronograma')
             .filter(PlanejamentoProducao.enviado_ao_padeiro.isnot(False))
             .first())

    # 1) Itens do plano com falta > 0 e etapas; marca quem é de massa-base.
    #    O plano de HOJE pode não existir (dia só de finalização de pães
    #    amassados antes) — nesse caso só rodam as continuações (1c).
    from app.models import MassaBaseItem
    membership = {row.receita_id: row for row in MassaBaseItem.query.all()}
    itens_plano = []
    sem_etapas = []
    for it in (plano.itens if plano else []):
        rec = it.receita
        if rec is None:
            continue
        if it.dispensada_em is not None:
            continue                      # dispensado pelo admin: sai do gantt
        falta = max(0, int(it.qtd_alvo or 0) - int(it.produzido_qtd or 0))
        if falta <= 0:
            continue
        etapas = list(rec.etapas)
        if not etapas:
            sem_etapas.append(rec.nome)
            continue
        itens_plano.append({
            'rec': rec, 'falta': falta, 'mult': it.multiplicador, 'etapas': etapas,
            'nf': fornadas_amassadeira(rec, it.multiplicador) or 1,
            'mbi': membership.get(rec.id)})

    produtos = []
    jobs = []

    def _novo_produto(nome, **kw):
        cor = kw.get('cor') or _CORES[len(produtos) % len(_CORES)]
        centro = kw.get('centro', CENTRO_PAES)
        p = {'nome': nome, 'cor': cor, 'tarefas': [], 'destino': None,
             'fim_min': 0, 'falta': kw.get('falta'), 'fornadas': kw.get('fornadas'),
             'tipo': kw.get('tipo', 'solo'), 'grupo': kw.get('grupo'),
             'receita_id': kw.get('receita_id'), 'centro': centro,
             'centro_label': rotulo_centro(centro)}
        produtos.append(p)
        return p

    def _passos(etapas, nf):
        # Etapas ATIVAS escalam com o nº de fornadas; passiva fica na duração base.
        return [{'nome': e.nome, 'equip': e.equipamento, 'ativa': bool(e.ativa),
                 'dur': int(e.duracao_min or 0) * (nf if e.ativa else 1)}
                for e in etapas]

    def _idx_amassadeira(etapas):
        idx = [i for i, e in enumerate(etapas) if e.equipamento == 'amassadeira']
        return idx[-1] if idx else -1

    # 1a) Receitas SOLO (sem massa-base): 1 job por receita, como sempre.
    for pi in [x for x in itens_plano if x['mbi'] is None]:
        prod = _novo_produto(pi['rec'].nome, falta=pi['falta'], fornadas=pi['nf'],
                             receita_id=pi['rec'].id,
                             centro=centro_trabalho_receita(pi['rec']))
        jobs.append({'prod': prod, 'passos': _passos(pi['etapas'], pi['nf']),
                     'ptr': 0, 'ready': 0})

    # 1b) Massa-base: UMA amassada da base (tronco) + retiradas em hidratação
    #     crescente; cada receita começa as etapas pós-amassamento na sua retirada.
    from app.services.massa_base import calcular_cascata
    grupos = {}
    for pi in [x for x in itens_plano if x['mbi'] is not None]:
        grupos.setdefault(pi['mbi'].massa_base_id, []).append(pi)

    for mb_id, items in grupos.items():
        mb = items[0]['mbi'].massa_base
        mults = {pi['rec'].id: max(1, int(pi['mult'] or 1)) for pi in items}
        calc = calcular_cascata(mb, mults)
        by_id = {pi['rec'].id: pi for pi in items}
        retiradas = ([p for p in (calc or {}).get('passos', [])
                      if p.get('tipo') == 'retirada']) if calc else []
        if not retiradas:
            for pi in items:                       # fallback: trata como solo
                prod = _novo_produto(pi['rec'].nome, falta=pi['falta'],
                                     fornadas=pi['nf'], receita_id=pi['rec'].id,
                                     centro=centro_trabalho_receita(pi['rec']))
                jobs.append({'prod': prod, 'passos': _passos(pi['etapas'], pi['nf']),
                             'ptr': 0, 'ready': 0})
            continue

        base_nf = calc['fornadas'] or 1
        cor_grupo = _CORES[len(produtos) % len(_CORES)]
        trunk_prod = _novo_produto('Massa base: ' + mb.nome, cor=cor_grupo,
                                   tipo='base', grupo=mb_id, fornadas=base_nf,
                                   centro=centro_trabalho_receita(items[0]['rec']))
        # quantidade e receita da base JÁ ESCALADAS pro plano do dia (a tela
        # Massa base mostra só pra 1 porção; aqui é o total a amassar no dia).
        trunk_prod['base_massa_label'] = _g_label(calc['base_massa'])
        trunk_prod['base_recipe'] = [
            {'nome': n, 'qtd': _g_label(g)} for n, g in
            sorted(calc['base_mix'].items(), key=lambda kv: -kv[1])]

        # ramos/derivados (cada receita): bloqueados até a sua retirada
        branch_jobs = {}
        for p in retiradas:
            pi = by_id.get(p['receita_id'])
            if pi is None:
                continue
            i = _idx_amassadeira(pi['etapas'])
            post = pi['etapas'][i + 1:] if i >= 0 else pi['etapas']
            prod_r = _novo_produto(pi['rec'].nome, falta=pi['falta'],
                                   fornadas=pi['nf'], tipo='ramo', grupo=mb_id,
                                   receita_id=pi['rec'].id,
                                   centro=centro_trabalho_receita(pi['rec']))
            bj = {'prod': prod_r, 'passos': _passos(post, pi['nf']),
                  'ptr': 0, 'ready': None}        # None = bloqueado
            branch_jobs[p['receita_id']] = bj
            jobs.append(bj)

        # tronco: processo da base (mise..amassar, escalado por base_nf) + a
        # cascata. Cada "tirar" desbloqueia a receita correspondente.
        rid0 = retiradas[0]['receita_id']
        tmpl = by_id[rid0]['etapas']
        i = _idx_amassadeira(tmpl)
        pre = tmpl[:i + 1] if i >= 0 else tmpl
        trunk_passos = []
        for e in pre:
            ativa = bool(e.ativa)
            nome = 'Amassar base' if e.equipamento == 'amassadeira' else e.nome
            trunk_passos.append({'nome': nome, 'equip': e.equipamento, 'ativa': ativa,
                                 'dur': int(e.duracao_min or 0) * (base_nf if ativa else 1)})

        # cascata em ordem: incrementos de água (tronco) e retiradas; cada
        # "Tirar X" desbloqueia a receita. O recheio (acrescentar da retirada) é
        # batido na porção — entra como passo curto antes do "Tirar".
        for p in calc['passos']:
            s = _step_acrescentar(p.get('acrescentar'))
            if s:
                trunk_passos.append(s)
            if p['tipo'] == 'retirada':
                # retirada ocupa a AMASSADEIRA (puxa a porção da base que está
                # nela) — encadeia logo após o amassamento, sem esperar o padeiro
                # que pode estar em outra receita, e segura a máquina até esvaziar.
                trunk_passos.append({'nome': 'Tirar ' + p['nome'],
                                     'equip': 'amassadeira', 'ativa': True,
                                     'dur': TIRAR_MIN,
                                     'desbloqueia': branch_jobs.get(p['receita_id'])})
        jobs.append({'prod': trunk_prod, 'passos': trunk_passos, 'ptr': 0, 'ready': 0})

    # 1c) CONTINUAÇÃO: pães amassados em dias ANTERIORES cuja fermentação termina
    #     hoje — a FINALIZAÇÃO (laminar/modelar/assar/congelar). rec.dias_producao
    #     = lead em dias (sourdough/croissant de 24h = 1, de 48h = 2); o que foi
    #     amassado em (dia - L) é finalizado hoje. Torna o fluxograma contínuo: a
    #     parte de assar/finalizar aparece no dia certo, não só no dia da amassada.
    from datetime import timedelta
    for L in range(1, MAX_LEAD_DIAS + 1):
        plano_ant = (PlanejamentoProducao.query
                     .filter_by(data=dia - timedelta(days=L), origem='cronograma')
                     .filter(PlanejamentoProducao.enviado_ao_padeiro.isnot(False))
                     .first())
        if plano_ant is None:
            continue
        for it in plano_ant.itens:
            rec = it.receita
            if rec is None or int(rec.dias_producao or 0) != L:
                continue
            if it.dispensada_em is not None:
                continue                  # dispensado pelo admin: não finaliza
            falta = max(0, int(it.qtd_alvo or 0) - int(it.produzido_qtd or 0))
            if falta <= 0:
                continue
            _, _, post = _split_long_passiva(list(rec.etapas))
            if not post:
                continue            # sem etapa pós-fermentação: nada a finalizar
            nf = fornadas_amassadeira(rec, it.multiplicador) or 1
            prod = _novo_produto(rec.nome, falta=falta, fornadas=nf,
                                 tipo='continuacao', receita_id=rec.id,
                                 centro=centro_trabalho_receita(rec))
            prod['origem_label'] = (dia - timedelta(days=L)).strftime('%d/%m')
            jobs.append({'prod': prod, 'passos': _passos(post, nf),
                         'ptr': 0, 'ready': 0})

    # 2) Greedy list-scheduling com recursos de capacidade 1. Jobs bloqueados
    #    (ready None) ficam de fora até a retirada que os libera.
    livre = {
        'padeiro_' + CENTRO_PAES: 0,
        'padeiro_' + CENTRO_VIENNOISERIE: 0,
        'amassadeira': 0,
        'forno': 0,
    }
    guarda = 0
    while guarda < 20000:
        guarda += 1
        melhor = None
        for j in jobs:
            if j['ready'] is None or j['ptr'] >= len(j['passos']):
                continue
            p = j['passos'][j['ptr']]
            rec = _recurso(p['equip'], p['ativa'], j['prod']['centro'])
            ini = j['ready'] if rec is None else max(j['ready'], livre[rec])
            chave = (ini, p['dur'])     # menor início; desempate menor duração
            if melhor is None or chave < melhor[0]:
                melhor = (chave, j, p, rec, ini)
        if melhor is None:
            break
        _, j, p, rec, ini = melhor
        prod = j['prod']

        # Fermentação longa: corta o dia aqui — vira marcador "→ câmara fria".
        # Congelar NÃO corta (é o passo final, fica inline com ícone de freezer).
        if (rec is None and not p['ativa'] and p['equip'] != 'congelar'
                and p['dur'] >= PASSIVA_LONGA_MIN):
            prod['destino'] = '%s %s' % (_icone(p['equip'], False),
                                         _dur_label(p['dur']))
            prod['destino_etapa'] = p['nome']
            j['ptr'] = len(j['passos'])      # encerra a receita no dia
            continue

        fim = ini + p['dur']
        if rec is not None:
            livre[rec] = fim
        j['ready'] = fim
        j['ptr'] += 1
        if p.get('desbloqueia') is not None:     # retirada libera a receita
            p['desbloqueia']['ready'] = fim
        prod['fim_min'] = max(prod['fim_min'], fim)
        prod['tarefas'].append({
            'etapa': p['nome'], 'equip': p['equip'], 'ativa': p['ativa'],
            'recurso': rec or 'descanso', 'retirada': bool(p.get('desbloqueia')),
            'ini': ini, 'fim': fim, 'dur': p['dur'],
            'ini_hhmm': _hhmm(DIA_INI + ini), 'fim_hhmm': _hhmm(DIA_INI + fim),
            'dur_label': _dur_label(p['dur']),
            'icone': _icone(p['equip'], p['ativa']),
        })

    # Nada a fazer hoje (sem plano e sem continuação) → sem Gantt.
    if not produtos and plano is None:
        return None

    # 3) Eixo em PIXELS (escala fixa: cada minuto vale PX_POR_MIN px). Layout em
    #    px — e não em % — pra cada etapa ter largura real e legível (a página
    #    rola na horizontal); assim dá pra decidir se o rótulo cabe DENTRO da
    #    barra ou se vai pro lado direito (fora), sem cortar o texto.
    fim_geral = max([p['fim_min'] for p in produtos] or [0])
    eixo_fim = max(fim_geral, 14 * 60 - DIA_INI)        # em min desde 06:00
    span = eixo_fim or 1
    canvas_px = span * PX_POR_MIN

    horas = []
    h = 0
    while h <= eixo_fim:
        horas.append({'min': h, 'label': _hhmm(DIA_INI + h),
                      'px': h * PX_POR_MIN})
        h += 60

    # Ordena por "cluster" (uma receita solo, ou um tronco de massa-base + seus
    # ramos) pelo início mais cedo do cluster; dentro do cluster o tronco vem
    # antes dos ramos. Mantém o grupo junto e em ordem de leitura.
    def _ini(p):
        return p['tarefas'][0]['ini'] if p['tarefas'] else 1e9

    def _cluster(p):
        return 'g:%s' % p['grupo'] if p['grupo'] is not None else 's:%d' % id(p)
    cluster_ini = {}
    for p in produtos:
        ch = _cluster(p)
        cluster_ini[ch] = min(cluster_ini.get(ch, 1e9), _ini(p))

    def _chave(p):
        rank = 0 if p['tipo'] in ('solo', 'base') else 1   # tronco antes do ramo
        return (cluster_ini[_cluster(p)], _cluster(p), rank, _ini(p), p['nome'])
    produtos.sort(key=_chave)

    for p in produtos:
        tarefas = p['tarefas']
        for idx, t in enumerate(tarefas):
            t['left_px'] = t['ini'] * PX_POR_MIN
            t['width_px'] = max(14, (t['fim'] - t['ini']) * PX_POR_MIN)
            # cabe DENTRO? (ícone + padding + folga; conservador pra não cortar)
            texto_px = len(t['etapa']) * 8.5 + 50
            t['label_dentro'] = t['width_px'] >= texto_px
            t['label_fora'] = False
            # Regra única: o rótulo só vai à DIREITA quando há espaço livre real
            # até o PRÓXIMO elemento — a próxima barra, ou (na última barra) o
            # marcador "→ câmara fria". Sem espaço, fica só o ícone (o nome
            # completo está no "Passo a passo" abaixo). Assim nada colide nem
            # trunca, sem precisar empurrar nada.
            if not t['label_dentro']:
                if idx + 1 < len(tarefas):
                    prox = tarefas[idx + 1]['ini'] * PX_POR_MIN
                elif p.get('destino'):
                    prox = t['left_px'] + t['width_px'] + 6   # o destino vem aqui
                else:
                    prox = canvas_px
                gap = prox - (t['left_px'] + t['width_px'])
                t['label_fora'] = gap >= len(t['etapa']) * 8 + 14
        p['fim_px'] = p['fim_min'] * PX_POR_MIN
    turnos = [{'nome': tt['nome'],
               'left_px': max(0, tt['ini'] - DIA_INI) * PX_POR_MIN,
               'width_px': (min(tt['fim'] - DIA_INI, span)
                            - max(0, tt['ini'] - DIA_INI)) * PX_POR_MIN}
              for tt in TURNOS]

    return {
        'dia': dia, 'dia_iso': dia.isoformat(),
        'eixo_fim': eixo_fim, 'span_min': span, 'canvas_px': canvas_px,
        'horas': horas, 'turnos': turnos,
        'produtos': produtos, 'sem_etapas': sem_etapas,
        'fim_estimado': _hhmm(DIA_INI + fim_geral) if fim_geral else None,
        'plano_id': plano.id if plano else None,
    }
