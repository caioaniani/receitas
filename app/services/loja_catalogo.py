"""Lógica de catálogo PÚBLICO da loja online (16/06/2026).

Quem decide "está vendendo no site?" mora aqui — pra que a vitrine (Fase 2),
o checkout (Fase 3) e o webhook de pagamento (Fase 4) usem a MESMA regra
sem duplicar. Decisão do dono: `preco_site > 0` já é o flag. Não há coluna
`disponivel_site` separada.

Os 'objetos públicos' devolvidos por este service são DICTS simples (não
ORM) — isso obriga a vitrine a só ler o que a gente expôs aqui, evitando
vazar campo interno (custo, modo de preparo, etc.) por engano.
"""
import logging
import re
import unicodedata

from app.models import Produto, Receita

logger = logging.getLogger(__name__)


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
        # descricao_seo: editorial, gerada com IA e revisada pelo dono em
        # /admin/seo/descricoes. Quando vazia, o template cai no fallback
        # generico "Nome — Categoria" no <meta description>/JSON-LD.
        'descricao': r.descricao_seo or '',
        'slug': _slugify(r.nome),
        'href': f'/loja/{_slugify(r.nome)}-r{r.id}',
    }


def _serializar_produto(p):
    return {
        'id': p.id,
        'kind': 'produto',
        'nome': p.nome,
        # Sem categoria → 'Outros' (não 'Cestas'). Antes o fallback era
        # 'Cestas' porque todo Produto da loja era cesta — mas o dono começou
        # a cadastrar conservas/geleias/molhos como Produto e elas caíam
        # erradas na vitrine.
        'categoria': p.categoria or 'Outros',
        'preco': float(p.preco_site) if p.preco_site else None,
        'imagem': p.imagem_dropbox_url or p.imagem_url or '',
        # descricao_seo (editorial) prevalece sobre `descricao` curta — vai
        # no <meta description>/JSON-LD/card do site. Quando ambas vazias,
        # template cai no fallback "Nome — Categoria".
        'descricao': p.descricao_seo or p.descricao or '',
        'slug': _slugify(p.nome),
        'href': f'/loja/{_slugify(p.nome)}-p{p.id}',
        # No detalhe, vamos expandir os itens da cesta (mas não no listing
        # pra não pesar).
    }


def produtos_publicados():
    """Devolve lista combinada (cestas + pães/doces) prontos pra vitrine.

    Filtro: `preco_site > 0` E item ativo (não arquivada / `ativo=True`).
    Ordenação manual: `ordem_site` ASC (NULLS LAST), depois `nome` ASC.
    Item sem `ordem_site` cai no fim alfabético da sua categoria. Não
    pagina — o catálogo é pequeno (dezenas, não milhares)."""
    receitas = (Receita.query
                .filter(Receita.arquivada_em.is_(None),
                        Receita.preco_site.isnot(None),
                        Receita.preco_site > 0)
                .order_by(Receita.ordem_site.asc().nullslast(),
                          Receita.nome.asc())
                .all())
    produtos = (Produto.query
                .filter(Produto.ativo.is_(True),
                        Produto.preco_site.isnot(None),
                        Produto.preco_site > 0)
                .order_by(Produto.ordem_site.asc().nullslast(),
                          Produto.nome.asc())
                .all())
    out = [_serializar_produto(p) for p in produtos]
    out.extend(_serializar_receita(r) for r in receitas)
    return out


def _estoque_site_map():
    """{(kind, id): saldo} do EstoqueLoja da loja do site — a MESMA de onde
    a entrega baixa (`loja_pagamento.loja_origem_site`). Só itens COM linha:
    quem não tem linha não entra no mapa (= tratado como 0 pela regra do
    dono: todo produto no site deve ter estoque preenchido).

    Devolve None se a loja do site não está configurada — sinaliza "não dá
    pra filtrar"; nesse caso a vitrine NÃO esconde nada (fail-open), pra não
    esvaziar a loja por misconfig. Importação tardia evita ciclo com
    loja_pagamento."""
    from app.models import EstoqueLoja
    from app.services.loja_pagamento import loja_origem_site
    loja = loja_origem_site()
    if not loja:
        logger.warning('vitrine: loja do site não configurada — NÃO filtra '
                       'por estoque (mostra tudo)')
        return None
    mapa = {}
    for el in EstoqueLoja.query.filter_by(loja_id=loja.id).all():
        # Disponivel pro site = quantidade fisica - quantidade reservada
        # por pedidos online em aguardando_pagamento. Sem isso, dois
        # clientes simultaneos viam o mesmo saldo e podiam sobrevender
        # (race condition no cutover, 21/06/2026).
        disp = max(0, (el.quantidade or 0) - (el.quantidade_reservada or 0))
        if el.produto_id:
            mapa[('produto', el.produto_id)] = disp
        elif el.receita_id:
            mapa[('receita', el.receita_id)] = disp
    return mapa


