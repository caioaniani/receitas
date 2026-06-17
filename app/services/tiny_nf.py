"""Mapeamento e (futuro) emissão de NF-e via Tiny pra loja online (Fase 5).

Esta primeira parte cobre só o MAPEAMENTO: ligar cada item publicado no
site (Receita/Produto) ao SKU dele no Tiny. A emissão de NF usa esse mapa
pra mandar o pedido pro Tiny por SKU (o Tiny aplica NCM/CFOP/CST do
cadastro do produto — fiscal não mora aqui).

Padrão espelha o mapeamento Seru/VNDA: auto-sugestão por nome (fuzzy) +
confirmação humana no admin.
"""
import logging
import unicodedata

from app.extensions import db
from app.models import TinyProdutoMap
from app.services import loja_catalogo, tiny
from app.utils import agora

logger = logging.getLogger(__name__)


def _norm(s):
    """Normaliza nome pra comparar: minúsculo, sem acento, sem pontuação."""
    s = unicodedata.normalize('NFKD', (s or '')).encode('ascii', 'ignore').decode()
    return ' '.join(s.lower().split())


def mapa_por_item():
    """Dict {(kind, item_id): TinyProdutoMap} de tudo que já tem registro."""
    return {(m.kind, m.item_id): m for m in TinyProdutoMap.query.all()}


def sku_do_item(kind, item_id):
    """SKU do Tiny pra um item nosso, ou None se não mapeado."""
    m = TinyProdutoMap.query.filter_by(kind=kind, item_id=item_id).first()
    return (m.tiny_sku or '').strip() if m and m.tiny_sku else None


def definir_sku(kind, item_id, sku, tiny_nome=None, user_id=None):
    """Upsert do SKU de um item. SKU vazio = volta a pendente."""
    sku = (sku or '').strip()
    m = TinyProdutoMap.query.filter_by(kind=kind, item_id=item_id).first()
    if not m:
        m = TinyProdutoMap(kind=kind, item_id=item_id)
        db.session.add(m)
    m.tiny_sku = sku or None
    if tiny_nome is not None:
        m.tiny_nome = tiny_nome
    m.auto_match = False  # definido por humano
    m.confirmado_em = agora() if sku else None
    m.confirmado_por = user_id if sku else None
    db.session.commit()
    return m


def itens_para_mapear():
    """Lista os itens publicados (preco_site>0) com o estado do mapeamento
    Tiny. Devolve [{kind,id,nome,categoria,sku,estado,confirmado}]."""
    mp = mapa_por_item()
    out = []
    for it in loja_catalogo.produtos_publicados():
        m = mp.get((it['kind'], it['id']))
        out.append({
            'kind': it['kind'], 'id': it['id'], 'nome': it['nome'],
            'categoria': it.get('categoria') or '',
            'sku': (m.tiny_sku if m else None),
            'tiny_nome': (m.tiny_nome if m else None),
            'estado': (m.estado if m else 'pendente'),
            'auto': bool(m and m.auto_match),
            'confirmado': bool(m and m.confirmado_em),
        })
    return out


_FUZZY_CUTOFF = 86  # score mínimo (rapidfuzz WRatio) pra sugerir um SKU


