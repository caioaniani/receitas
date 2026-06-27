"""Massa-base com retiradas em cascata.

A padaria amassa UMA base comum (o mínimo de cada ingrediente entre as receitas
do grupo) e vai tirando cada receita em sequência, acrescentando só o incremento
que falta pra próxima:

    Amassar base ─► tirar Pão Francês ─► +água ─► tirar Sourdough ─► +grãos ─►
    tirar Sourdough 7 grãos

1 amassada no lugar de N. Este módulo calcula, a partir da ficha técnica de cada
receita, a base comum, os acréscimos de cada passo, a massa total e quantas
batidas (fornadas) a base ocupa na amassadeira.

Só entram na massa os ingredientes que vão pra amassadeira (mp percentual e
mp_direto) — add-ins de montagem (sub-receita e mp_un) ficam de fora, igual
`producao.massa_receita_base`.
"""
from math import ceil

# Tolerância (g) pra tratar diferença como acréscimo real / detectar cadeia
# inválida (evita ruído de arredondamento de %).
_TOL = 0.5


def ingredientes_por_porcao(receita):
    """{ingrediente_nome: gramas por porção-base} dos ingredientes que vão na
    amassadeira. Mesma regra de `massa_receita_base`."""
    peso = receita.peso_base or 0
    out = {}
    for ing in receita.ingredientes:
        tipo = ing.tipo or 'mp'
        nome = ing.ingrediente_nome
        if tipo == 'mp_direto':
            out[nome] = out.get(nome, 0.0) + (ing.porcentagem or 0)
        elif tipo not in ('receita', 'mp_un'):
            out[nome] = out.get(nome, 0.0) + (ing.porcentagem or 0) / 100.0 * peso
    return out


