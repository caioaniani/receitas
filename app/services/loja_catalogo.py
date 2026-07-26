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


# Pães da família sourdough que NÃO se fatiam (pãezinhos/baguetes — o cliente
# rasga, não corta em fatias). Decisão do dono 16/07/2026: "pão francês não
# pode ser fatiado". Casado por trecho do nome (minúsculo, sem acento).
_NAO_FATIAVEL_NOME = ('frances', 'baguete', 'baguette')


def _sem_acento(s):
    import unicodedata
    return ''.join(c for c in unicodedata.normalize('NFKD', s or '')
                   if not unicodedata.combining(c))


def receita_fatiavel(r):
    """Oferece a opção 'fatiado?' no site? (16/07/2026) — só pão sourdough
    de FATIAR (pães de forma / boules grandes), nunca pãozinho.

    Critério: família `pao_sourdough` (a definição do domínio, `Receita.
    familia`) OU o nome contém 'sourdough' — porque alguns sourdoughs (os
    'Mini Sourdough') estão com `familia` NULL no cadastro e seriam perdidos
    por um teste estrito; o default NULL→sourdough pegaria granola/iogurte
    (que também são Receita) por engano, então NÃO usamos `familia_default`.
    EXCETO os pãezinhos de `_NAO_FATIAVEL_NOME` (pão francês, baguete): são
    família sourdough mas não se fatiam. Se um sourdough novo não aparecer,
    basta marcar a família na ficha."""
    if r is None:
        return False
    nome = _sem_acento((r.nome or '').lower())
    if any(t in nome for t in _NAO_FATIAVEL_NOME):
        return False
    if (r.familia or '') == 'pao_sourdough':
        return True
    return 'sourdough' in nome


def _serializar_receita(r):
    return {
        'id': r.id,
        'kind': 'receita',
        'nome': r.nome,
        'categoria': r.categoria or '',
        # Sourdough → oferece "fatiado?" no site (só preferência de corte;
        # não muda preço nem estoque). Front gateia o checkbox por aqui.
        'fatiavel': receita_fatiavel(r),
        # Sob encomenda D+2 (dono 21/07/2026): so vende pra data >= D+2 e e
        # produzido pro pedido — a vitrine trata como SEMPRE disponivel
        # (nao olha plano-do-dia) e a venda nao abate EstoqueLoja.
        'sob_encomenda': bool(getattr(r, 'sob_encomenda', False)),
        'preco': float(r.preco_site) if r.preco_site else None,
        'imagem': r.imagem_dropbox_url or r.imagem_url or '',
        # descricao_seo: editorial, gerada com IA e revisada pelo dono em
        # /admin/seo/descricoes. Quando vazia, o template cai no fallback
        # generico "Nome — Categoria" no <meta description>/JSON-LD.
        'descricao': r.descricao_seo or '',
        # Gramagem por unidade (cadastro tecnico da ficha). O BOT usa pra
        # responder "quantas gramas tem?" sem depender da descricao editorial
        # mencionar o peso (auditoria 03/07: Focaccia sem gramagem derrubou
        # contencao). None = ficha sem peso unitario.
        'peso_g': int(r.peso_unitario) if r.peso_unitario else None,
        'slug': _slugify(r.nome),
        'href': f'/loja/{_slugify(r.nome)}-r{r.id}',
    }


