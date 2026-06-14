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
    """Extrai [{nome, sku, preco, disponivel, descricao, url}] do VNDA.

    SKU sempre de variants[].sku; preco prioriza sale_price (o que o cliente
    paga); descricao vem do produto. `url` e a pagina do produto montada do
    slug — caso real (10/06/2026): a "Cesta Especial Dia dos Namorados" nao
    estava na lista fixa do prompt e o bot mandou o link do Kit Brunch pro
    cliente; com a url vinda do catalogo, produto novo nunca mais depende de
    lista decorada. Uma linha por variante que tenha SKU."""
    out = []
    for p in (raw or []):
        nome_base = (p.get('name') or p.get('title') or '').strip()
        descricao = _limpar_descricao(p)
        slug = str(p.get('slug') or '').strip()
        url = f'{SHOP}/produto/{slug}' if slug else None
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
                'url': url,
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


def consultar_ingredientes(nome_produto):
    """Consulta a Receita por nome (fuzzy) e devolve a lista de ingredientes
    pra o bot responder duvidas de gluten/lactose/ovo/origem animal de forma
    HONESTA (sem chutar). Filtra ingredientes < 0.5% (irrelevantes pro cliente
    e ruido na resposta).

    Retorna:
      {'receita': str, 'ingredientes': [{'nome': str, 'pct': float}, ...]}
      {'erro': 'nao_encontrado', 'sugestoes': [str]} — match falhou; sugere
        os 5 nomes mais proximos pra o bot esclarecer com o cliente.
      {'erro': str} — falha de DB ou tabela vazia.

    Nao retorna a receita inteira (peso, modo de preparo) — so a lista que
    importa pra alergia/restricao. NUNCA usar pra alergia confirmada (regra do
    prompt: alergia = handoff sempre)."""
    from app.models import Receita
    from app.utils import normalizar_busca
    try:
        alvo = normalizar_busca(nome_produto or '').strip()
        if not alvo:
            return {'erro': 'nome vazio'}
        receitas = Receita.query.filter(Receita.arquivada_em.is_(None)).all()
        if not receitas:
            return {'erro': 'sem receitas cadastradas'}
        match = next((r for r in receitas
                      if normalizar_busca(r.nome or '') == alvo), None)
        if match is None:
            termos = [t for t in alvo.split() if len(t) > 2]
            if termos:
                match = next((r for r in receitas
                              if all(t in normalizar_busca(r.nome or '')
                                     for t in termos)), None)
        if match is None:
            sug = sorted(
                ({normalizar_busca(r.nome or ''): r.nome for r in receitas
                  if any(t in normalizar_busca(r.nome or '')
                         for t in alvo.split() if len(t) > 2)}.values()),
            )[:5]
            return {'erro': 'nao_encontrado', 'sugestoes': sug}
        ings = []
        for ing in (match.ingredientes or []):
            pct = float(ing.porcentagem or 0)
            if pct < 0.5:
                continue
            ings.append({'nome': (ing.ingrediente_nome or '').strip(),
                         'pct': round(pct, 2)})
        ings.sort(key=lambda x: x['pct'], reverse=True)
        return {'receita': match.nome, 'ingredientes': ings}
    except Exception as exc:  # noqa: BLE001
        logger.exception('consultar_ingredientes falhou nome=%r', nome_produto)
        return {'erro': str(exc)}


def editar_cartinha_pedido(numero_pedido, texto_cartinha):
    """UPSERTA a cartinha de um pedido do site no NOSSO sistema
    (CartinhaEntrega). Aparece pro time de produção/embalagem na tela
    /entregas (manual sobrescreve a cartinha original do VNDA).

    Validacao: confirma que o pedido EXISTE no VNDA antes de gravar —
    senao bot poderia criar cartinha pra pedido fake. Retorna:
      {'ok': True, 'pedido': X, 'acao': 'criada'|'atualizada',
       'texto': str, 'aviso_se_diferente'?: str}
      {'erro': 'pedido_nao_encontrado'} — pedido nao existe no VNDA
      {'erro': 'texto_vazio'} — cliente nao informou cartinha
      {'erro': str} — falha generica
    """
    from app.extensions import db
    from app.models import CartinhaEntrega
    from app.utils import agora as _agora
    try:
        numero = str(numero_pedido or '').strip()
        texto = (texto_cartinha or '').strip()
        if not numero:
            return {'erro': 'numero_pedido vazio'}
        if not texto:
            return {'erro': 'texto_vazio'}
        # Confirma pedido no VNDA — evita gravar cartinha de pedido fake.
        try:
            pedido = vnda.buscar_pedido_completo(numero)
        except Exception:  # noqa: BLE001
            logger.exception('editar_cartinha_pedido: VNDA falhou nro=%r', numero)
            return {'erro': 'vnda_indisponivel'}
        if not pedido or not isinstance(pedido, dict):
            return {'erro': 'pedido_nao_encontrado'}
        code = str(pedido.get('code') or pedido.get('number') or numero).strip()
        c = CartinhaEntrega.query.filter_by(pedido_code=code).first()
        existia = c is not None
        if not c:
            c = CartinhaEntrega(pedido_code=code)
            db.session.add(c)
        c.texto = texto
        c.atualizado_em = _agora()
        c.atualizado_por = None  # bot — sem usuario humano
        db.session.commit()
        return {
            'ok': True,
            'pedido': code,
            'acao': 'atualizada' if existia else 'criada',
            'texto': texto,
        }
    except Exception as exc:  # noqa: BLE001
        logger.exception('editar_cartinha_pedido falhou nro=%r', numero_pedido)
        return {'erro': str(exc)}


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
    """O pedido no Tiny veio do site (VNDA) ou outra origem?

    Estrategia em duas camadas:
      1) Se o pedido tem `numero_ecommerce` nao-vazio, e do site — esse
         campo so existe em pedido vindo via integracao com loja virtual,
         B2B/local nao tem.
      2) Senao, cai no fallback de checar o campo `origem` (texto livre,
         pode vir 'ecommerce', 'vnda', etc).

    A regra existe pra cumprir a decisao 'so atender NF de pedido do site'."""
    if (pedido_tiny.get('numero_ecommerce') or '').strip():
        return True
    origem = (pedido_tiny.get('origem') or '').lower()
    return any(t in origem for t in ('vnda', 'ecommerce', 'e-commerce', 'site'))


