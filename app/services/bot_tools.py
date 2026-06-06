"""Ferramentas do bot de atendimento (Fase 2): consulta de produtos e pedidos
no VNDA + geracao de links de carrinho/cesta.

Catalogo: usamos GET /api/v2/products?available=true (mesmo endpoint que o bot
antigo no n8n usava) e filtramos o termo localmente — o catalogo da padaria e
pequeno. ATENCAO ao formato do VNDA: `variants` vem como DICT keyed pelo id da
variante ({"11": {"sku": "10007", ...}}), NAO como lista. O SKU do link de
carrinho e SEMPRE variants[].sku (nunca o id do produto nem o id da variante);
extraimos isso deterministicamente aqui pra o Claude nunca montar SKU na mao.

Entrega/CEP NAO esta aqui de proposito: o endpoint de frete do VNDA
(/variants/{sku}/shipping_methods) precisa ser validado antes de o bot
afirmar entrega a cliente. Ate la, o prompt manda o bot passar pro humano.
"""
import logging
import time

from app.services import vnda

logger = logging.getLogger(__name__)

SHOP = 'https://www.padariaartesanalonline.com.br'

# Catalogo muda pouco; cache curto evita martelar o VNDA a cada mensagem.
_CATALOGO_TTL = 300  # segundos
_catalogo_cache = {}  # {'produtos': [...], 'ts': float}
_STOPWORDS = {'de', 'da', 'do', 'com', 'sem', 'para', 'pra', 'uma', 'um',
              'os', 'as', 'que', 'meu', 'minha'}

# Links das paginas das cestas (estaticos, do prompt do cliente).
LINKS_CESTAS = {
    'sweet coffee': f'{SHOP}/produto/sweet-coffee-55',
    'bonjour': f'{SHOP}/produto/bonjour-44',
    'box mimo': f'{SHOP}/produto/box-mimo-42',
    'bandeja de cafe da manha': f'{SHOP}/produto/bandeja-de-cafe-da-manha-41',
    'family box': f'{SHOP}/produto/family-box-20',
    'caixa especial': f'{SHOP}/produto/caixa-especial-45',
    'abraco em forma de paes': f'{SHOP}/produto/abraco-em-forma-de-paes-46',
    'especial pascoa': f'{SHOP}/produto/especial-pascoa-58',
    'lancheira especial': f'{SHOP}/produto/lancheira-especial-59',
    'kit brunch': f'{SHOP}/produto/kit-brunch-56',
}


def _parse_produtos(raw):
    """Extrai [{nome, sku, preco, disponivel}] da resposta do VNDA.

    Tolera `variants` como DICT (formato real do VNDA: {id: {...}}) ou como
    lista. Uma linha por variante que tenha SKU."""
    out = []
    for p in (raw or []):
        nome_base = (p.get('name') or p.get('title') or '').strip()
        variants = p.get('variants')
        if isinstance(variants, dict):
            lista_v = list(variants.values())
        elif isinstance(variants, list):
            lista_v = variants
        else:
            lista_v = []
        for v in lista_v:
            if not isinstance(v, dict):
                continue
            sku = v.get('sku')
            if not sku:
                continue
            vnome = (v.get('name') or '').strip()
            nome = nome_base
            if vnome and vnome.lower() not in nome_base.lower():
                nome = f'{nome_base} {vnome}'.strip()
            disp = v.get('available')
            if disp is None:
                disp = p.get('available', True)
            preco = v.get('price')
            if preco is None:
                preco = p.get('price')
            out.append({
                'nome': nome,
                'sku': str(sku),
                'preco': preco,
                'disponivel': bool(disp),
            })
    return out


