"""Agregacao de itens vendidos da Seru.

Para cada produto vendido num intervalo, calcula:
- quantidade total vendida
- faturamento total
- numero de pedidos distintos
- match no catalogo local (Receita ou Produto), por fuzzy
- estado do mapeamento Seru (SeruProdutoMap): mapeado/ignorado/pendente/sem_map

Filtros: intervalo de datas BRT e (opcional) nome da loja Seru
(o campo 'company.name' do pedido — que e o que a Seru chama de loja).
"""
import unicodedata

from app.models import Produto, Receita, SeruProdutoMap
from app.services import seru


def _ascii(s):
    if not s:
        return ''
    nf = unicodedata.normalize('NFD', s)
    return ''.join(c for c in nf if unicodedata.category(c) != 'Mn').lower().strip()


def _carregar_catalogo():
    receitas = [(r.id, r.nome, _ascii(r.nome)) for r in Receita.query.all()]
    produtos = [(p.id, p.nome, _ascii(p.nome))
                for p in Produto.query.filter_by(ativo=True).all()]
    return receitas, produtos


def _match_local(nome, receitas, produtos):
    """Retorna {'tipo': 'receita'|'produto', 'id', 'nome', 'kind': 'exato'|'fuzzy'}
    ou None se nao houver match razoavel."""
    alvo = _ascii(nome)
    if not alvo:
        return None
    # 1. exato
    for rid, rnome, rasc in receitas:
        if rasc == alvo:
            return {'tipo': 'receita', 'id': rid, 'nome': rnome, 'kind': 'exato'}
    for pid, pnome, pasc in produtos:
        if pasc == alvo:
            return {'tipo': 'produto', 'id': pid, 'nome': pnome, 'kind': 'exato'}
    # 2. substring
    for rid, rnome, rasc in receitas:
        if alvo in rasc or rasc in alvo:
            return {'tipo': 'receita', 'id': rid, 'nome': rnome, 'kind': 'fuzzy'}
    for pid, pnome, pasc in produtos:
        if alvo in pasc or pasc in alvo:
            return {'tipo': 'produto', 'id': pid, 'nome': pnome, 'kind': 'fuzzy'}
    return None


def _nome_loja(pedido):
    """Extrai o nome da loja Seru do pedido (campo 'company.name' tipicamente)."""
    c = pedido.get('company')
    if isinstance(c, dict):
        return (c.get('name') or c.get('label') or '').strip()
    if isinstance(c, str):
        return c.strip()
    return ''


def agregar_itens(data_inicial, data_final, loja_seru=None,
                  expandir_dias_frente=0):
    """Pega pedidos da Seru no intervalo, agrega por nome de produto.

    Retorna:
        {
          'inicio': iso, 'fim': iso, 'loja': str|None,
          'total_pedidos': N, 'total_itens_vendidos': N,
          'faturamento_total': float,
          'produtos': [{nome, qtd, faturamento, n_pedidos, pct_faturamento, match}],
          'sem_match_count': N,
          'lojas_no_intervalo': [lojaA, lojaB, ...],  # pra preencher dropdown
        }
    """
    receitas, produtos = _carregar_catalogo()

    pedidos = seru.listar_pedidos_completo(
        data_inicial, data_final, expandir_dias_frente=expandir_dias_frente)

    # Filtra por createdAt no intervalo BRT + (opcional) loja
    lojas_vistas = set()
    pedidos_filtrados = []
    for p in pedidos:
        if not isinstance(p, dict):
            continue
        if p.get('canceledAt'):
            continue
        d = seru.data_local(p.get('createdAt'))
        if not d or not (data_inicial <= d <= data_final):
            continue
        ln = _nome_loja(p)
        if ln:
            lojas_vistas.add(ln)
        if loja_seru and ln != loja_seru:
            continue
        pedidos_filtrados.append(p)

    # Agrega por nome do produto Seru
    agg = {}  # nome -> {qtd, faturamento, n_pedidos (set de ids), sku}
    for p in pedidos_filtrados:
        pid = p.get('id') or p.get('orderNumber') or p.get('code')
        for it in seru.extrair_itens(p):
            if it['cancelado']:
                continue
            nome = it['nome']
            if nome not in agg:
                agg[nome] = {'qtd': 0.0, 'faturamento': 0.0,
                             'pedidos': set(), 'sku': it['sku']}
            agg[nome]['qtd'] += it['qtd']
            agg[nome]['faturamento'] += it['total']
            if pid is not None:
                agg[nome]['pedidos'].add(pid)

    faturamento_total = sum(v['faturamento'] for v in agg.values())
    total_itens = sum(v['qtd'] for v in agg.values())

    # Index dos SeruProdutoMap pra mostrar estado nas linhas.
    maps = {m.seru_nome: m for m in SeruProdutoMap.query.filter(
        SeruProdutoMap.seru_nome.in_(list(agg.keys()))).all()}

    produtos_lista = []
    sem_match = 0
    pendentes = 0
    for nome, v in agg.items():
        match = _match_local(nome, receitas, produtos)
        if not match:
            sem_match += 1
        # Estado do mapeamento Seru (autoritativo pra auto-baixa).
        m = maps.get(nome)
        if m:
            estado_map = m.estado  # mapeado/ignorado/pendente
            mapeado_para = {
                'tipo': 'receita' if m.receita_id else ('produto' if m.produto_id else None),
                'id': m.receita_id or m.produto_id,
                'nome': m.alvo_nome,
            } if m.estado == 'mapeado' else None
            map_id = m.id
            fator = float(m.fator_quantidade or 1.0)
        else:
            estado_map = 'sem_map'  # ainda nao foi visto numa sync
            mapeado_para = None
            map_id = None
            fator = 1.0
        if estado_map in ('pendente', 'sem_map'):
            pendentes += 1
        produtos_lista.append({
            'nome': nome,
            'sku': v['sku'],
            'qtd': v['qtd'],
            'faturamento': round(v['faturamento'], 2),
            'n_pedidos': len(v['pedidos']),
            'pct_faturamento': round(100 * v['faturamento'] / faturamento_total, 1)
                if faturamento_total else 0.0,
            'match': match,  # palpite por fuzzy local (sugestao)
            'estado_map': estado_map,
            'mapeado_para': mapeado_para,
            'map_id': map_id,
            'fator': fator,
        })
    produtos_lista.sort(key=lambda x: x['faturamento'], reverse=True)

    return {
        'inicio': data_inicial.isoformat(),
        'fim': data_final.isoformat(),
        'loja': loja_seru,
        'total_pedidos': len(pedidos_filtrados),
        'total_itens_vendidos': round(total_itens, 2),
        'faturamento_total': round(faturamento_total, 2),
        'produtos': produtos_lista,
        'sem_match_count': sem_match,  # mantido por compat
        'pendentes_count': pendentes,
        'lojas_no_intervalo': sorted(lojas_vistas),
    }