def _anotar_menu(d, p, *, com_slots=False):
    """Anota o bloco `menu` num Produto que é MENU CONFIGURÁVEL (26/07/2026)
    e troca o `preco` exibido pelo preço REAL da pré-seleção.

    O `preco_site` do menu continua sendo só o interruptor de publicação
    (`produtos_publicados` filtra por ele) — o preço que o cliente vê e paga
    é a soma do `preco_menu` de cada mini escolhido (decisão do dono).
    Deixar os dois divergirem na tela seria mentir o preço.

    Devolve False quando o menu NÃO pode ser vendido — fail-close, some da
    vitrine com WARNING em vez de publicar algo que o cliente não consegue
    comprar. O admin vê o pendente na tela da cesta. Três motivos (os três
    achados de revisão 26/07/2026):

    1. **algum SLOT sem `preco_menu` > 0** — não só os da pré-seleção: o
       cliente pode escolher QUALQUER slot, então todos precisam de preço.
       (Antes, um slot com quantidade 0 e preço vazio passava pelo gate e
       estourava a página do produto ao formatar `None`.)
    2. **pré-seleção que não fecha o total obrigatório** — o preço da
       vitrine nunca poderia ser cobrado e o quick-add do card geraria um
       carrinho impossível de fechar no checkout.
    3. **preço total zero** — publicaria "R$ 0,00" e o checkout responderia
       "saiu de catálogo" (o gate genérico trata preço 0 como ausente)."""
    from app.services import loja_menu
    slots = loja_menu.slots(p)
    if not slots:
        logger.warning('menu %r fora da vitrine: sem componentes.', p.nome)
        return False
    sem_preco = [s['nome'] for s in slots
                 if s['preco'] is None or s['preco'] <= 0]
    if sem_preco:
        logger.warning('menu %r fora da vitrine: sem "preço no menu" em %s.',
                       p.nome, ', '.join(sem_preco))
        return False
    total, teto = loja_menu.regras(p)
    padrao = loja_menu.composicao_padrao(p)
    if sum(padrao.values()) != total:
        logger.warning('menu %r fora da vitrine: a pré-seleção soma %d, mas '
                       'o total obrigatório é %d — ajuste as quantidades do '
                       'cadastro.', p.nome, sum(padrao.values()), total)
        return False
    preco_padrao = loja_menu.preco(p, padrao)
    if not preco_padrao or preco_padrao <= 0:
        logger.warning('menu %r fora da vitrine: preço total zerado.', p.nome)
        return False
    d['preco'] = float(preco_padrao)
    d['menu'] = {
        'total': total,
        'max_por_item': teto,
        'comp_padrao': loja_menu.compactar(padrao),
        # Resumo da pré-seleção: o quick-add do card da vitrine precisa dele
        # pra a linha do carrinho já nascer dizendo o que vem dentro.
        'resumo_padrao': loja_menu.resumo(p, padrao),
    }
    if com_slots:
        d['menu']['slots'] = loja_menu.slots(p)
    return True


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
        # Sob encomenda D+2 (espelho da receita) — ver _serializar_receita.
        'sob_encomenda': bool(getattr(p, 'sob_encomenda', False)),
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
    from app.services import loja_menu
    out = []
    for p in produtos:
        d = _serializar_produto(p)
        # Menu configurável (26/07/2026): preço vem da pré-seleção, não do
        # preco_site. Menu sem preço por item cadastrado NÃO vai pra vitrine.
        if loja_menu.eh_menu(p) and not _anotar_menu(d, p):
            continue
        out.append(d)
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


# Janela usada pra "tem em outros dias" — quantos dias o cliente PODE escolher
# como entrega futura. 14 cobre a janela do calendario de planejamento e
# evita expor saldos muito distantes que ainda nao foram planejados.
_JANELA_DIAS_FUTUROS = 14


def _saldo_para_dia(kind, item_id, data, *, saldos_dia_cache=None):
    """Saldo disponivel pra vender no site na data X — SO pelo plano-do-dia.

    Regra do dono (01/07/2026): a disponibilidade do site vem UNICAMENTE do
    plano-do-dia (loja_plano_dia), NUNCA do EstoqueLoja fisico. Se ha plano
    cadastrado pra esse item naquela data → usa o saldo do plano (pode ser 0 =
    esgotado). Se NAO ha plano → None = "sem controle" = fail-open: o item
    vende livre; o plano serve so pra CAPAR/zerar itens especificos.

    `saldos_dia_cache` evita re-querar o plano em loop (anotar_esgotado
    processa N itens)."""
    from app.services import loja_plano_dia
    if saldos_dia_cache is None:
        saldos_dia_cache = {}
    if data not in saldos_dia_cache:
        saldos_dia_cache[data] = loja_plano_dia.saldos_para_dia(data)
    saldos = saldos_dia_cache[data]
    if (kind, item_id) in saldos:
        return saldos[(kind, item_id)]
    return None  # sem plano = sem controle (fail-open) — nao olha o fisico


def _datas_janela_futura(inicio):
    """Lista [hoje, hoje+1, ..., hoje+JANELA-1] em date."""
    from datetime import timedelta
    return [inicio + timedelta(days=i) for i in range(_JANELA_DIAS_FUTUROS)]


