"""Entrada em lote pro estoque de loja (EstoqueLoja).

Espelha estoque_congelados.py mas com semantica de SOMA:
cada item soma na quantidade existente em vez de sobrescrever.

Itens sem match no catalogo (receita/produto/MP/orfao existente) entram
como EstoqueLoja com nome_pendente preenchido — depois o admin vincula
a um item cadastrado via /pedidos/estoque-loja/vincular.

Saida em lote (manual, lojas sem PDV API) usa LojaProdutoMap pra lembrar
vinculacoes — vincula um nome uma vez, vale pra sempre. Espelha SeruProdutoMap.
"""
import re
import unicodedata
from datetime import datetime

from sqlalchemy import func

from app.extensions import db
from app.models import (EstoqueLoja, MovEstoqueLoja,
                        Receita, Produto, MateriaPrima, LojaProdutoMap)


EXPANSOES = {
    'cro': 'croissant',
    'tra': 'tradicional',
    'trd': 'mini',
    'int': 'integral',
    'gra': 'graos',
}


def _ascii(s):
    if not s:
        return ''
    nf = unicodedata.normalize('NFD', s)
    return ''.join(c for c in nf if unicodedata.category(c) != 'Mn').lower().strip()


def _expandir_abreviacoes(nome_ascii):
    if not nome_ascii:
        return nome_ascii
    tokens = re.split(r'(\s+)', nome_ascii)
    return ''.join(EXPANSOES.get(t, t) if t.strip() else t for t in tokens)


def parsear_linha(linha):
    """Mesmo parser do estoque_congelados — 'Nome: qtd', 'Nome=qtd', etc."""
    linha = (linha or '').strip()
    if not linha or linha.startswith('#'):
        return None
    m = re.split(r'\s*[:=\-—]\s*', linha, maxsplit=1)
    if len(m) == 2 and m[1]:
        nome, qtd_raw = m[0].strip(), m[1].strip()
    else:
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
    if qtd <= 0:
        return {'linha': linha, 'nome': nome, 'erro': 'quantidade_invalida'}
    return {'linha': linha, 'nome': nome, 'quantidade': qtd}


def parsear_lista(texto):
    if not texto:
        return []
    out = []
    for linha in texto.splitlines():
        item = parsear_linha(linha)
        if item:
            out.append(item)
    return out


def _carregar_catalogo(loja_id):
    """Catalogo de match: receitas + produtos + materias-primas + orfaos
    daquela loja (EstoqueLoja com nome_pendente)."""
    receitas = [(r.id, r.nome, _ascii(r.nome)) for r in Receita.query.all()]
    produtos = [(p.id, p.nome, _ascii(p.nome)) for p in Produto.query.all()]
    materias = [(m.id, m.nome, _ascii(m.nome)) for m in MateriaPrima.query.all()]
    orfaos = []
    if loja_id:
        orfaos = [
            (ep.id, ep.nome_pendente, _ascii(ep.nome_pendente))
            for ep in EstoqueLoja.query.filter(
                EstoqueLoja.loja_id == loja_id,
                EstoqueLoja.nome_pendente.isnot(None),
                EstoqueLoja.receita_id.is_(None),
                EstoqueLoja.produto_id.is_(None),
                EstoqueLoja.materia_prima_id.is_(None),
            ).all()
            if ep.nome_pendente
        ]
    return receitas, produtos, materias, orfaos


