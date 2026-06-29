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
    """Calcula a cascata da `massa_base` como uma sequência de passos na ordem
    em que o padeiro executa, partindo da ficha técnica.

    Regra física (confirmada pelo dono): a ÁGUA (e tudo que é da massa branca:
    farinha, levain, sal, fermento) só pode ser ADICIONADA na amassadeira — a
    hidratação só sobe ao longo do dia. Então:
      - amassa a base (o mínimo comum de cada ingrediente da massa branca);
      - as receitas saem em ordem de HIDRATAÇÃO CRESCENTE; a água é um passo do
        TRONCO ('incremento'), aplicado a toda a massa que ainda está na
        amassadeira;
      - quando uma receita atinge a sua hidratação, ela é TIRADA. Se tiver
        recheio EXCLUSIVO (grãos, nozes — ingrediente que não está na base),
        o recheio é batido na própria porção tirada (batida separada na
        amassadeira). Essas retiradas são marcadas eh_ramo=True (laranja); as
        sem recheio são da linha principal (verde).

    multiplicadores: {receita_id: nº de porções no plano}. Aceita FRACIONÁRIO
    (ex: 212 un / rendimento 10 = 21,2 porções) — a massa e os acréscimos seguem
    o consumo REAL, não o multiplicador inteiro de fornada (que arredonda pra
    cima e infla a base). Default 1 de cada.
    Retorna None se o grupo não tem receitas; senão um dict com:
      - base / base_mix / base_massa / fornadas / capacidade / total_porcoes
      - passos: lista ORDENADA. Cada passo é:
          incremento: {tipo:'incremento', nome:None, acrescentar:{ing:g}}
                      (água/líquido posto na amassadeira inteira)
          retirada:   {tipo:'retirada', receita_id, nome, porcoes, tirar_massa,
                       acrescentar:{recheio:g}, eh_ramo}
      - avisos: []
    """
    receitas = [it.receita for it in massa_base.itens if it.receita]
    if not receitas:
        return None
    # Sem multiplicadores -> modo GENERICO (config/preview da base): mostra TODAS
    # as receitas-membro com 1 porcao. COM multiplicadores (plano do dia) -> so
    # as receitas DO PLANO contam; membro fora do plano = 0, sai da cascata.
    # (Antes o default era 1 mesmo com plano, e receita que NAO ia ser produzida
    # hoje aparecia com 1 porcao fantasma — ex: Nozes e Azeitonas / 7 Graos num
    # dia que nao os produz.)
    if multiplicadores is None:
        porcoes = {r.id: 1.0 for r in receitas}
    else:
        porcoes = {r.id: max(0.0, float(multiplicadores.get(r.id, 0)))
                   for r in receitas}
    receitas = [r for r in receitas if porcoes[r.id] > 0]
    if not receitas:
        return None
    ings = {r.id: ingredientes_por_porcao(r) for r in receitas}

    # base = mínimo comum de cada ingrediente (entre as receitas presentes).
    nomes = set().union(*[set(d) for d in ings.values()])
    base = {n: min(ings[r.id].get(n, 0.0) for r in receitas) for n in nomes}
    base = {n: g for n, g in base.items() if g > _TOL}
    # massa branca = ingredientes comuns (vão na amassadeira); o resto é recheio.
    compartilhado = set(base)

    total_porcoes = sum(porcoes[r.id] for r in receitas)
    base_mix = {n: g * total_porcoes for n, g in base.items()}
    base_massa = sum(base_mix.values())

    def _massa(comp):
        return sum(comp.values())

    def _falta_branca(r, running):
        """Ingredientes COMPARTILHADOS que faltam pra completar a massa branca."""
        d = {}
        for n in compartilhado:
            v = ings[r.id].get(n, 0.0) - running.get(n, 0.0)
            if v > _TOL:
                d[n] = v
        return d

    def _recheio(r):
        """Add-ins exclusivos (fora da base) — batidos na porção tirada."""
        return {n: g for n, g in ings[r.id].items()
                if n not in compartilhado and g > _TOL}

    def _passo_retirada(r, extra):
        rec = _recheio(r)
        acr = dict(extra)
        for n, g in rec.items():
            acr[n] = acr.get(n, 0.0) + g
        return {'tipo': 'retirada', 'receita_id': r.id, 'nome': r.nome,
                'porcoes': porcoes[r.id],
                'tirar_massa': round(_massa(ings[r.id]) * porcoes[r.id], 1),
                'acrescentar': {n: round(g * porcoes[r.id], 1)
                                for n, g in acr.items()},
                'eh_ramo': bool(rec)}

    running = dict(base)
    remaining = list(receitas)
    porcoes_rest = total_porcoes
    passos = []
    guarda = 0
    while remaining and guarda < 1000:
        guarda += 1

        # 1. incremento COMUM da massa branca (água etc.) — sobe a amassadeira
        #    inteira pro próximo nível de hidratação.
        faltas = {r.id: _falta_branca(r, running) for r in remaining}
        comum = {}
        for n in compartilhado:
            m = min(faltas[r.id].get(n, 0.0) for r in remaining)
            if m > _TOL:
                comum[n] = m
        if comum:
            for n, g in comum.items():
                running[n] = running.get(n, 0.0) + g
            passos.append({'tipo': 'incremento', 'receita_id': None, 'nome': None,
                           'porcoes': None, 'tirar_massa': None, 'eh_ramo': False,
                           'acrescentar': {n: round(g * porcoes_rest, 1)
                                           for n, g in comum.items()}})
            continue

        # 2. tira quem já está na hidratação certa (só falta o recheio próprio).
        #    Verde (sem recheio) primeiro, laranja depois — leitura mais limpa.
        prontas = [r for r in remaining if not _falta_branca(r, running)]
        if prontas:
            prontas.sort(key=lambda r: bool(_recheio(r)))
            for r in prontas:
                passos.append(_passo_retirada(r, {}))
                remaining.remove(r)
                porcoes_rest -= porcoes[r.id]
            continue

        # 3. impasse (raro): ingredientes da branca divergem entre as receitas
        #    restantes. Cada uma puxa a massa branca atual e finaliza na própria
        #    porção (água + recheio batidos só nela).
        for r in remaining:
            passos.append(_passo_retirada(r, _falta_branca(r, running)))
        porcoes_rest = 0
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
        'passos': passos, 'avisos': [],
        'total_porcoes': total_porcoes,
    }