def _aplicar_pares(pares, user_id=None):
    """Aplica uma lista de (nome_tiny, sku) ao mapeamento dos itens
    publicados. Match EXATO (nome normalizado igual) → confirma automático;
    match FUZZY (parecido) → sugestão pra revisar. Nunca toca no que já foi
    confirmado por humano. Devolve {exatos, sugeridos, sem_match, total}."""
    from rapidfuzz import fuzz, process

    # Index: nome_normalizado -> (nome_tiny, sku); e lista pro fuzzy.
    por_norma = {}
    for nome, sku in pares:
        sku = (str(sku) or '').strip()
        if sku.endswith('.0'):
            sku = sku[:-2]
        k = _norm(nome)
        if k and sku and k not in por_norma:
            por_norma[k] = (nome, sku)
    chaves = list(por_norma.keys())

    mp = mapa_por_item()
    exatos = sugeridos = sem_match = 0
    for it in loja_catalogo.produtos_publicados():
        m = mp.get((it['kind'], it['id']))
        if m and m.confirmado_em:   # humano já confirmou — não mexe
            continue
        alvo = _norm(it['nome'])
        exato = por_norma.get(alvo)
        if exato:
            nome_tiny, sku = exato
            confirmado = True
        else:
            achado = process.extractOne(alvo, chaves, scorer=fuzz.WRatio,
                                        score_cutoff=_FUZZY_CUTOFF)
            if not achado:
                sem_match += 1
                continue
            nome_tiny, sku = por_norma[achado[0]]
            confirmado = False
        if not m:
            m = TinyProdutoMap(kind=it['kind'], item_id=it['id'])
            db.session.add(m)
        m.tiny_sku = sku
        m.tiny_nome = nome_tiny
        if confirmado:
            m.auto_match = False
            m.confirmado_em = agora()
            m.confirmado_por = user_id
            exatos += 1
        else:
            m.auto_match = True  # sugestão — humano confirma salvando
            sugeridos += 1
    db.session.commit()
    return {'exatos': exatos, 'sugeridos': sugeridos, 'sem_match': sem_match,
            'total': len(pares)}


def sincronizar_sugestoes(user_id=None):
    """Busca o catálogo do Tiny (API) e mapeia por nome. Match exato confirma
    automático; parecido vira sugestão. Devolve contadores ou {erro}."""
    produtos_tiny = tiny.listar_produtos()
    if not produtos_tiny:
        return {'erro': 'Tiny não respondeu (token ausente ou rede). '
                'Dica: você pode importar a planilha de produtos do Tiny.'}
    pares = [(p['nome'], p['sku']) for p in produtos_tiny if p.get('sku')]
    res = _aplicar_pares(pares, user_id=user_id)
    res['total_tiny'] = len(produtos_tiny)
    return res


def _parse_planilha(conteudo, filename):
    """Extrai [(nome, sku)] de um export de produtos do Tiny (.xls ou .csv).
    Acha as colunas pelo cabeçalho ('Descrição'/'nome' e 'Código (SKU)')."""
    nome_l = (filename or '').lower()
    linhas = []
    if nome_l.endswith('.csv') or (not nome_l.endswith('.xls')
                                   and b',' in conteudo[:2000]):
        import csv
        import io
        txt = conteudo.decode('utf-8', errors='ignore')
        amostra = txt[:1000]
        sep = ';' if amostra.count(';') > amostra.count(',') else ','
        linhas = list(csv.reader(io.StringIO(txt), delimiter=sep))
    else:
        import xlrd
        wb = xlrd.open_workbook(file_contents=conteudo,
                                ignore_workbook_corruption=True)
        sh = wb.sheet_by_index(0)
        linhas = [[sh.cell_value(r, c) for c in range(sh.ncols)]
                  for r in range(sh.nrows)]
    if not linhas:
        return []
    hdr = [str(x).strip().lower() for x in linhas[0]]

    def achar(*termos):
        for i, h in enumerate(hdr):
            if any(t in h for t in termos):
                return i
        return None
    i_sku = achar('sku', 'código', 'codigo')
    i_nome = achar('descrição', 'descricao', 'nome')
    if i_sku is None or i_nome is None:
        return []
    out = []
    for row in linhas[1:]:
        if i_sku < len(row) and i_nome < len(row):
            nome = str(row[i_nome]).strip()
            sku = str(row[i_sku]).strip()
            if nome and sku:
                out.append((nome, sku))
    return out


def importar_planilha(conteudo, filename, user_id=None):
    """Importa o export de produtos do Tiny e mapeia os SKUs por nome.
    Devolve contadores ou {erro}."""
    pares = _parse_planilha(conteudo, filename)
    if not pares:
        return {'erro': 'Não consegui ler a planilha. Use o export de '
                'produtos do Tiny (.xls ou .csv) com as colunas "Descrição" '
                'e "Código (SKU)".'}
    return _aplicar_pares(pares, user_id=user_id)