def _resolver_nome_item(tipo, item_id):
    if tipo == 'receita':
        r = Receita.query.get(item_id)
        return r.nome if r else None
    if tipo == 'produto':
        p = Produto.query.get(item_id)
        return p.nome if p else None
    if tipo == 'mp':
        from app.models import MateriaPrima
        m = MateriaPrima.query.get(item_id)
        return f'{m.nome} (MP)' if m else None
    return None


def agregar_itens_consolidado(data_inicial, data_final):
    """Versao consolidada pra uso do copilot/tools.

    Historico: agregava Seru + VNDA. VNDA APOSENTADO em 24/06/2026
    (operacao 100% no sistema proprio); hoje a funcao apenas reformata a
    saida de `agregar_itens` (Seru) no shape esperado pelo copilot,
    mantendo `faturamento_vnda=0` e `faturamento_fonte='seru_apenas'` por
    compat com chamadores e testes que ainda olham essas chaves.
    """
    seru_data = agregar_itens(data_inicial, data_final)

    linhas = []
    for p in seru_data['produtos']:
        p = dict(p)
        p['qtd_seru'] = p['qtd']
        p['qtd_vnda'] = 0
        p['fonte'] = 'seru'
        linhas.append(p)
    linhas.sort(key=lambda x: -x['qtd'])

    fat_seru = seru_data['faturamento_total'] or 0
    return {
        'inicio': data_inicial.isoformat(),
        'fim': data_final.isoformat(),
        'total_pedidos_seru': seru_data['total_pedidos'],
        'faturamento_total': round(fat_seru, 2),
        'faturamento_seru': round(fat_seru, 2),
        'faturamento_vnda': 0.0,
        'faturamento_fonte': 'seru_apenas',
        'produtos': linhas,
        'vnda_aviso': 'VNDA aposentado em 06/2026',
        'lojas_no_intervalo': seru_data['lojas_no_intervalo'],
    }


