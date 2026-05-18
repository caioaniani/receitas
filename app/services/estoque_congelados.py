"""Balanco/inventario do estoque de congelados (EstoqueProducao).

Diferente de /congelados/entrada (soma) e /congelados/ajuste (subtrai),
o balanco SOBRESCREVE a quantidade pra refletir a contagem fisica.
Cria MovEstoqueProducao com tipo='balanco_entrada' ou 'balanco_saida'
e quantidade=|delta| pra auditar a diferenca entre o sistema e a contagem.
"""
import re
import unicodedata

from app.extensions import db
from app.models import EstoqueProducao, MovEstoqueProducao, Produto, Receita


# Abreviacoes comuns que aparecem em listas manuscritas — expandidas
# antes do fuzzy match. Tudo minusculo, sem acento.
EXPANSOES = {
    'cro': 'croissant',
    'tra': 'tradicional',
    'trd': 'mini',
    'int': 'integral',
    'gra': 'graos',
}


def _ascii(s):
    """Lowercase + sem acento, pra match insensivel a acentos/maiusculas."""
    if not s:
        return ''
    nf = unicodedata.normalize('NFD', s)
    return ''.join(c for c in nf if unicodedata.category(c) != 'Mn').lower().strip()


def _expandir_abreviacoes(nome_ascii):
    """Substitui tokens conhecidos: 'cro' -> 'croissant', etc."""
    if not nome_ascii:
        return nome_ascii
    tokens = re.split(r'(\s+)', nome_ascii)
    return ''.join(EXPANSOES.get(t, t) if t.strip() else t for t in tokens)


def parsear_linha(linha):
    """Converte uma linha em {linha, nome, quantidade} ou {linha, erro}.

    Aceita: 'Pao Frances: 570', 'pao frances=570', 'Pao 570',
    'Pao Frances - 570', 'Pao Frances    570'.
    Ignora linhas em branco e comentarios (#).
    Aceita separadores de milhar: '2.060' -> 2060, '2,060' -> 2060.
    """
    linha = (linha or '').strip()
    if not linha or linha.startswith('#'):
        return None
    # tenta separador explicito
    m = re.split(r'\s*[:=\-—]\s*', linha, maxsplit=1)
    if len(m) == 2 and m[1]:
        nome, qtd_raw = m[0].strip(), m[1].strip()
    else:
        # fallback: ultimo numero da string e a quantidade
        m2 = re.match(r'^(.+?)\s+([\d.,]+)\s*$', linha)
        if not m2:
            return {'linha': linha, 'erro': 'formato'}
        nome, qtd_raw = m2.group(1).strip(), m2.group(2)
    if not nome:
        return {'linha': linha, 'erro': 'sem_nome'}
    qtd_clean = re.sub(r'[^\d]', '', qtd_raw)
    if not qtd_clean:
        return {'linha': linha, 'nome': nome, 'erro': 'sem_quantidade'}
    try:
        qtd = int(qtd_clean)
    except ValueError:
        return {'linha': linha, 'nome': nome, 'erro': 'quantidade_invalida'}
    if qtd < 0:
        return {'linha': linha, 'nome': nome, 'erro': 'negativo'}
    return {'linha': linha, 'nome': nome, 'quantidade': qtd}


def parsear_lista(texto):
    """Recebe texto multi-linha, retorna lista de itens parseados."""
    if not texto:
        return []
    out = []
    for linha in texto.splitlines():
        item = parsear_linha(linha)
        if item:
            out.append(item)
    return out


def _carregar_catalogo():
    """Carrega todas receitas/produtos + orfaos do estoque (EstoqueProducao
    sem receita/produto vinculado) + apelidos globais confirmados
    (LojaProdutoMap mapeado pra receita/produto). Apelidos sao globais —
    o mesmo apelido vinculado em /pedidos/estoque-loja vale aqui."""
    from app.models import LojaProdutoMap
    receitas = [(r.id, r.nome, _ascii(r.nome)) for r in Receita.query.all()]
    produtos = [(p.id, p.nome, _ascii(p.nome)) for p in Produto.query.all()]
    orfaos = [
        (ep.id, ep.nome_pendente, _ascii(ep.nome_pendente))
        for ep in EstoqueProducao.query.filter(
            EstoqueProducao.nome_pendente.isnot(None),
            EstoqueProducao.receita_id.is_(None),
            EstoqueProducao.produto_id.is_(None),
        ).all()
        if ep.nome_pendente
    ]
    apelidos = []
    for mp in LojaProdutoMap.query.filter(
        LojaProdutoMap.confirmado_em.isnot(None),
        LojaProdutoMap.ignorar.is_(False),
    ).all():
        # MP nao se aplica aqui (congelados so vincula receita/produto).
        if mp.receita_id:
            apelidos.append((mp.nome_digitado, _ascii(mp.nome_digitado),
                              'receita', mp.receita_id,
                              mp.receita.nome if mp.receita else mp.nome_digitado))
        elif mp.produto_id:
            apelidos.append((mp.nome_digitado, _ascii(mp.nome_digitado),
                              'produto', mp.produto_id,
                              mp.produto.nome if mp.produto else mp.nome_digitado))
    return receitas, produtos, orfaos, apelidos


