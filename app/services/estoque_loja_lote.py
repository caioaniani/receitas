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

from sqlalchemy import func

from app.extensions import db
from app.models import EstoqueLoja, LojaDebito, LojaProdutoMap, MateriaPrima, MovEstoqueLoja, Produto, Receita

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
    daquela loja (EstoqueLoja com nome_pendente) + apelidos globais
    confirmados (LojaProdutoMap mapeado)."""
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
    # Apelidos globais — match exato em LojaProdutoMap confirmado vira atalho.
    apelidos = []
    for mp in LojaProdutoMap.query.filter(
        LojaProdutoMap.confirmado_em.isnot(None),
        LojaProdutoMap.ignorar.is_(False),
    ).all():
        if mp.receita_id:
            apelidos.append((mp.nome_digitado, _ascii(mp.nome_digitado),
                              'receita', mp.receita_id,
                              mp.receita.nome if mp.receita else mp.nome_digitado))
        elif mp.produto_id:
            apelidos.append((mp.nome_digitado, _ascii(mp.nome_digitado),
                              'produto', mp.produto_id,
                              mp.produto.nome if mp.produto else mp.nome_digitado))
        elif mp.materia_prima_id:
            apelidos.append((mp.nome_digitado, _ascii(mp.nome_digitado),
                              'mp', mp.materia_prima_id,
                              mp.materia_prima.nome if mp.materia_prima else mp.nome_digitado))
    return receitas, produtos, materias, orfaos, apelidos


def _matches_para(nome, receitas, produtos, materias, orfaos, apelidos=()):
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

    # 0. apelido global confirmado — match exato no nome digitado vira atalho.
    for ap_digitado, ap_asc, ap_tipo, ap_id, ap_nome in apelidos:
        if ap_asc == alvo:
            add(ap_tipo, ap_id, ap_nome, 'apelido')
    if out:
        return out

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


def sugerir_para_pendentes(estoques_pendentes):
    """Pra cada EstoqueLoja pendente, retorna {ep_id: melhor_match}.

    Usado em /pedidos/estoque-loja pra pre-selecionar o dropdown da
    vinculacao — admin so confirma com 1 clique em vez de buscar na lista.
    Retorna {} pra item sem nome_pendente. Match pode ser receita/produto/mp.
    """
    if not estoques_pendentes:
        return {}
    receitas = [(r.id, r.nome, _ascii(r.nome)) for r in Receita.query.all()]
    produtos = [(p.id, p.nome, _ascii(p.nome)) for p in Produto.query.all()]
    materias = [(m.id, m.nome, _ascii(m.nome)) for m in MateriaPrima.query.all()]
    apelidos = []
    for mp in LojaProdutoMap.query.filter(
        LojaProdutoMap.confirmado_em.isnot(None),
        LojaProdutoMap.ignorar.is_(False),
    ).all():
        if mp.receita_id:
            apelidos.append((mp.nome_digitado, _ascii(mp.nome_digitado),
                              'receita', mp.receita_id,
                              mp.receita.nome if mp.receita else mp.nome_digitado))
        elif mp.produto_id:
            apelidos.append((mp.nome_digitado, _ascii(mp.nome_digitado),
                              'produto', mp.produto_id,
                              mp.produto.nome if mp.produto else mp.nome_digitado))
        elif mp.materia_prima_id:
            apelidos.append((mp.nome_digitado, _ascii(mp.nome_digitado),
                              'mp', mp.materia_prima_id,
                              mp.materia_prima.nome if mp.materia_prima else mp.nome_digitado))
    out = {}
    for ep in estoques_pendentes:
        nome = ep.nome_pendente if hasattr(ep, 'nome_pendente') else None
        if not nome:
            continue
        matches = _matches_para(nome, receitas, produtos, materias, (), apelidos)
        if matches:
            out[ep.id] = matches[0]
    return out


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
    receitas, produtos, materias, orfaos, apelidos = _carregar_catalogo(loja_id)
    enriq = []
    for item in linhas_parseadas:
        if item.get('erro'):
            enriq.append(item)
            continue
        matches = _matches_para(item['nome'], receitas, produtos, materias, orfaos, apelidos)
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


def _get_or_create_map(nome_digitado):
    """Acha LojaProdutoMap por nome (case-insensitive) ou cria novo pendente."""
    nome = (nome_digitado or '').strip()
    if not nome:
        return None
    nome_lower = nome.lower()
    mp = LojaProdutoMap.query.filter(
        func.lower(LojaProdutoMap.nome_digitado) == nome_lower
    ).first()
    if mp:
        return mp
    mp = LojaProdutoMap(nome_digitado=nome)
    db.session.add(mp)
    db.session.flush()
    return mp


def resolver_lista_saida(linhas_parseadas, loja_id):
    """Resolve usando LojaProdutoMap (cria pendentes pra nomes novos).

    Retorna lista enriquecida com 'map_entry' (LojaProdutoMap),
    'estoque_atual', 'novo', 'faltou'.
    """
    enriq = []
    for item in linhas_parseadas:
        if item.get('erro'):
            enriq.append(item)
            continue
        nome = (item.get('nome') or '').strip()
        if not nome:
            enriq.append(item)
            continue
        mp = _get_or_create_map(nome)
        atual = 0
        novo = 0
        faltou = 0
        qtd = item.get('quantidade') or 0
        if mp and mp.estado == 'mapeado' and loja_id:
            filtro = {'loja_id': loja_id}
            if mp.receita_id:
                filtro['receita_id'] = mp.receita_id
            elif mp.produto_id:
                filtro['produto_id'] = mp.produto_id
            elif mp.materia_prima_id:
                filtro['materia_prima_id'] = mp.materia_prima_id
            el = EstoqueLoja.query.filter_by(**filtro).first()
            atual = (el.quantidade if el else 0) or 0
            qtd_efetiva = int((qtd * float(mp.fator_quantidade or 1.0)) + 1e-9)
            real = min(qtd_efetiva, atual)
            novo = atual - real
            faltou = max(0, qtd_efetiva - atual)
        enriq.append({
            **item,
            'map_entry': mp,
            'estoque_atual': atual,
            'novo': novo,
            'faltou': faltou,
        })
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
    return enriq


def aplicar_saida_lote(itens_resolvidos, loja_id, user, referencia=None):
    """Aplica saida usando LojaProdutoMap. Pendentes/ignorados sao pulados.

    Mapeados: subtrai qtd*fator do EstoqueLoja correspondente. Se estoque
    insuficiente, baixa ate 0 e registra 'venda_loja_sem_estoque' pra falta.
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
        mp = item.get('map_entry')
        if not mp:
            ignorados.append({'linha': item.get('linha', '?'), 'motivo': 'sem_map'})
            continue
        if mp.ignorar:
            ignorados.append({'linha': item.get('linha', '?'),
                              'nome': mp.nome_digitado, 'motivo': 'ignorado'})
            continue
        if mp.estado == 'pendente':
            ignorados.append({'linha': item.get('linha', '?'),
                              'nome': mp.nome_digitado, 'motivo': 'pendente'})
            continue

        fator = float(mp.fator_quantidade or 1.0)
        qtd_efetiva_float = qtd_sub * fator

        # Acumulador (igual SeruDebito/VndaDebito) — fracao soma proxima vez
        debito = LojaDebito.query.filter_by(
            loja_id=loja_id, loja_produto_map_id=mp.id).first()
        if not debito:
            debito = LojaDebito(loja_id=loja_id, loja_produto_map_id=mp.id,
                                 fracao_pendente=0.0)
            db.session.add(debito)
            db.session.flush()
        debito_total = (debito.fracao_pendente or 0.0) + qtd_efetiva_float
        inteiros = int(debito_total + 1e-9)
        debito.fracao_pendente = max(0.0, round(debito_total - inteiros, 6))

        if inteiros <= 0:
            ignorados.append({'linha': item.get('linha', '?'),
                              'nome': mp.nome_digitado,
                              'motivo': f'fracao_acumulando ({debito.fracao_pendente:g})'})
            continue

        filtro = {'loja_id': loja_id}
        if mp.receita_id:
            filtro['receita_id'] = mp.receita_id
        elif mp.produto_id:
            # Se Produto for cesta (tem ProdutoItens), desempacota e baixa
            # CADA componente individual em vez do produto inteiro. Loja so
            # tem os componentes em estoque — nao a cesta montada.
            from app.services.cestas import componentes_de_cesta
            produto = Produto.query.get(mp.produto_id)
            componentes_cesta = componentes_de_cesta(produto)

            if componentes_cesta:
                # Cesta: baixa cada componente, registra mov por componente.
                # Acumulador 'fracao_pendente' por componente ficaria mais
                # robusto, mas pra v1 arredondamos: qtd_componente = round(inteiros * qtd_no_item).
                # Suficiente pra padaria onde componentes sao inteiros (pao, croissant).
                componentes_baixados = []
                for col, item_id, nome_comp, qtd_no_item in componentes_cesta:
                    qtd_baixar = int(round(inteiros * qtd_no_item))
                    if qtd_baixar <= 0:
                        continue
                    filtro_c = {'loja_id': loja_id, col: item_id}
                    ep_c = EstoqueLoja.query.filter_by(**filtro_c).first()
                    if not ep_c:
                        ep_c = EstoqueLoja(**filtro_c, quantidade=0)
                        db.session.add(ep_c)
                        db.session.flush()
                    anterior_c = ep_c.quantidade or 0
                    real_c = min(qtd_baixar, anterior_c)
                    falta_c = qtd_baixar - real_c
                    ep_c.quantidade = anterior_c - real_c
                    if real_c > 0:
                        db.session.add(MovEstoqueLoja(
                            estoque_loja_id=ep_c.id, tipo='saida_lote',
                            quantidade=real_c,
                            referencia=(f'{ref} [{mp.nome_digitado} → cesta] '
                                        f'{nome_comp} (era {anterior_c}, ficou {ep_c.quantidade})'),
                            usuario_id=getattr(user, 'id', None),
                        ))
                    if falta_c > 0:
                        db.session.add(MovEstoqueLoja(
                            estoque_loja_id=ep_c.id, tipo='venda_loja_sem_estoque',
                            quantidade=falta_c,
                            referencia=(f'{ref} [{mp.nome_digitado} → cesta] '
                                        f'{nome_comp} — faltou {falta_c}'),
                            usuario_id=getattr(user, 'id', None),
                        ))
                    componentes_baixados.append(
                        f'{real_c}× {nome_comp}' + (f' ({falta_c} faltou)' if falta_c else ''))

                aplicados.append({
                    'nome': mp.nome_digitado,
                    'alvo': f'CESTA: {produto.nome}',
                    'tipo': 'cesta',
                    'anterior': '-',
                    'novo': '-',
                    'delta': f'desempacotado: {", ".join(componentes_baixados)}',
                    'faltou': 0,
                })
                continue  # ja registrou tudo, pula o fluxo normal abaixo

            # Produto normal (nao cesta) — fluxo padrao
            filtro['produto_id'] = mp.produto_id
        elif mp.materia_prima_id:
            filtro['materia_prima_id'] = mp.materia_prima_id
        else:
            ignorados.append({'linha': item.get('linha', '?'),
                              'nome': mp.nome_digitado, 'motivo': 'sem_alvo'})
            continue

        ep = EstoqueLoja.query.filter_by(**filtro).first()
        if not ep:
            ep = EstoqueLoja(**filtro, quantidade=0)
            db.session.add(ep)
            db.session.flush()

        anterior = ep.quantidade or 0
        real = min(inteiros, anterior)
        falta = inteiros - real
        ep.quantidade = anterior - real

        if real > 0:
            db.session.add(MovEstoqueLoja(
                estoque_loja_id=ep.id, tipo='saida_lote', quantidade=real,
                referencia=f'{ref} [{mp.nome_digitado}] (era {anterior}, ficou {ep.quantidade})',
                usuario_id=getattr(user, 'id', None),
            ))
        if falta > 0:
            db.session.add(MovEstoqueLoja(
                estoque_loja_id=ep.id, tipo='venda_loja_sem_estoque', quantidade=falta,
                referencia=f'{ref} [{mp.nome_digitado}] — faltou {falta}',
                usuario_id=getattr(user, 'id', None),
            ))

        aplicados.append({
            'nome': mp.nome_digitado,
            'alvo': mp.alvo_nome,
            'tipo': mp.alvo_tipo,
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