def _matches_para(nome, receitas, produtos, materias, orfaos):
    """Retorna [{tipo, id, nome, match}]. tipo em receita/produto/mp/pendente."""
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

    # 1. exato
    for oid, onome, oasc in orfaos:
        if oasc == alvo:
            add('pendente', oid, onome, 'exato')
    for rid, rnome, rasc in receitas:
        if rasc == alvo:
            add('receita', rid, rnome, 'exato')
    for pid, pnome, pasc in produtos:
        if pasc == alvo:
            add('produto', pid, pnome, 'exato')
    for mid, mnome, masc in materias:
        if masc == alvo:
            add('mp', mid, mnome, 'exato')
    if out:
        return out

    # 2. substring
    for oid, onome, oasc in orfaos:
        if alvo in oasc or oasc in alvo:
            add('pendente', oid, onome, 'fuzzy')
    for rid, rnome, rasc in receitas:
        if alvo in rasc or rasc in alvo:
            add('receita', rid, rnome, 'fuzzy')
    for pid, pnome, pasc in produtos:
        if alvo in pasc or pasc in alvo:
            add('produto', pid, pnome, 'fuzzy')
    for mid, mnome, masc in materias:
        if alvo in masc or masc in alvo:
            add('mp', mid, mnome, 'fuzzy')
    if out:
        return out[:10]

    # 3. abreviacoes
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
        for mid, mnome, masc in materias:
            if expandido in masc or masc in expandido:
                add('mp', mid, mnome, 'fuzzy')

    return out[:10]


def _filtro_para_resolvido(loja_id, resolvido):
    """Monta o filtro de EstoqueLoja.filter_by() pra um resolvido."""
    filtro = {'loja_id': loja_id}
    if resolvido['tipo'] == 'receita':
        filtro['receita_id'] = resolvido['id']
    elif resolvido['tipo'] == 'produto':
        filtro['produto_id'] = resolvido['id']
    elif resolvido['tipo'] == 'mp':
        filtro['materia_prima_id'] = resolvido['id']
    return filtro


def resolver_lista(linhas_parseadas, loja_id):
    receitas, produtos, materias, orfaos = _carregar_catalogo(loja_id)
    enriq = []
    for item in linhas_parseadas:
        if item.get('erro'):
            enriq.append(item)
            continue
        matches = _matches_para(item['nome'], receitas, produtos, materias, orfaos)
        resolvido = matches[0] if matches else None
        atual = 0
        if resolvido and loja_id:
            if resolvido['tipo'] == 'pendente':
                ep = EstoqueLoja.query.get(resolvido['id'])
            else:
                ep = EstoqueLoja.query.filter_by(
                    **_filtro_para_resolvido(loja_id, resolvido)).first()
            atual = ep.quantidade if ep else 0
        enriq.append({
            **item,
            'matches': matches,
            'resolvido': resolvido,
            'estoque_atual': atual,
            'novo': atual + item['quantidade'],  # SOMA
        })
    return enriq


def aplicar_saida_lote(itens_resolvidos, loja_id, user, referencia=None):
    """SUBTRAI a quantidade de cada item do EstoqueLoja correspondente.

    Itens sem match no catalogo sao IGNORADOS (saida nao cria pendentes —
    usuario precisa cadastrar antes). Se estoque atual < qtd saida, baixa
    ate 0 e registra mov 'venda_loja_sem_estoque' pra falta.
    """
    ref = (referencia or 'Saida em lote').strip()
    aplicados = []
    ignorados = []

    if not loja_id:
        return {'aplicados': [], 'ignorados': [{'linha': '*', 'motivo': 'sem_loja'}]}

    for item in itens_resolvidos:
        if item.get('erro'):
            ignorados.append({'linha': item.get('linha', '?'), 'motivo': item['erro']})
            continue
        try:
            qtd_sub = int(item['quantidade'])
        except (KeyError, TypeError, ValueError):
            ignorados.append({'linha': item.get('linha', '?'), 'motivo': 'quantidade_invalida'})
            continue
        if qtd_sub <= 0:
            ignorados.append({'linha': item.get('linha', '?'), 'motivo': 'quantidade_invalida'})
            continue

        resolvido = item.get('resolvido')
        if not resolvido or not resolvido.get('id'):
            ignorados.append({
                'linha': item.get('linha', '?'),
                'nome': item.get('nome', ''),
                'motivo': 'sem_match',
            })
            continue

        if resolvido['tipo'] == 'pendente':
            ep = EstoqueLoja.query.get(resolvido['id'])
        else:
            filtro = _filtro_para_resolvido(loja_id, resolvido)
            ep = EstoqueLoja.query.filter_by(**filtro).first()
            if not ep:
                ep = EstoqueLoja(**filtro, quantidade=0)
                db.session.add(ep)
                db.session.flush()

        anterior = ep.quantidade or 0
        real = min(qtd_sub, anterior)
        falta = qtd_sub - real
        ep.quantidade = anterior - real

        if real > 0:
            db.session.add(MovEstoqueLoja(
                estoque_loja_id=ep.id,
                tipo='saida_lote',
                quantidade=real,
                referencia=f'{ref} (era {anterior}, ficou {ep.quantidade})',
                usuario_id=getattr(user, 'id', None),
            ))
        if falta > 0:
            db.session.add(MovEstoqueLoja(
                estoque_loja_id=ep.id,
                tipo='venda_loja_sem_estoque',
                quantidade=falta,
                referencia=f'{ref} — faltou {falta} (tinha {anterior})',
                usuario_id=getattr(user, 'id', None),
            ))

        aplicados.append({
            'nome': resolvido['nome'],
            'tipo': resolvido['tipo'],
            'anterior': anterior,
            'novo': ep.quantidade,
            'delta': -real,
            'faltou': falta,
        })

    if aplicados:
        db.session.commit()

    return {'aplicados': aplicados, 'ignorados': ignorados}


