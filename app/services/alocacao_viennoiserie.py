"""Distribuição pura da massa comum em unidades inteiras de produtos.

O chamador calcula a massa disponível e a necessidade líquida de cada produto,
incluindo os caminhos de sub-receitas, estoques, datas e ordens já enviadas.
Este módulo não conhece o banco nem arredonda a farinha para outro batimento.
"""

import heapq
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, InvalidOperation, localcontext


def _decimal(valor):
    if isinstance(valor, bool):
        return None
    try:
        numero = Decimal(str(valor))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return numero if numero.is_finite() else None


def _inteiro(valor):
    numero = _decimal(valor)
    if numero is None or numero != numero.to_integral_value():
        return None
    return int(numero)


def _texto_decimal(numero):
    texto = format(numero, 'f')
    return texto.rstrip('0').rstrip('.') if '.' in texto else texto


def distribuir_excedente(massa_g, candidatos):
    """Aloca massa comum por necessidade decrescente, sem exceder a massa.

    Cada candidato informa ``receita_id`` (ou ``id``), ``nome``,
    ``gramas_por_unidade``, ``necessidade`` e ``maximo_adicional`` opcional.
    A necessidade é líquida de estoque/produção e diminui a cada unidade
    alocada. Empates usam nome (sem distinguir maiúsculas), depois ID.

    Quando todas as necessidades que ainda cabem na massa estão satisfeitas,
    o restante vai primeiro ao produto de MAIOR necessidade ORIGINAL, até seu
    limite. Essa prioridade fixa também vale para o excedente além da demanda;
    não se inventa demanda igual para todos os produtos.

    Candidatos inválidos, sem necessidade positiva ou com limite zero ficam
    fora. IDs válidos repetidos são erro: o chamador deve consolidar caminhos
    da mesma receita antes de aplicar o limite. Massa inválida é erro.
    Necessidade/capacidade que não cabem em uma unidade inteira não provocam
    outro batimento. O resíduo fica explícito em ``restante_g``.

    A saída tem ``alocacoes`` (ID → unidades inteiras) e ``restante_g`` em
    texto decimal exato. Não modifica a entrada e não grava estoque.
    """
    massa = _decimal(massa_g)
    if massa is None or massa < 0:
        raise ValueError('A massa disponível deve ser um número finito não negativo.')

    validos = []
    ids = set()
    for candidato in candidatos:
        if not isinstance(candidato, dict):
            continue
        rid = _inteiro(candidato.get('receita_id', candidato.get('id')))
        gramas = _decimal(candidato.get('gramas_por_unidade'))
        necessidade = _decimal(candidato.get('necessidade'))
        limite_raw = candidato.get('maximo_adicional')
        limite = _inteiro(limite_raw) if limite_raw is not None else None
        if (rid is None or rid <= 0 or gramas is None or gramas <= 0
                or necessidade is None or necessidade <= 0
                or (limite_raw is not None and (limite is None or limite <= 0))):
            continue
        if rid in ids:
            raise ValueError(f'Receita {rid} repetida: consolide seus caminhos antes de alocar.')
        ids.add(rid)
        validos.append({
            'id': rid, 'nome': str(candidato.get('nome') or '').casefold(),
            'gramas': gramas, 'necessidade': necessidade, 'original': necessidade,
            'limite': limite, 'alocado': 0,
        })

    # A precisão cobre a maior parte inteira e a menor casa decimal dos
    # valores de entrada, incluindo a contagem máxima possível de unidades.
    # Assim soma/subtração e divisão inteira não dependem do contexto global
    # (normalmente 28 dígitos) nem de uma tolerância que fabricaria massa.
    numeros = [massa]
    for candidato in validos:
        numeros.extend((candidato['gramas'], candidato['necessidade']))
    maior_inteiro = max([1, *(n.adjusted() + 1 for n in numeros if n)])
    menor_casa = min(0, *(n.as_tuple().exponent for n in numeros))
    with localcontext() as contexto:
        contexto.prec = max(28, maior_inteiro - menor_casa + 16)
        return _distribuir(massa, validos)


def _distribuir(massa, candidatos):
    restante = massa
    por_id = {c['id']: c for c in candidatos}

    def chave(c):
        return (-c['necessidade'], c['nome'], c['id'])

    def cabem(c):
        quantidade = int(restante // c['gramas'])
        if c['limite'] is not None:
            quantidade = min(quantidade, c['limite'] - c['alocado'])
        return max(0, quantidade)

    def alocar(c, quantidade):
        nonlocal restante
        c['alocado'] += quantidade
        c['necessidade'] = max(Decimal(0), c['necessidade'] - quantidade)
        restante -= c['gramas'] * quantidade

    fila = [chave(c) for c in candidatos if cabem(c)]
    heapq.heapify(fila)
    while fila:
        _necessidade, _nome, rid = heapq.heappop(fila)
        candidato = por_id[rid]
        capacidade = cabem(candidato)
        if not capacidade:
            continue
        # Massa só diminui: quem deixou de caber nunca voltará a ser elegível.
        while fila and not cabem(por_id[fila[0][2]]):
            heapq.heappop(fila)
        quantidade = int(candidato['necessidade'].to_integral_value(
            rounding=ROUND_CEILING))
        if fila:
            segundo = por_id[fila[0][2]]
            diferenca = candidato['necessidade'] - segundo['necessidade']
            desempate = (candidato['nome'], rid) < (segundo['nome'], segundo['id'])
            # Agrupa somente as unidades consecutivas em que este candidato
            # venceria a seleção individual, incluindo o desempate exato.
            ate_segundo = (int(diferenca.to_integral_value(rounding=ROUND_FLOOR)) + 1
                           if desempate else int(diferenca.to_integral_value(
                               rounding=ROUND_CEILING)))
            quantidade = min(quantidade, ate_segundo)
        alocar(candidato, min(capacidade, quantidade))
        if candidato['necessidade'] > 0 and cabem(candidato):
            heapq.heappush(fila, chave(candidato))

    # A demanda que ainda falta não cabe ou atingiu seu teto. Para preencher
    # o batimento com produtos, usa a maior necessidade original entre os
    # candidatos que ainda aceitam unidades, sem ultrapassar seus limites.
    for candidato in sorted(candidatos, key=lambda c: (-c['original'], c['nome'], c['id'])):
        quantidade = cabem(candidato)
        if quantidade:
            alocar(candidato, quantidade)

    return {
        'alocacoes': {c['id']: c['alocado'] for c in sorted(candidatos, key=lambda c: c['id'])
                      if c['alocado']},
        'restante_g': _texto_decimal(restante),
    }