def agregar_itens_consolidado_DESLIGADO(data_inicial, data_final):
    from app.services.vendas_manuais import _agregar_vendas_vnda_api

    seru_data = agregar_itens(data_inicial, data_final)
    vendas_vnda, vnda_aviso = _agregar_vendas_vnda_api(data_inicial, data_final)

    seru_por_chave = {}
    seru_orfaos = []
    for p in seru_data['produtos']:
        m = p.get('match')
        if m and m.get('id') and m.get('tipo') in ('receita', 'produto'):
            chave = (m['tipo'], m['id'])
            existing = seru_por_chave.get(chave)
            if not existing or p['qtd'] > existing['qtd']:
                seru_por_chave[chave] = p
        else:
            seru_orfaos.append(p)

    linhas = []
    chaves_vnda_usadas = set()
    for chave, p in seru_por_chave.items():
        qtd_vnda = vendas_vnda.get(chave, 0)
        chaves_vnda_usadas.add(chave)
        p = dict(p)  # copia rasa pra nao mutar seru_data
        p['qtd_seru'] = p['qtd']
        p['qtd_vnda'] = qtd_vnda
        p['qtd'] = p['qtd_seru'] + qtd_vnda
        p['fonte'] = 'seru+vnda' if qtd_vnda > 0 else 'seru'
        linhas.append(p)

    for chave, qtd in vendas_vnda.items():
        if chave in chaves_vnda_usadas or qtd <= 0:
            continue
        tipo_v, id_v = chave
        nome = _resolver_nome_item(tipo_v, id_v)
        if not nome:
            continue
        linhas.append({
            'nome': nome,
            'sku': None,
            'qtd': qtd,
            'qtd_seru': 0,
            'qtd_vnda': qtd,
            'faturamento': 0,
            'pct_faturamento': 0,
            'n_pedidos': 0,
            'fonte': 'vnda',
            'estado_map': 'vnda_only',
            'mapeado_para': {'tipo': tipo_v, 'id': id_v, 'nome': nome},
            'match': {'tipo': tipo_v, 'id': id_v, 'nome': nome, 'kind': 'exato'},
        })

    for p in seru_orfaos:
        p = dict(p)
        p['qtd_seru'] = p['qtd']
        p['qtd_vnda'] = 0
        p['fonte'] = 'seru'
        linhas.append(p)

    linhas.sort(key=lambda x: -x['qtd'])

    # Faturamento VNDA por data de venda (mesma base do /api/bot/faturamento),
    # pra o "faturamento total" do copilot bater com o atalho do celular.
    # Best-effort: se o site cair, mantem so o Seru (vnda_aviso ja sinaliza).
    from app.services import vnda_sync
    fat_vnda = 0.0
    try:
        fat_vnda = vnda_sync.faturamento_por_dia(data_inicial, data_final)['total']
    except Exception:  # noqa: BLE001
        fat_vnda = 0.0

    fat_seru = seru_data['faturamento_total'] or 0
    return {
        'inicio': data_inicial.isoformat(),
        'fim': data_final.isoformat(),
        'total_pedidos_seru': seru_data['total_pedidos'],
        'faturamento_total': round(fat_seru + fat_vnda, 2),
        'faturamento_seru': round(fat_seru, 2),
        'faturamento_vnda': round(fat_vnda, 2),
        'faturamento_fonte': 'seru+vnda' if fat_vnda > 0 else 'seru_apenas',
        'produtos': linhas,
        'vnda_aviso': vnda_aviso,
        'lojas_no_intervalo': seru_data['lojas_no_intervalo'],
    }


def vendas_vnda_loja(data_inicial, data_final):
    """Vendas do site (VNDA → loja Anesio) por produto, no formato que o
    `consultar_vendas_itens` do copilot espera.

    Usado quando o usuario filtra a venda pela loja do site: essa loja nao tem
    PDV Seru (e manual), entao as vendas vem do VNDA. Qty por produto vem de
    `vnda_sync.agregar_vendas` (por data de entrega); `faturamento_total` vem de
    `vnda_sync.faturamento_por_dia` (por data de venda — mesma base do
    /api/bot/faturamento). O faturamento NAO eh quebrado por produto aqui.
    """
    from app.services import vnda_sync

    base = {
        'inicio': data_inicial.isoformat(),
        'fim': data_final.isoformat(),
        'total_pedidos': 0,
        'faturamento_total': 0.0,
        'faturamento_fonte': 'vnda',
        'produtos': [],
        'vnda_aviso': None,
        'lojas_no_intervalo': [],
    }

    vd = vnda_sync.agregar_vendas(data_inicial, data_final)
    if vd.get('erro'):
        base['vnda_aviso'] = vd['erro']
        return base

    fat = 0.0
    try:
        fat = vnda_sync.faturamento_por_dia(data_inicial, data_final)['total']
    except Exception:  # noqa: BLE001
        fat = 0.0

    produtos = []
    for vp in vd.get('produtos', []):
        mp = vp.get('mapeado_para')
        match = ({'tipo': mp.get('tipo'), 'id': mp.get('id'),
                  'nome': mp.get('nome'), 'kind': 'exato'} if mp else None)
        produtos.append({
            'nome': vp['nome'], 'sku': vp.get('sku'),
            'qtd': vp['qtd'], 'qtd_seru': 0, 'qtd_vnda': vp['qtd'],
            'faturamento': 0, 'fonte': 'vnda', 'match': match,
        })

    base['total_pedidos'] = vd.get('total_pedidos', 0)
    base['faturamento_total'] = round(fat, 2)
    base['produtos'] = produtos
    base['lojas_no_intervalo'] = [vd['loja']] if vd.get('loja') else []
    return base