def _matches_para(nome, receitas, produtos, orfaos=(), apelidos=()):
    """Resolve 1 nome contra o catalogo carregado.

    Estrategias em ordem:
    0. Apelido global confirmado (LojaProdutoMap) — match exato ascii
    1. Match exato no catalogo (ascii)
    2. Substring direta (ascii)
    3. Substring com abreviacoes expandidas (CRO -> croissant, TRD -> mini)

    Em todas as fases, orfaos (EstoqueProducao pendentes) sao candidatos
    junto com receitas e produtos. Retorna [{tipo, id, nome, match}] (max 10).
    """
    alvo = _ascii(nome)
    if not alvo:
        return []
    out, seen = [], set()

    def add(tipo, _id, nome_real, kind):
        key = (tipo, _id)
        if key in seen:
            return
        seen.add(key)
        out.append({'tipo': tipo, 'id': _id, 'nome': nome_real, 'match': kind})

    # 0. apelido global confirmado — match exato ascii vira atalho direto
    for ap_digitado, ap_asc, ap_tipo, ap_id, ap_nome in apelidos:
        if ap_asc == alvo:
            add(ap_tipo, ap_id, ap_nome, 'apelido')
    if out:
        return out

    # 1. exato — orfaos primeiro pra colar nele se for reaplicar o balanco
    for oid, onome, oasc in orfaos:
        if oasc == alvo:
            add('pendente', oid, onome, 'exato')
    for rid, rnome, rasc in receitas:
        if rasc == alvo:
            add('receita', rid, rnome, 'exato')
    for pid, pnome, pasc in produtos:
        if pasc == alvo:
            add('produto', pid, pnome, 'exato')
    if out:
        return out

    # 2. substring direta — ambos os sentidos (alvo dentro do catalogo OU vice-versa)
    for oid, onome, oasc in orfaos:
        if alvo in oasc or oasc in alvo:
            add('pendente', oid, onome, 'fuzzy')
    for rid, rnome, rasc in receitas:
        if alvo in rasc or rasc in alvo:
            add('receita', rid, rnome, 'fuzzy')
    for pid, pnome, pasc in produtos:
        if alvo in pasc or pasc in alvo:
            add('produto', pid, pnome, 'fuzzy')
    if out:
        return out[:10]

    # 3. com abreviacoes expandidas
    expandido = _expandir_abreviacoes(alvo)
    if expandido != alvo:
        for oid, onome, oasc in orfaos:
            if expandido in oasc or oasc in expandido:
                add('pendente', oid, onome, 'fuzzy')
        for rid, rnome, rasc in receitas:
            if expandido in rasc or rasc in expandido:
                add('receita', rid, rnome, 'fuzzy')
        for pid, pnome, pasc in produtos:
            if expandido in pasc or pasc in expandido:
                add('produto', pid, pnome, 'fuzzy')

    return out[:10]


def sugerir_para_pendentes(estoques_pendentes):
    """Pra cada EstoqueProducao pendente, retorna {ep_id: melhor_match}.

    Usado em /pedidos/congelados pra pre-selecionar o dropdown do
    vincular. Match pode ser receita ou produto — MP nao se aplica aqui.
    """
    if not estoques_pendentes:
        return {}
    receitas, produtos, _, apelidos = _carregar_catalogo()
    # Filtra apelidos que apontam pra MP (nao aplicavel em congelados).
    apelidos = [a for a in apelidos if a[2] in ('receita', 'produto')]
    out = {}
    for ep in estoques_pendentes:
        nome = ep.nome_pendente if hasattr(ep, 'nome_pendente') else None
        if not nome:
            continue
        # Nao passa orfaos pra nao sugerir o proprio pendente.
        matches = _matches_para(nome, receitas, produtos, (), apelidos)
        if matches:
            out[ep.id] = matches[0]
    return out