def calcular_cascata(massa_base, multiplicadores=None):
    """Calcula a cascata da `massa_base` como uma ÁRVORE (não uma fila).

    O sistema descobre a estrutura sozinho a partir da ficha técnica:
      - amassa a base (o mínimo comum de cada ingrediente);
      - puxa em LINHA as receitas que vão se aninhando (ex: tira o pão francês,
        acrescenta a água comum e tira o sourdough tradicional);
      - quando sobram receitas com recheios EXCLUSIVOS (ex: 7 grãos × nozes e
        azeitonas — uma não é continuação da outra), elas RAMIFICAM: cada uma
        tira sua porção da massa branca e recebe o seu recheio à parte.

    multiplicadores: {receita_id: nº de porções no plano}. Aceita FRACIONÁRIO
    (ex: 212 un / rendimento 10 = 21,2 porções) — a massa e os acréscimos seguem
    o consumo REAL, não o multiplicador inteiro de fornada (que arredonda pra
    cima e infla a base). Default 1 de cada.
    Retorna None se o grupo não tem receitas; senão um dict com:
      - base / base_mix / base_massa / fornadas / capacidade / total_porcoes
      - lineares: passos da linha principal, em ordem. Cada um:
          {receita_id?, nome?, porcoes?, tirar_massa?,
           acrescentar: {ingrediente: g}}  (nome None = só um acréscimo comum)
      - ramos: receitas com recheio próprio (paralelas), cada uma:
          {receita_id, nome, porcoes, tirar_branca (g da massa branca),
           acrescentar: {recheio: g}, tirar_massa (g final)}
      - avisos: [] (a árvore não precisa de ordem manual)
    """
    receitas = [it.receita for it in massa_base.itens if it.receita]
    if not receitas:
        return None
    mult = multiplicadores or {}
    porcoes = {r.id: max(0.0, float(mult.get(r.id, 1))) for r in receitas}
    receitas = [r for r in receitas if porcoes[r.id] > 0]
    if not receitas:
        return None
    ings = {r.id: ingredientes_por_porcao(r) for r in receitas}

    # base = mínimo comum de cada ingrediente (entre as receitas presentes).
    nomes = set().union(*[set(d) for d in ings.values()])
    base = {n: min(ings[r.id].get(n, 0.0) for r in receitas) for n in nomes}
    base = {n: g for n, g in base.items() if g > _TOL}

    total_porcoes = sum(porcoes[r.id] for r in receitas)
    base_mix = {n: g * total_porcoes for n, g in base.items()}
    base_massa = sum(base_mix.values())

    def _massa(comp):
        return sum(comp.values())

    def _falta(r, running):
        """Ingredientes que faltam pra completar r a partir de `running`."""
        d = {}
        for n in set(ings[r.id]) | set(running):
            v = ings[r.id].get(n, 0.0) - running.get(n, 0.0)
            if v > _TOL:
                d[n] = v
        return d

    running = dict(base)
    remaining = list(receitas)
    porcoes_rest = total_porcoes
    pendente = {}          # acréscimo comum acumulado, anexado ao próximo "tirar"
    lineares = []
    ramos = []
    guarda = 0
    while remaining and guarda < 1000:
        guarda += 1

        # incremento COMUM acima de running (mínimo das faltas sobre TODAS as
        # receitas restantes) — vai pro tronco.
        faltas = {r.id: _falta(r, running) for r in remaining}
        nomes_f = set().union(*[set(f) for f in faltas.values()]) if faltas else set()
        comum = {}
        for n in nomes_f:
            m = min(faltas[r.id].get(n, 0.0) for r in remaining)
            if m > _TOL:
                comum[n] = m
        if comum:
            for n, g in comum.items():
                running[n] = running.get(n, 0.0) + g
                pendente[n] = pendente.get(n, 0.0) + g * porcoes_rest
            continue       # re-avalia: agora alguém pode estar completo

        completas = [r for r in remaining if not _falta(r, running)]
        if completas:
            for r in completas:
                lineares.append({
                    'receita_id': r.id, 'nome': r.nome, 'porcoes': porcoes[r.id],
                    'tirar_massa': round(_massa(ings[r.id]) * porcoes[r.id], 1),
                    'acrescentar': {n: round(g, 1) for n, g in pendente.items()}})
                pendente = {}
                remaining.remove(r)
                porcoes_rest -= porcoes[r.id]
            continue

        # Sem incremento comum e ninguém completo:
        if len(remaining) == 1:
            # cauda linear: a última receita recebe o que falta e é tirada.
            r = remaining[0]
            falta = _falta(r, running)
            for n, g in falta.items():
                pendente[n] = pendente.get(n, 0.0) + g * porcoes[r.id]
            lineares.append({
                'receita_id': r.id, 'nome': r.nome, 'porcoes': porcoes[r.id],
                'tirar_massa': round(_massa(ings[r.id]) * porcoes[r.id], 1),
                'acrescentar': {n: round(g, 1) for n, g in pendente.items()}})
            pendente = {}
            remaining = []
        else:
            # RAMIFICA: recheios exclusivos. Se sobrou acréscimo comum pendente,
            # ele já está na massa branca (running) — registra como passo solto.
            if pendente:
                lineares.append({'receita_id': None, 'nome': None, 'porcoes': None,
                                 'tirar_massa': None,
                                 'acrescentar': {n: round(g, 1)
                                                 for n, g in pendente.items()}})
                pendente = {}
            branca = _massa(running)
            for r in remaining:
                falta = _falta(r, running)
                ramos.append({
                    'receita_id': r.id, 'nome': r.nome, 'porcoes': porcoes[r.id],
                    'tirar_branca': round(branca * porcoes[r.id], 1),
                    'acrescentar': {n: round(g * porcoes[r.id], 1)
                                    for n, g in falta.items()},
                    'tirar_massa': round(_massa(ings[r.id]) * porcoes[r.id], 1)})
            remaining = []

    caps = [int(getattr(r, 'capacidade_amassadeira_g', 0) or 0) for r in receitas]
    caps = [c for c in caps if c > 0]
    cap = min(caps) if caps else 0
    fornadas = ceil(base_massa / cap) if cap > 0 and base_massa > 0 else None

    return {
        'massa_base': massa_base,
        'base': {n: round(g, 1) for n, g in base.items()},
        'base_mix': {n: round(g, 1) for n, g in base_mix.items()},
        'base_massa': round(base_massa, 1),
        'fornadas': fornadas, 'capacidade': cap,
        'lineares': lineares, 'ramos': ramos, 'avisos': [],
        'total_porcoes': total_porcoes,
    }
