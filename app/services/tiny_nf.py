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


def sincronizar_sugestoes():
    """Busca o catálogo do Tiny e sugere SKUs por nome (fuzzy) pros itens
    AINDA não mapeados. Não sobrescreve mapeamento confirmado por humano.
    Devolve {sugeridos, sem_match, total_tiny}."""
    produtos_tiny = tiny.listar_produtos()
    if not produtos_tiny:
        return {'sugeridos': 0, 'sem_match': 0, 'total_tiny': 0,
                'erro': 'Tiny não respondeu (token? rede?)'}
    # Index por nome normalizado (primeiro SKU vence em empate).
    por_nome = {}
    for p in produtos_tiny:
        chave = _norm(p['nome'])
        if chave and chave not in por_nome:
            por_nome[chave] = p

    mp = mapa_por_item()
    sugeridos = sem_match = 0
    for it in loja_catalogo.produtos_publicados():
        m = mp.get((it['kind'], it['id']))
        # Não mexe no que já tem SKU (confirmado ou sugerido antes).
        if m and (m.tiny_sku or '').strip():
            continue
        match = por_nome.get(_norm(it['nome']))
        if not match or not match.get('sku'):
            sem_match += 1
            continue
        if not m:
            m = TinyProdutoMap(kind=it['kind'], item_id=it['id'])
            db.session.add(m)
        m.tiny_sku = match['sku']
        m.tiny_nome = match['nome']
        m.auto_match = True  # sugestão — humano ainda confirma
        sugeridos += 1
    db.session.commit()
    return {'sugeridos': sugeridos, 'sem_match': sem_match,
            'total_tiny': len(produtos_tiny)}
