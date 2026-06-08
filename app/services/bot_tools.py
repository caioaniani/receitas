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


def _iter_variants(variants):
    """Normaliza o campo `variants` do VNDA, que aparece em 3 formatos:
      - dict keyed por id:         {"61": {...}}
      - lista de variantes:        [{...sku...}]
      - lista de {id: variante}:   [{"61": {...}}]   (formato REAL observado)
    Devolve a lista dos dicts de variante (com sku/price/etc)."""
    if isinstance(variants, dict):
        candidatos = list(variants.values())
    elif isinstance(variants, list):
        candidatos = variants
    else:
        return []
    out = []
    for c in candidatos:
        if not isinstance(c, dict):
            continue
        if 'sku' in c or 'id' in c or 'price' in c:
            out.append(c)              # variante direta
        else:
            for v in c.values():       # wrapper {id: variante}
                if isinstance(v, dict):
                    out.append(v)
    return out


def _limpar_descricao(p):
    """Descricao do produto VNDA (texto/HTML) limpa e truncada. E o que permite
    o bot responder 'o que tem na cesta X?' sem jogar pro humano."""
    import re
    raw = (p.get('description') or p.get('html_description')
           or p.get('short_description') or p.get('meta_description') or '')
    if not raw:
        return ''
    texto = re.sub(r'<[^>]+>', ' ', str(raw))       # tira tags HTML
    texto = re.sub(r'\s+', ' ', texto).strip()       # normaliza espacos
    return texto[:600]