def _carregar_catalogo():
    """Catalogo de produtos disponiveis no VNDA, com cache curto em memoria.

    Retorna lista de {nome, sku, preco, disponivel}, ou None se o VNDA estiver
    fora (1a pagina falhou) — caller trata None como erro e passa pro humano."""
    agora = time.time()
    if (_catalogo_cache.get('produtos') is not None
            and agora - _catalogo_cache.get('ts', 0) < _CATALOGO_TTL):
        return _catalogo_cache['produtos']

    todos = []
    page = 1
    while page <= 10:
        resp = vnda._get('/products', params={'available': 'true',
                                              'per_page': 100, 'page': page})
        if not resp:
            if page == 1:
                return None  # VNDA fora -> caller faz handoff (nao inventa)
            break  # paginas seguintes: tolerantes (catalogo parcial > nada)
        try:
            data = resp.json()
        except ValueError:
            break
        lote = data if isinstance(data, list) else (
            data.get('products') or data.get('results') or [])
        if not lote:
            break
        todos.extend(lote)
        if len(lote) < 100:
            break
        page += 1

    produtos = _parse_produtos(todos)
    _catalogo_cache['produtos'] = produtos
    _catalogo_cache['ts'] = agora
    return produtos


def consultar_produtos(busca):
    """Busca produtos no catalogo do VNDA por texto. Retorna
    {'produtos': [{nome, sku, preco, disponivel}]} ou {'erro': ...}.

    Carrega o catalogo (/products) e filtra pelos termos da busca. Sem match
    local, devolve o catalogo (limitado) pro Claude aplicar o mapa de sinonimos
    do prompt (ex: "amendoas" -> "Almond"). SKU sempre de variants[].sku."""
    catalogo = _carregar_catalogo()
    if catalogo is None:
        return {'erro': 'VNDA indisponível no momento'}

    termos = [t for t in (busca or '').strip().lower().split()
              if len(t) > 2 and t not in _STOPWORDS]
    if termos:
        filtrados = [p for p in catalogo
                     if any(t in p['nome'].lower() for t in termos)]
        if filtrados:
            return {'produtos': filtrados[:40]}
    return {'produtos': catalogo[:80]}


def gerar_link_carrinho(itens):
    """itens: lista de dicts {'sku': str, 'qtd': int}. Monta o link de
    carrinho do VNDA: /carrinho?itens=SKU:qtd,SKU:qtd (parametro 'itens' em
    portugues). Retorna {'link': str} ou {'erro': ...}.

    Determinístico de proposito — tira do Claude o risco de montar a URL
    errada (a regra anti-erro de SKU do prompt vira garantia aqui)."""
    partes = []
    for it in (itens or []):
        sku = str(it.get('sku') or '').strip()
        qtd = it.get('qtd') or it.get('quantidade') or 1
        if sku:
            partes.append(f'{sku}:{int(qtd)}')
    if not partes:
        return {'erro': 'nenhum SKU válido'}
    return {'link': f'{SHOP}/carrinho?itens=' + ','.join(partes)}


def consultar_pedido(numero):
    """Status + DATA DE ENTREGA de um pedido pelo número (code do VNDA).

    A data vem de vnda._extrair_data_entrega, que prioriza a data AGENDADA no
    checkout (extra.DataDeEntrega) — e NAO o expected_delivery_date do VNDA, que
    e o campo bugado por tras do "pedido pode ser entregue hoje" no site. Ou
    seja: esta data e a correta pra desfazer essa confusao com o cliente.

    Retorna dados do pedido ou {'erro': ...}. Nunca expoe dados de outro
    cliente — busca direta pelo code informado."""
    code = str(numero or '').strip()
    if not code:
        return {'erro': 'informe o número do pedido'}
    order = vnda.buscar_pedido_completo(code)
    if not order:
        return {'erro': 'pedido não encontrado'}
    itens = [{'nome': i.get('product_name') or i.get('name') or '',
              'qtd': i.get('quantity', 1)} for i in (order.get('items') or [])]
    data_entrega = vnda._extrair_data_entrega(order)
    return {
        'numero': order.get('code'),
        'status': order.get('status'),
        'total': order.get('total'),
        'data_entrega': data_entrega.strftime('%d/%m/%Y') if data_entrega else None,
        'periodo': vnda._extrair_periodo(order),
        'itens': itens,
    }
