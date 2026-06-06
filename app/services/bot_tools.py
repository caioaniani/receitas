"""Ferramentas do bot de atendimento (Fase 2): consulta de produtos e pedidos
no VNDA + geracao de links de carrinho/cesta.

As chamadas ao VNDA seguem a doc oficial (developers.vnda.com.br) e reusam o
cliente de `app.services.vnda`. NAO foram testadas ao vivo aqui (sem token no
ambiente) — confirmar contra o fluxo do n8n na ativacao, em especial o nome do
campo de SKU/estoque na resposta de /products/search.

Entrega/CEP NAO esta aqui de proposito: o endpoint de frete do VNDA
(/variants/{sku}/shipping_methods) precisa ser validado antes de o bot
afirmar entrega a cliente. Ate la, o prompt manda o bot passar pro humano.
"""
import logging

from app.services import vnda

logger = logging.getLogger(__name__)

SHOP = 'https://www.padariaartesanalonline.com.br'

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


def consultar_produtos(busca):
    """Busca produtos no VNDA por texto. Retorna
    {'produtos': [{nome, sku, preco, disponivel}]} ou {'erro': ...}.

    SKU vem SEMPRE de variants[].sku — é o que o link de carrinho exige.
    """
    resp = vnda._get('/products/search', params={'q': busca, 'per_page': 20})
    if not resp:
        return {'erro': 'VNDA indisponível no momento'}
    try:
        data = resp.json()
    except ValueError:
        return {'erro': 'resposta inválida do VNDA'}

    produtos = data if isinstance(data, list) else (data.get('products') or data.get('results') or [])
    out = []
    for p in produtos:
        nome_base = p.get('name') or p.get('title') or ''
        variants = p.get('variants') or []
        if not variants:
            continue
        for v in variants:
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
            out.append({
                'nome': nome,
                'sku': str(sku),
                'preco': v.get('price') or p.get('price'),
                'disponivel': bool(disp),
            })
    return {'produtos': out}


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
    """Status de um pedido pelo número (code do VNDA). Retorna dados do
    pedido mais recente correspondente ou {'erro': ...}. Nunca expõe dados
    de outro cliente — busca direta pelo code informado."""
    code = str(numero or '').strip()
    if not code:
        return {'erro': 'informe o número do pedido'}
    order = vnda.buscar_pedido_completo(code)
    if not order:
        return {'erro': 'pedido não encontrado'}
    itens = [{'nome': i.get('product_name') or i.get('name') or '',
              'qtd': i.get('quantity', 1)} for i in (order.get('items') or [])]
    return {
        'numero': order.get('code'),
        'status': order.get('status'),
        'total': order.get('total'),
        'itens': itens,
    }
