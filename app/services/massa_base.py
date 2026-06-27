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
    """Calcula a cascata da `massa_base`.

    multiplicadores: {receita_id: nº de porções (batidas-base) no plano}. Default
    1 de cada (visualização). Retorna None se o grupo não tem receitas; senão um
    dict com:
      - base: {ingrediente: g por porção} (o mínimo comum)
      - base_mix: {ingrediente: g total na amassadeira}
      - base_massa: g total da base
      - fornadas: nº de batidas (ceil(base_massa/capacidade)) ou None
      - capacidade: g (capacidade da amassadeira do grupo)
      - passos: lista na ordem da cascata, cada um:
          {receita_id, nome, porcoes, massa_porcao, tirar_massa,
           acrescentar: {ingrediente: g a adicionar ANTES de tirar}}
      - avisos: textos (ex: ordem não forma cadeia)
      - total_porcoes
    """
    receitas = [it.receita for it in massa_base.itens if it.receita]
    if not receitas:
        return None
    mult = multiplicadores or {}
    porcoes = {r.id: max(0, int(mult.get(r.id, 1))) for r in receitas}
    ings = {r.id: ingredientes_por_porcao(r) for r in receitas}

    # base = mínimo comum de cada ingrediente (presente em TODAS as receitas;
    # se faltar em alguma, o mínimo é 0 e não entra na base).
    nomes = set().union(*[set(d) for d in ings.values()])
    base = {}
    for nome in nomes:
        m = min(ings[r.id].get(nome, 0.0) for r in receitas)
        if m > _TOL:
            base[nome] = m

    total_porcoes = sum(porcoes.values())
    base_mix = {nome: g * total_porcoes for nome, g in base.items()}
    base_massa = sum(base_mix.values())

    # cascata: parte do running = base; a cada receita acrescenta o que falta
    # (pras porções ainda na bacia) e tira a receita.
    running = dict(base)
    restantes = total_porcoes
    passos = []
    avisos = []
    for r in receitas:
        inc = {}
        for nome in set(ings[r.id]) | set(running):
            d = ings[r.id].get(nome, 0.0) - running.get(nome, 0.0)
            if d > _TOL:
                inc[nome] = d
            elif d < -_TOL:
                avisos.append(
                    '%s: "%s" precisaria DIMINUIR %.0f g — a ordem não forma '
                    'cadeia (mova essa receita pra mais cedo).'
                    % (r.nome, nome, -d))
        acrescentar = {nome: g * restantes for nome, g in inc.items()}
        running = dict(ings[r.id])
        massa_porcao = sum(ings[r.id].values())
        passos.append({
            'receita_id': r.id, 'nome': r.nome, 'porcoes': porcoes[r.id],
            'massa_porcao': round(massa_porcao, 1),
            'tirar_massa': round(massa_porcao * porcoes[r.id], 1),
            'acrescentar': {n: round(g, 1) for n, g in acrescentar.items()},
        })
        restantes -= porcoes[r.id]

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
        'passos': passos, 'avisos': avisos, 'total_porcoes': total_porcoes,
    }
