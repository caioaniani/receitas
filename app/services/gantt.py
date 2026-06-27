"""Gantt da produção do dia (fluxograma do padeiro).

Pega o plano aprovado do cronograma (`PlanejamentoProducao origem='cronograma'`)
e agenda as etapas de cada receita numa linha do tempo, respeitando:

- **1 amassadeira e 1 forno** (a padaria tem 1 de cada): as etapas que usam
  esses equipamentos SERIALIZAM — duas receitas não amassam/assam ao mesmo tempo.
- **1 padeiro (mão de obra)** pras etapas manuais (mise en place, modelagem,
  dobras). Mas enquanto a amassadeira/forno trabalha sozinha, o padeiro fica
  livre pra adiantar a etapa manual de OUTRA receita — é a paralelização que o
  dono pediu ("enquanto o amassamento acontece, abre outro mise en place").
- **Descansos curtos** (< 4h: caixa, fermentação de pão de mesmo dia) ficam
  inline na linha — não ocupam ninguém, só seguram a receita.
- **Fermentação longa** (≥ 4h: lead time de pão de fermentação natural) NÃO
  cabe no dia: vira um marcador "→ câmara fria 48h" no fim da receita. O
  cronograma já antecipa a produção em N dias por causa disso; aqui mostramos só
  o trabalho que ARRANCA o ciclo no dia.

A escala do eixo é REAL (minutos), origem 06:00. Greedy list-scheduling: a cada
passo escolhe a etapa pronta que começa mais cedo (desempate: menor duração).
"""
from app.services.producao import fornadas_amassadeira

DIA_INI = 6 * 60          # 06:00 — origem do eixo (minutos desde a meia-noite)
TURNOS = [
    {'nome': 'Manhã', 'ini': 6 * 60, 'fim': 14 * 60},
    {'nome': 'Tarde', 'ini': 13 * 60, 'fim': 21 * 60},
]

# Etapa passiva acima disso = lead time (carrega pra fora do dia).
PASSIVA_LONGA_MIN = 240   # 4h

# Escala do eixo: pixels por minuto. Largura fixa por minuto (a página rola na
# horizontal) pra cada etapa ter espaço real e o rótulo ser legível.
PX_POR_MIN = 4

# Paleta estável por índice de produto.
_CORES = ['#0d6efd', '#198754', '#fd7e14', '#6f42c1', '#d63384', '#20c997',
          '#dc3545', '#0dcaf0', '#caa300', '#6610f2', '#0a8f6c', '#495057']


def _recurso(equip, ativa):
    """Recurso (capacidade 1) que a etapa ocupa, ou None se passiva.

    Etapas de MÁQUINA (amassadeira/forno) ocupam o EQUIPAMENTO, não o padeiro —
    a máquina trabalha sozinha e a pessoa fica livre pra adiantar outra receita.
    Só o trabalho manual (mise en place, modelagem) ocupa o `padeiro`."""
    if not ativa:
        return None
    if equip in ('amassadeira', 'forno'):
        return equip          # máquina trabalha sozinha (padeiro livre)
    return 'padeiro'          # mão de obra do padeiro


def _icone(equip, ativa):
    if equip == 'amassadeira':
        return '🥣'
    if equip == 'forno':
        return '🔥'
    if equip == 'camara_fria':
        return '❄️'
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

    plano = (PlanejamentoProducao.query
             .filter_by(data=dia, origem='cronograma').first())
    if plano is None:
        return None

    # 1) Monta os jobs (1 por receita com falta > 0 e etapas cadastradas).
    jobs = []
    produtos = []
    sem_etapas = []
    for it in plano.itens:
        rec = it.receita
        if rec is None:
            continue
        falta = max(0, int(it.qtd_alvo or 0) - int(it.produzido_qtd or 0))
        if falta <= 0:
            continue
        etapas = list(rec.etapas)
        if not etapas:
            sem_etapas.append(rec.nome)
            continue
        nf = fornadas_amassadeira(rec, it.multiplicador) or 1
        cor = _CORES[len(produtos) % len(_CORES)]
        produtos.append({'nome': rec.nome, 'cor': cor, 'fornadas': nf,
                         'falta': falta, 'tarefas': [], 'destino': None,
                         'fim_min': 0})
        prod_ref = produtos[-1]
        # Etapas ATIVAS escalam com o nº de fornadas (a máquina carrega 1 batida
        # por vez; o trabalho manual também cresce com o volume). Passiva fica na
        # duração base (um descanso é um descanso).
        passos = []
        for e in etapas:
            ativa = bool(e.ativa)
            base = int(e.duracao_min or 0)
            passos.append({'nome': e.nome, 'equip': e.equipamento,
                           'ativa': ativa, 'dur': base * nf if ativa else base})
        jobs.append({'prod': prod_ref, 'passos': passos, 'ptr': 0, 'ready': 0})

    # 2) Greedy list-scheduling com recursos de capacidade 1.
    livre = {'padeiro': 0, 'amassadeira': 0, 'forno': 0}
    restantes = sum(len(j['passos']) for j in jobs)
    guarda = 0
    while restantes > 0 and guarda < 20000:
        guarda += 1
        melhor = None
        for j in jobs:
            if j['ptr'] >= len(j['passos']):
                continue
            p = j['passos'][j['ptr']]
            rec = _recurso(p['equip'], p['ativa'])
            ini = j['ready'] if rec is None else max(j['ready'], livre[rec])
            chave = (ini, p['dur'])     # menor início; desempate menor duração
            if melhor is None or chave < melhor[0]:
                melhor = (chave, j, p, rec, ini)
        _, j, p, rec, ini = melhor
        prod = j['prod']

        # Fermentação longa: corta o dia aqui — vira marcador "→ câmara fria".
        if rec is None and not p['ativa'] and p['dur'] >= PASSIVA_LONGA_MIN:
            prod['destino'] = '%s %s' % (_icone(p['equip'], False),
                                         _dur_label(p['dur']))
            prod['destino_etapa'] = p['nome']
            j['ptr'] = len(j['passos'])      # encerra a receita no dia
            restantes = sum(len(x['passos']) - x['ptr'] for x in jobs)
            continue

        fim = ini + p['dur']
        if rec is not None:
            livre[rec] = fim
        j['ready'] = fim
        j['ptr'] += 1
        restantes -= 1
        prod['fim_min'] = max(prod['fim_min'], fim)
        prod['tarefas'].append({
            'etapa': p['nome'], 'equip': p['equip'], 'ativa': p['ativa'],
            'recurso': rec or 'descanso',
            'ini': ini, 'fim': fim, 'dur': p['dur'],
            'ini_hhmm': _hhmm(DIA_INI + ini), 'fim_hhmm': _hhmm(DIA_INI + fim),
            'dur_label': _dur_label(p['dur']),
            'icone': _icone(p['equip'], p['ativa']),
        })

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

    # ordena produtos pelo início do 1º trabalho (linha do tempo legível)
    produtos.sort(key=lambda p: (p['tarefas'][0]['ini'] if p['tarefas'] else 1e9,
                                 p['nome']))

    for p in produtos:
        for t in p['tarefas']:
            t['left_px'] = t['ini'] * PX_POR_MIN
            t['width_px'] = max(14, (t['fim'] - t['ini']) * PX_POR_MIN)
            # o rótulo cabe dentro? (estimativa: ~7px/char + ícone + folga)
            texto_px = len(t['etapa']) * 7.3 + 30
            t['label_dentro'] = t['width_px'] >= texto_px
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
        'plano_id': plano.id,
    }