def aplicar_entrada_lote(itens_resolvidos, loja_id, user, referencia=None):
    """SOMA a quantidade de cada item ao EstoqueLoja correspondente.

    Itens sem match viram EstoqueLoja pendente. Cria movimento de auditoria
    tipo='entrada_lote' com a quantidade SOMADA.
    """
    ref = (referencia or 'Entrada em lote').strip()
    aplicados = []
    ignorados = []

    if not loja_id:
        return {'aplicados': [], 'ignorados': [{'linha': '*', 'motivo': 'sem_loja'}]}

    for item in itens_resolvidos:
        if item.get('erro'):
            ignorados.append({'linha': item.get('linha', '?'), 'motivo': item['erro']})
            continue
        try:
            qtd_add = int(item['quantidade'])
        except (KeyError, TypeError, ValueError):
            ignorados.append({'linha': item.get('linha', '?'), 'motivo': 'quantidade_invalida'})
            continue
        if qtd_add <= 0:
            ignorados.append({'linha': item.get('linha', '?'), 'motivo': 'quantidade_invalida'})
            continue

        resolvido = item.get('resolvido')
        ep = None

        if resolvido and resolvido.get('id'):
            if resolvido['tipo'] == 'pendente':
                ep = EstoqueLoja.query.get(resolvido['id'])
            else:
                filtro = _filtro_para_resolvido(loja_id, resolvido)
                ep = EstoqueLoja.query.filter_by(**filtro).first()
                if not ep:
                    ep = EstoqueLoja(**filtro, quantidade=0)
                    db.session.add(ep)
                    db.session.flush()
            tipo_resultado = resolvido['tipo']
            nome_resultado = resolvido['nome']
        else:
            nome_digitado = (item.get('nome') or '').strip()
            if not nome_digitado:
                ignorados.append({'linha': item.get('linha', '?'), 'motivo': 'sem_nome'})
                continue
            ep = EstoqueLoja(loja_id=loja_id, nome_pendente=nome_digitado, quantidade=0)
            db.session.add(ep)
            db.session.flush()
            tipo_resultado = 'pendente'
            nome_resultado = nome_digitado

        anterior = ep.quantidade or 0
        ep.quantidade = anterior + qtd_add

        db.session.add(MovEstoqueLoja(
            estoque_loja_id=ep.id,
            tipo='entrada_lote',
            quantidade=qtd_add,
            referencia=f'{ref} (era {anterior}, ficou {ep.quantidade})',
            usuario_id=getattr(user, 'id', None),
        ))

        aplicados.append({
            'nome': nome_resultado,
            'tipo': tipo_resultado,
            'anterior': anterior,
            'novo': ep.quantidade,
            'delta': qtd_add,
        })

    if aplicados:
        db.session.commit()

    return {'aplicados': aplicados, 'ignorados': ignorados}