def anotar_esgotado(itens):
    """Marca cada item da lista com `esgotado` (bool). Regra do dono
    (18/06/2026): NADA some da vitrine — saldo 0 (ou sem linha de estoque na
    loja do site) vira 'Esgotado' e não pode ser comprado. Cestas incluídas:
    também precisam de estoque e também aparecem como Esgotado quando zeram.

    Loja do site não configurada → ninguém esgotado (fail-open). Devolve a
    MESMA lista (anota in-place) pra encadear com produtos_publicados()."""
    mapa = _estoque_site_map()
    for it in itens:
        it['esgotado'] = (False if mapa is None
                          else mapa.get((it['kind'], it['id']), 0) <= 0)
    return itens


def tem_estoque_site(kind, item_id):
    """True se o item tem saldo > 0 na loja do site (ou se a loja do site não
    está configurada → fail-open). Usado pela página de produto e pelo
    checkout pra não vender esgotado."""
    mapa = _estoque_site_map()
    if mapa is None:
        return True
    return mapa.get((kind, item_id), 0) > 0


# Categoria especial: produtos com este nome de categoria abrem o modo
# "monte sua cesta" na página de produto — cliente adiciona OUTROS itens
# do catálogo ao carrinho junto da cesta. (Decisão do dono 17/06/2026.)
CATEGORIA_PERSONALIZADA = 'Cestas Personalizadas'


def eh_personalizada(item):
    """Retorna True se o item é uma cesta personalizada."""
    cat = (item.get('categoria') or '').strip() if isinstance(item, dict) \
        else getattr(item, 'categoria', '') or ''
    return cat.strip().lower() == CATEGORIA_PERSONALIZADA.lower()


def itens_para_montar(excluir_item=None):
    """Lista os itens publicados que o cliente pode adicionar pra montar
    uma cesta personalizada. EXCLUI categorias 'Cestas Personalizadas' e
    'Cestas' (pra não meter cesta dentro de cesta) e o próprio item de
    referência (se passado)."""
    excluir_cats = {CATEGORIA_PERSONALIZADA.lower(), 'cestas'}
    out = []
    for it in produtos_publicados():
        cat = (it.get('categoria') or '').strip().lower()
        if cat in excluir_cats:
            continue
        if excluir_item and excluir_item.get('kind') == it['kind'] \
                and excluir_item.get('id') == it['id']:
            continue
        out.append(it)
    return anotar_esgotado(out)


def por_categorias(itens):
    """Agrupa lista de itens publicados por categoria.

    A ORDEM dos grupos vem da tabela `CategoriaSite` (configurável pelo
    admin). Categorias sem linha em CategoriaSite vão pro fim, em ordem
    alfabética. Dentro de cada grupo, mantém a ordem que veio em `itens`
    (já vem ordenada por `ordem_site` ASC, nome ASC)."""
    from app.models import CategoriaSite
    pesos = {c.nome: c.ordem for c in CategoriaSite.query.all()}
    grupos = {}
    for it in itens:
        cat = it.get('categoria') or 'Outros'
        grupos.setdefault(cat, []).append(it)

    def chave(cat):
        # (peso explícito, alfabético). Sem peso → infinito, vai pro fim.
        return (pesos.get(cat, 10**9), cat.lower())
    cats_ord = sorted(grupos.keys(), key=chave)
    return [(cat, grupos[cat]) for cat in cats_ord if grupos[cat]]


def categorias_publicadas():
    """Categorias COM itens publicados, na ordem da vitrine. Devolve
    [{'nome', 'slug'}] — usado pelo dropdown "Produtos" do header (que
    aparece em todas as páginas da loja) e pelas âncoras da home.

    O slug bate com o `id="cat-<slug>"` de cada seção na home, então o
    link `/loja/#cat-<slug>` pula direto pra categoria."""
    return [{'nome': cat, 'slug': _slugify(cat)}
            for cat, _itens in por_categorias(produtos_publicados())]


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
        # Detalhe inclui composição da cesta (nomes só, sem custos). Mostra a
        # quantidade JA FORMATADA com unidade (g/ml/un) — sem isso, peso virava
        # "100x peito de peru" (incidente 22/06/2026, era 100g).
        d['itens'] = [
            {'nome': it.nome_resolvido,
             'quantidade': float(it.quantidade or 1),
             'qtd_formatada': it.qtd_formatada}
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