def anotar_esgotado(itens):
    """Marca cada item com 3 flags pra a vitrine sinalizar disponibilidade:

    - `esgotado_hoje`: sem saldo pra HOJE no plano-do-dia (sem plano = livre).
    - `tem_em_outros_dias`: tem saldo em algum dos proximos 14 dias no plano.
      Default True quando nao ha plano cadastrado pra nenhum dos proximos 14
      (fail-open: cliente pode comprar pra outro dia).
    - `esgotado`: a "esgotado dura" (sem saldo em nenhum dia). Pra o template
      mostrar a etiqueta vermelha.

    Disponibilidade vem SO do plano-do-dia (regra do dono 01/07/2026) — o
    EstoqueLoja fisico NAO entra aqui. Sem plano → flags False (fail-open).
    Devolve a mesma lista (anota in-place)."""
    from app.utils import hoje
    dia_hoje = hoje()
    datas = _datas_janela_futura(dia_hoje)
    saldos_cache = {}
    for it in itens:
        kind, item_id = it['kind'], it['id']
        # Sob encomenda: produzido pro pedido, SEMPRE disponivel na vitrine
        # (nunca esgota — a trava e so a data D+2 no checkout). Nao olha
        # plano-do-dia nem estoque fisico.
        if it.get('sob_encomenda'):
            it['esgotado_hoje'] = False
            it['tem_em_outros_dias'] = True
            it['esgotado'] = False
            continue
        saldo_hoje = _saldo_para_dia(
            kind, item_id, dia_hoje, saldos_dia_cache=saldos_cache)
        if saldo_hoje is None:
            it['esgotado_hoje'] = False
            it['tem_em_outros_dias'] = True
            it['esgotado'] = False
            continue
        it['esgotado_hoje'] = saldo_hoje <= 0
        # Olha os PROXIMOS dias (sem incluir hoje) pra saber se ainda da pra
        # comprar pra outra data.
        tem_outros = False
        for d in datas[1:]:
            s = _saldo_para_dia(
                kind, item_id, d, saldos_dia_cache=saldos_cache)
            if s is None:
                # Sem plano pra esse dia → sem controle → disponivel (fail-open).
                tem_outros = True
                break
            if s > 0:
                tem_outros = True
                break
        it['tem_em_outros_dias'] = tem_outros
        it['esgotado'] = it['esgotado_hoje'] and not tem_outros
    return itens


def tem_estoque_site(kind, item_id):
    """Compat SEM data: True se o item tem ALGUM dia vendavel na janela de 14
    dias pelo PLANO-DO-DIA (regra do dono 01/07/2026 — nunca olha o EstoqueLoja
    fisico). Fail-open: sem plano em nenhum dia → True. So retorna False no
    "esgotado duro" (plano zera o item em TODOS os proximos 14 dias). A trava
    fina por data de entrega e o `tem_estoque_para_dia(kind, id, data)`."""
    from app.utils import hoje
    saldos_cache = {}
    for d in _datas_janela_futura(hoje()):
        s = _saldo_para_dia(kind, item_id, d, saldos_dia_cache=saldos_cache)
        if s is None or s > 0:
            return True
    return False


def tem_estoque_para_dia(kind, item_id, data):
    """True se da pra vender o item pra entregar na data X — SO pelo plano-do-
    dia. Fail-open: sem plano cadastrado pra o item/data → True (vende livre);
    com plano → saldo do plano > 0. O EstoqueLoja fisico NAO entra.

    Item sob encomenda (produzido pro pedido) e SEMPRE vendavel — nao passa
    pelo plano-do-dia (a unica trava dele e a data D+2 no checkout)."""
    if item_e_sob_encomenda(kind, item_id):
        return True
    s = _saldo_para_dia(kind, item_id, data)
    if s is None:
        return True
    return s > 0


def item_e_sob_encomenda(kind, item_id):
    """True se a receita/produto esta marcada `sob_encomenda` (produzido pro
    pedido: nao abate EstoqueLoja, fica fora do plano-do-dia, so vende D+2).
    Fonte unica pra o checkout/reserva/pagamento/previsao consultarem."""
    if kind == 'receita':
        r = Receita.query.get(item_id)
        return bool(r and getattr(r, 'sob_encomenda', False))
    if kind == 'produto':
        p = Produto.query.get(item_id)
        return bool(p and getattr(p, 'sob_encomenda', False))
    return False


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
        from app.services import loja_menu
        if loja_menu.eh_menu(p):
            # Menu configurável: leva os SLOTS (o cliente escolhe quantos de
            # cada). Sem preço por item cadastrado, o menu não é vendável —
            # devolve None (mesma porta do "saiu de catálogo").
            if not _anotar_menu(d, p, com_slots=True):
                return None
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