# Resumo curto pra cada resultado da NF — entra no aviso do dono no WhatsApp.
_NF_RESUMO = {
    'enviada': 'NF enviada ✅',
    'sem_nf_ainda': 'pedido sem NF emitida ainda',
    'nao_encontrado': 'CPF+nº não bateu',
    'fora_site': 'pedido fora do site → atendente',
    'erro': 'erro técnico',
    'handoff': 'handoff pro atendente',
}


def _avisar_dono_nf(resultado, cpf_digits, numero, conv_id, canal, *, detalhe=''):
    """Manda WhatsApp pro dono cada vez que alguem solicita NF — pra ele
    acompanhar. Best-effort: nunca propaga exception, e desligavel por
    env var CHATBOT_AVISAR_NF=0.

    Dados intencionalmente compactos:
      - so 4 ultimos digitos do CPF (mesma regra do NFLog)
      - sem nome de cliente (link pro Chatwoot resolve)
      - 1 linha por solicitacao
    """
    import os as _os

    from flask import current_app as _app
    cfg = _app.config
    if _os.environ.get('CHATBOT_AVISAR_NF',
                       str(cfg.get('CHATBOT_AVISAR_NF', '1'))) == '0':
        return
    numero_destino = ((cfg.get('CHATBOT_VIGIA_NUMERO') or '').strip()
                       or (cfg.get('ZAPI_NUMERO_DESTINO') or '').strip())
    if not numero_destino:
        return

    cpf_4 = cpf_digits[-4:] if len(cpf_digits or '') >= 4 else '????'
    resumo = _NF_RESUMO.get(resultado, resultado)
    canal_label = (canal or 'cliente').strip()

    linhas = [
        '*NF solicitada*',
        f'Pedido #{numero or "?"} · CPF ...{cpf_4}',
        f'Canal: {canal_label}',
        f'Resultado: {resumo}',
    ]
    if detalhe and resultado in ('erro', 'sem_nf_ainda'):
        linhas.append(detalhe[:200])

    base_cw = (cfg.get('CHATWOOT_URL') or '').rstrip('/')
    acc = (cfg.get('CHATWOOT_ACCOUNT_ID') or '').strip()
    if base_cw and acc and conv_id:
        linhas.append('')
        linhas.append(f'{base_cw}/app/accounts/{acc}/conversations/{conv_id}')

    try:
        from app.services import zapi
        zapi.enviar_texto(numero_destino, '\n'.join(linhas))
    except Exception:  # noqa: BLE001
        logger.exception('aviso dono NF falhou')


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
        # Aviso pro dono no WhatsApp via Z-API (cada solicitacao de NF). E
        # disparado JUNTO com o log pra centralizar; se um falhar, o outro
        # tenta mesmo assim. Best-effort.
        _avisar_dono_nf(resultado, cpf_d, numero, conv_id, canal,
                         detalhe=detalhe)
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

    diag: dict = {}
    pedido = tiny.buscar_pedido_por_cpf_e_numero(cpf_d, numero, diag=diag)
    if not pedido:
        # Diferencia "API caiu" de "nao casou". Se a API falhou, a gente NUNCA
        # pode dizer ao cliente "confere os dados" — isso lava as maos da
        # falha tecnica e culpa ele. Joga pro humano com handoff explicito.
        if diag.get('api_falhou_em_pagina'):
            causa = tiny._consumir_falha() or 'desconhecida'
            det = (f'API Tiny falhou na pagina {diag["api_falhou_em_pagina"]} '
                   f'({causa}; paginas_lidas={diag.get("paginas_lidas")})')
            _log('erro', det)
            return {'erro': 'tiny_indisponivel',
                    'mensagem': 'Não consegui consultar a nota agora — está com instabilidade. Já passo pra um atendente.'}
        det = (f'sem match (paginas={diag.get("paginas_lidas")}, '
               f'pedidos_vistos={diag.get("pedidos_vistos")})')
        _log('nao_encontrado', det)
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
