"""Lógica de catálogo PÚBLICO da loja online (16/06/2026).

Quem decide "está vendendo no site?" mora aqui — pra que a vitrine (Fase 2),
o checkout (Fase 3) e o webhook de pagamento (Fase 4) usem a MESMA regra
sem duplicar. Decisão do dono: `preco_site > 0` já é o flag. Não há coluna
`disponivel_site` separada.

Os 'objetos públicos' devolvidos por este service são DICTS simples (não
ORM) — isso obriga a vitrine a só ler o que a gente expôs aqui, evitando
vazar campo interno (custo, modo de preparo, etc.) por engano.
"""
import re
import unicodedata

from app.models import Produto, Receita


def _slugify(texto):
    """Slug ASCII pra URL. 'Sourdough Tradicional' → 'sourdough-tradicional'."""
    if not texto:
        return 'item'
    s = unicodedata.normalize('NFKD', texto)
    s = ''.join(c for c in s if not unicodedata.combining(c))
    s = s.lower()
    s = re.sub(r'[^a-z0-9]+', '-', s).strip('-')
    return s or 'item'


def _serializar_receita(r):
    return {
        'id': r.id,
        'kind': 'receita',
        'nome': r.nome,
        'categoria': r.categoria or '',
        'preco': float(r.preco_site) if r.preco_site else None,
        'imagem': r.imagem_dropbox_url or r.imagem_url or '',
        'descricao': '',  # Receita não tem campo de descrição editorial (v1)
        'slug': _slugify(r.nome),
        'href': f'/loja/{_slugify(r.nome)}-r{r.id}',
    }


def _serializar_produto(p):
    return {
        'id': p.id,
        'kind': 'produto',
        'nome': p.nome,
        'categoria': p.categoria or 'Cestas',
        'preco': float(p.preco_site) if p.preco_site else None,
        'imagem': p.imagem_dropbox_url or p.imagem_url or '',
        'descricao': p.descricao or '',
        'slug': _slugify(p.nome),
        'href': f'/loja/{_slugify(p.nome)}-p{p.id}',
        # No detalhe, vamos expandir os itens da cesta (mas não no listing
        # pra não pesar).
    }


def produtos_publicados():
    """Devolve lista combinada (cestas + pães/doces) prontos pra vitrine.

    Filtro: `preco_site > 0` E item ativo (não arquivada / `ativo=True`).
    Ordenação: cestas primeiro (chamariz visual), depois receitas por
    categoria e nome. Não pagina — o catálogo é pequeno (dezenas, não
    milhares). Quando crescer, paginar."""
    receitas = (Receita.query
                .filter(Receita.arquivada_em.is_(None),
                        Receita.preco_site.isnot(None),
                        Receita.preco_site > 0)
                .order_by(Receita.categoria, Receita.nome)
                .all())
    produtos = (Produto.query
                .filter(Produto.ativo.is_(True),
                        Produto.preco_site.isnot(None),
                        Produto.preco_site > 0)
                .order_by(Produto.nome)
                .all())
    out = [_serializar_produto(p) for p in produtos]
    out.extend(_serializar_receita(r) for r in receitas)
    return out


def por_categorias(itens):
    """Agrupa lista de itens publicados por categoria (mantém a ordem de
    primeira aparição). Pula categorias vazias. Pra o template iterar."""
    grupos = {}
    ordem = []
    for it in itens:
        cat = it.get('categoria') or 'Outros'
        if cat not in grupos:
            grupos[cat] = []
            ordem.append(cat)
        grupos[cat].append(it)
    return [(cat, grupos[cat]) for cat in ordem if grupos[cat]]


def por_id_publicado(kind, item_id):
    """`kind` = 'receita' (r) ou 'produto' (p). Devolve o dict do item se
    estiver publicado (preço > 0 + ativo); senão None."""
    if kind == 'receita':
        r = Receita.query.filter(
            Receita.id == item_id,
            Receita.arquivada_em.is_(None),
            Receita.preco_site.isnot(None),
            Receita.preco_site > 0).first()
        return _serializar_receita(r) if r else None
    if kind == 'produto':
        p = Produto.query.filter(
            Produto.id == item_id,
            Produto.ativo.is_(True),
            Produto.preco_site.isnot(None),
            Produto.preco_site > 0).first()
        if not p:
            return None
        d = _serializar_produto(p)
        # Detalhe inclui composição da cesta (nomes só, sem custos)
        d['itens'] = [
            {'nome': it.nome_resolvido, 'quantidade': float(it.quantidade or 1)}
            for it in p.itens
        ]
        return d
    return None


def parse_slug_id(slug_completo):
    """Parse '/loja/sourdough-tradicional-r12' → ('receita', 12, 'sourdough-tradicional').
    Parse 'box-mimo-p7' → ('produto', 7, 'box-mimo').
    Retorna (None, None, None) se não bate."""
    m = re.match(r'^(.+)-([rp])(\d+)$', slug_completo or '')
    if not m:
        return (None, None, None)
    slug, letra, raw_id = m.group(1), m.group(2), m.group(3)
    kind = 'receita' if letra == 'r' else 'produto'
    try:
        return (kind, int(raw_id), slug)
    except ValueError:
        return (None, None, None)