def resolver_lista(linhas_parseadas):
    """Enriquece cada item com matches, resolvido (primeiro match),
    estoque_atual e delta. Itens com 'erro' passam intactos.

    Itens sem match nao sao mais 'erro' — entram no balanco como pendentes
    e ganham linha em EstoqueProducao com nome_pendente preenchido."""
    receitas, produtos, orfaos, apelidos = _carregar_catalogo()
    enriq = []
    for item in linhas_parseadas:
        if item.get('erro'):
            enriq.append(item)
            continue
        matches = _matches_para(item['nome'], receitas, produtos, orfaos, apelidos)
        resolvido = matches[0] if matches else None
        atual = 0
        if resolvido:
            if resolvido['tipo'] == 'pendente':
                ep = EstoqueProducao.query.get(resolvido['id'])
            else:
                ep = EstoqueProducao.query.filter_by(
                    receita_id=resolvido['id'] if resolvido['tipo'] == 'receita' else None,
                    produto_id=resolvido['id'] if resolvido['tipo'] == 'produto' else None,
                ).first()
            atual = ep.quantidade if ep else 0
        delta = item['quantidade'] - atual
        enriq.append({
            **item,
            'matches': matches,
            'resolvido': resolvido,
            'estoque_atual': atual,
            'delta': delta,
        })
    return enriq


def aplicar_balanco(itens_resolvidos, user, referencia=None):
    """Sobrescreve a quantidade de cada item resolvido e registra MovEstoqueProducao.

    - tipo='balanco_entrada' se nova > anterior, 'balanco_saida' se menor.
    - quantidade da mov = |delta|; nao registra movimento se delta == 0.
    - Itens sem match no catalogo (receita/produto/orfao existente) sao
      gravados como EstoqueProducao pendente — guardam a contagem fisica e
      ficam disponiveis pra serem vinculados a uma receita/produto depois.
    - So entram em 'ignorados' linhas com erro de parse.

    Retorna {aplicados:[{nome,tipo,anterior,novo,delta}], ignorados:[{linha,motivo}]}.
    """
    ref = (referencia or 'Balanço de inventário').strip()
    aplicados = []
    ignorados = []

    for item in itens_resolvidos:
        if item.get('erro'):
            ignorados.append({'linha': item.get('linha', '?'), 'motivo': item['erro']})
            continue
        try:
            nova_qtd = int(item['quantidade'])
        except (KeyError, TypeError, ValueError):
            ignorados.append({'linha': item.get('linha', '?'), 'motivo': 'quantidade_invalida'})
            continue

        resolvido = item.get('resolvido')
        ep = None

        if resolvido and resolvido.get('id'):
            if resolvido['tipo'] == 'pendente':
                ep = EstoqueProducao.query.get(resolvido['id'])
            else:
                ep = EstoqueProducao.query.filter_by(
                    receita_id=resolvido['id'] if resolvido['tipo'] == 'receita' else None,
                    produto_id=resolvido['id'] if resolvido['tipo'] == 'produto' else None,
                ).first()
                if not ep:
                    ep = EstoqueProducao(
                        receita_id=resolvido['id'] if resolvido['tipo'] == 'receita' else None,
                        produto_id=resolvido['id'] if resolvido['tipo'] == 'produto' else None,
                        quantidade=0,
                    )
                    db.session.add(ep)
                    db.session.flush()
            tipo_resultado = resolvido['tipo']
            nome_resultado = resolvido['nome']
        else:
            # Sem match — cria orfao guardando o nome digitado.
            nome_digitado = (item.get('nome') or '').strip()
            if not nome_digitado:
                ignorados.append({'linha': item.get('linha', '?'), 'motivo': 'sem_nome'})
                continue
            ep = EstoqueProducao(
                nome_pendente=nome_digitado,
                quantidade=0,
            )
            db.session.add(ep)
            db.session.flush()
            tipo_resultado = 'pendente'
            nome_resultado = nome_digitado

        anterior = ep.quantidade or 0
        delta = nova_qtd - anterior
        ep.quantidade = nova_qtd

        if delta != 0:
            db.session.add(MovEstoqueProducao(
                estoque_producao_id=ep.id,
                tipo='balanco_entrada' if delta > 0 else 'balanco_saida',
                quantidade=abs(delta),
                referencia=f'{ref} (era {anterior}, ficou {nova_qtd})',
                usuario_id=getattr(user, 'id', None),
            ))

        aplicados.append({
            'nome': nome_resultado,
            'tipo': tipo_resultado,
            'anterior': anterior,
            'novo': nova_qtd,
            'delta': delta,
        })

    if aplicados:
        db.session.commit()

    return {'aplicados': aplicados, 'ignorados': ignorados}