def _parse_produtos(raw):
    """Extrai [{nome, sku, preco, disponivel, descricao}] da resposta do VNDA.

    SKU sempre de variants[].sku; preco prioriza sale_price (o que o cliente
    paga); descricao (conteudo da cesta/produto) vem do produto. Uma linha por
    variante que tenha SKU."""
    out = []
    for p in (raw or []):
        nome_base = (p.get('name') or p.get('title') or '').strip()
        descricao = _limpar_descricao(p)
        for v in _iter_variants(p.get('variants')):
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
            preco = v.get('sale_price')
            if preco is None:
                preco = v.get('price')
            if preco is None:
                preco = p.get('price')
            out.append({
                'nome': nome,
                'sku': str(sku),
                'preco': preco,
                'disponivel': bool(disp),
                'descricao': descricao,
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
        # Params iguais aos do n8n do cliente (validados em producao por anos):
        # per_page=100, available=true. _parse_produtos ainda marca disponibilidade
        # por variante (catalogo da padaria pequeno; lote unico cabe).
        # Token de produtos: o token principal nao tem escopo de catalogo (403);
        # _produtos_token usa VNDA_PRODUTOS_TOKEN se setado, senao o principal.
        resp = vnda._get('/products', params={'available': 'true',
                                              'per_page': 100, 'page': page},
                         token=vnda._produtos_token())
        if not resp:
            logger.warning('catalogo VNDA /products falhou (page=%s)', page)
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
    logger.info('catalogo VNDA carregado: %d produto(s) com SKU', len(produtos))
    _catalogo_cache['produtos'] = produtos
    _catalogo_cache['ts'] = agora
    return produtos


def consultar_produtos(busca):
    """Busca produtos no catalogo do VNDA por texto. Retorna
    {'produtos': [{nome, sku, preco, disponivel, descricao?}]} ou {'erro': ...}.

    Match focado (achou produto pelo termo): inclui a `descricao` — pra o bot
    responder 'o que tem na cesta X?'. Sem match: devolve o catalogo amplo SEM
    descricao (economiza token) pro Claude aplicar sinonimos (ex: "amendoas" ->
    "Almond"). SKU sempre de variants[].sku."""
    catalogo = _carregar_catalogo()
    if catalogo is None:
        return {'erro': 'VNDA indisponível no momento'}

    from app.utils import normalizar_busca
    termos = [t for t in normalizar_busca(busca or '').split()
              if len(t) > 2 and t not in _STOPWORDS]
    if termos:
        filtrados = [p for p in catalogo
                     if any(t in normalizar_busca(p['nome']) for t in termos)]
        if filtrados:
            return {'produtos': filtrados[:40]}  # com descricao (foco)
    # Sem match: catalogo amplo SEM descricao (token-light).
    leve = [{k: v for k, v in p.items() if k != 'descricao'} for p in catalogo[:80]]
    return {'produtos': leve}


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


def _origem_e_vnda(pedido_tiny):
    """Heuristica: o pedido no Tiny veio do site (VNDA) ou outra origem?
    Pra cumprir a regra 'so VNDA' definida com o dono."""
    origem = (pedido_tiny.get('origem') or '').lower()
    # Tiny marca origem como 'ecommerce', 'vnda', etc — qualquer um desses indica site.
    return any(t in origem for t in ('vnda', 'ecommerce', 'e-commerce', 'site'))


def buscar_nota_fiscal(cpf, numero_pedido, *, conv_id=None, canal=None):
    """Busca a NF do pedido no Tiny por (CPF + numero do pedido). E o caminho
    SEGURO — sem CPF o bot nunca expoe dado fiscal de outro cliente.

    Retorna dict com instrucao clara pro Claude:
      {'link': str}                          → NF emitida (link do DANFE)
      {'erro': 'sem_nf_ainda', ...}          → pedido existe mas NF nao foi emitida
      {'erro': 'nao_encontrado', ...}        → CPF+numero nao casou
      {'erro': 'fora_site', ...}             → pedido B2B/local -> humano
      {'erro': 'tiny_indisponivel', ...}     → API caiu

    SEMPRE registra no NFLog (audit LGPD)."""
    from app.services import tiny
    cpf_d = ''.join(c for c in (cpf or '') if c.isdigit())
    numero = (numero_pedido or '').strip()

    def _log(resultado, detalhe=''):
        try:
            from app.extensions import db
            from app.models import NFLog
            db.session.add(NFLog(
                conv_id=str(conv_id) if conv_id else None,
                canal=canal or None,
                cpf_4ultimos=cpf_d[-4:] if len(cpf_d) >= 4 else None,
                numero_pedido=numero[:50] or None,
                resultado=resultado,
                detalhe=(detalhe or '')[:500] or None,
            ))
            db.session.commit()
        except Exception:  # noqa: BLE001
            logger.exception('NFLog falhou (resultado=%s)', resultado)
            try:
                from app.extensions import db
                db.session.rollback()
            except Exception:  # noqa: BLE001
                pass

    if len(cpf_d) not in (11, 14) or not numero:
        _log('erro', 'CPF/numero ausentes ou invalidos')
        return {'erro': 'dados_incompletos',
                'mensagem': 'Preciso do CPF do pedido e do número do pedido.'}

    if not tiny.disponivel():
        _log('erro', 'TINY_API_TOKEN nao configurado')
        return {'erro': 'tiny_indisponivel',
                'mensagem': 'Não consigo consultar a nota agora. Já passo pra um atendente.'}

    pedido = tiny.buscar_pedido_por_cpf_e_numero(cpf_d, numero)
    if not pedido:
        _log('nao_encontrado')
        return {'erro': 'nao_encontrado',
                'mensagem': 'Não encontrei pedido com esse CPF e número. Confere os dados, por favor.'}

    if not _origem_e_vnda(pedido):
        _log('handoff', f'pedido origem={pedido.get("origem")}')
        return {'erro': 'fora_site',
                'mensagem': 'Esse pedido vou passar pra um atendente continuar com você.'}

    nota_id = pedido.get('nota_fiscal_id') or ''
    if not nota_id:
        _log('sem_nf', f'situacao={pedido.get("situacao")}')
        return {'erro': 'sem_nf_ainda',
                'situacao': pedido.get('situacao'),
                'mensagem': 'Achei seu pedido, mas a nota ainda não foi emitida. Ela sai junto com o despacho — te aviso ou você pode pedir depois.'}

    link = tiny.obter_link_nota_fiscal(nota_id)
    if not link:
        _log('erro', f'sem link pra nota_id={nota_id}')
        return {'erro': 'link_falhou',
                'mensagem': 'Achei a nota mas não consegui gerar o link agora. Já passo pra um atendente.'}

    _log('enviada', f'nota_id={nota_id}')
    return {'link': link, 'numero_pedido': pedido.get('numero')}


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
