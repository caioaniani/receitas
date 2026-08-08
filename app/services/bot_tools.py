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


# ── LEGADO VNDA (catálogo) — o bot NÃO usa mais isto desde 19/06/2026 (migrou
# pra loja_catalogo/opao.online). Mantido durante a transição em paralelo do
# VNDA: ainda referenciado pelo diag do PDV (`_catalogo_cache`) e por teste do
# parser. Remover no cleanup quando o VNDA for desligado de vez. ─────────────
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


def _base_loja():
    """Base URL pública da loja (opao.online) pra links ABSOLUTOS — o bot
    atende canais externos (WhatsApp/IG), então o link tem que ser completo."""
    from flask import current_app
    return (current_app.config.get('LOJA_BASE_URL')
            or 'https://opao.online').rstrip('/')


def _fmt_item_catalogo(it, base):
    """Item do `loja_catalogo` → dict enxuto pro bot. `disponivel` reflete o
    ESTOQUE REAL da loja do site agora (não mais o flag bugado do VNDA)."""
    d = {
        'nome': it['nome'],
        'kind': it['kind'],
        'id': it['id'],
        'preco': it['preco'],
        'disponivel': not it.get('esgotado', False),
        'descricao': it.get('descricao') or '',
        'categoria': it.get('categoria') or '',
        'url': base + it['href'],
    }
    # Gramagem por unidade quando a ficha tem (auditoria 03/07: cliente
    # perguntou o peso da Focaccia e o bot nao sabia -> handoff evitavel).
    if it.get('peso_g'):
        d['peso_g'] = it['peso_g']
    return d


def _datas_indisponiveis(it, saldos):
    """Datas dd/mm (janela do plano) em que o plano-do-dia ZEROU o item.
    Sob encomenda TAMBÉM entra desde 07/08/2026 (decisão do dono — o plano
    passou a valer pra encomenda; sem isto o bot afirmaria disponível num
    dia curado e o vigia acusaria falso erro na direção oposta)."""
    chave = (it['kind'], it['id'])
    out = []
    for d in sorted(saldos):
        saldo = saldos[d].get(chave)
        if saldo is not None and saldo <= 0:
            out.append(d.strftime('%d/%m'))
    return out


def _saldos_janela_plano():
    """{data: {(kind,id): saldo}} do plano-do-dia pros próximos 14 dias —
    uma query, compartilhada por consultar_produtos e o vigia."""
    from datetime import timedelta

    from app.services import loja_catalogo, loja_plano_dia
    from app.utils import hoje
    di = hoje()
    janela = getattr(loja_catalogo, '_JANELA_DIAS_FUTUROS', 14)
    return loja_plano_dia.saldos_no_periodo(di, di + timedelta(days=janela - 1))


def consultar_produtos(busca):
    """Busca no catálogo PRÓPRIO (opao.online, via `loja_catalogo`). Retorna
    {'produtos': [{nome, kind, id, preco, disponivel, descricao, categoria,
    url, itens?, indisponivel_em?}]} ou {'erro': ...}.

    `disponivel` = ESTOQUE REAL agora (fim do "bug do site"). Match focado
    (achou pelo termo) inclui `descricao` e, em cesta, `itens` (composição) —
    pra responder "o que vem na cesta X?" — e `indisponivel_em` (datas dd/mm
    em que o plano-do-dia zerou o item; caso Dia dos Pais: só cestas pra
    09/08, o bot precisa saber negar POR DATA com base em dado). Sem match:
    catálogo amplo token-light (sem descrição/itens/datas) pro Claude aplicar
    sinônimos ("amendoas" → "Almond"). Identidade = kind+id (não SKU)."""
    from app.services import loja_catalogo
    from app.utils import normalizar_busca
    try:
        catalogo = loja_catalogo.anotar_esgotado(
            loja_catalogo.produtos_publicados())
    except Exception as exc:  # noqa: BLE001
        logger.exception('consultar_produtos (loja própria) falhou')
        return {'erro': str(exc)}
    base = _base_loja()
    termos = [t for t in normalizar_busca(busca or '').split()
              if len(t) > 2 and t not in _STOPWORDS]
    if termos:
        filtrados = [it for it in catalogo
                     if any(t in normalizar_busca(it['nome']) for t in termos)]
        if filtrados:
            try:
                saldos = _saldos_janela_plano()
            except Exception:  # noqa: BLE001 — best-effort, sem datas
                logger.exception('consultar_produtos: plano-do-dia falhou')
                saldos = {}
            out = []
            for it in filtrados[:40]:
                d = _fmt_item_catalogo(it, base)
                datas = _datas_indisponiveis(it, saldos)
                if datas:
                    d['indisponivel_em'] = datas
                if it['kind'] == 'produto':  # cesta: anexa composição
                    det = loja_catalogo.por_id_publicado('produto', it['id'])
                    if det and det.get('itens'):
                        d['itens'] = det['itens']
                out.append(d)
            return {'produtos': out}
    # Sem match: catálogo amplo, token-light (sem descrição/itens).
    return {'produtos': [{k: v for k, v in _fmt_item_catalogo(it, base).items()
                          if k != 'descricao'} for it in catalogo[:80]]}


def catalogo_disponibilidade():
    """[{nome, disponivel, indisponivel_em}] do catálogo do site — usado pela
    vigia pra comparar o que o bot disse com a MESMA fonte que o bot consulta
    (antes era o VNDA). Devolve None se o catálogo falhar.

    `disponivel` é a disponibilidade GERAL (hoje/próximos dias, visão da
    vitrine). `indisponivel_em` lista as DATAS (dd/mm, próximos 14 dias) em
    que o plano-do-dia ZEROU o item — a verdade POR DATA de entrega, a mesma
    que o site/checkout aplicam. Caso real 04/08/2026 (véspera do Dia dos
    Pais): plano de 09/08 só com cestas; o bot dizia CERTO "croissant não
    disponível pra 09/08" e o vigia, olhando só a disponibilidade geral,
    acusava erro do bot (falso ALTA). Sob encomenda nunca fica indisponível
    (produzido pro pedido)."""
    from app.services import loja_catalogo
    try:
        catalogo = loja_catalogo.anotar_esgotado(
            loja_catalogo.produtos_publicados())
        saldos = _saldos_janela_plano()
    except Exception:  # noqa: BLE001
        logger.exception('catalogo_disponibilidade falhou')
        return None
    return [{'nome': it['nome'],
             'disponivel': not it.get('esgotado', False),
             'indisponivel_em': _datas_indisponiveis(it, saldos)}
            for it in catalogo]


def consultar_ingredientes(nome_produto):
    """Consulta a Receita por nome (fuzzy) e devolve a lista de ingredientes
    pra o bot responder duvidas de gluten/lactose/ovo/origem animal de forma
    HONESTA (sem chutar). Filtra ingredientes < 0.5% (irrelevantes pro cliente
    e ruido na resposta).

    Retorna SOMENTE os NOMES dos ingredientes (sem percentual) — receita
    com percentuais e segredo industrial. Pra resposta de alergia
    ("tem leite?", "tem ovo?") a lista de nomes ja cobre 100% — percentual
    nao acrescenta nada e abre risco de concorrente raspar receita pelo bot.

    Retorna:
      {'receita': str, 'ingredientes': [{'nome': str}, ...]}
      {'erro': 'nao_encontrado', 'sugestoes': [str]} — match falhou; sugere
        os 5 nomes mais proximos pra o bot esclarecer com o cliente.
      {'erro': str} — falha de DB ou tabela vazia.

    Nao retorna a receita inteira (peso, modo de preparo, percentuais) —
    so a lista que importa pra alergia/restricao. NUNCA usar pra alergia
    confirmada (regra do prompt: alergia = handoff sempre)."""
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
        # Filtra ingredientes irrelevantes (< 0.5%) e ordena por importancia
        # internamente — mas NAO expoe o percentual no retorno.
        relevantes = []
        for ing in (match.ingredientes or []):
            pct = float(ing.porcentagem or 0)
            if pct < 0.5:
                continue
            nome = (ing.ingrediente_nome or '').strip()
            if nome:
                relevantes.append((pct, nome))
        relevantes.sort(reverse=True)
        ings = [{'nome': nome} for _pct, nome in relevantes]
        return {'receita': match.nome, 'ingredientes': ings}
    except Exception as exc:  # noqa: BLE001
        logger.exception('consultar_ingredientes falhou nome=%r', nome_produto)
        return {'erro': str(exc)}


def gerar_link_carrinho(itens):
    """itens: lista de {'kind': 'receita'|'produto', 'id': int, 'qtd': int}.
    Monta o link de 1 CLIQUE que enche o carrinho do opao.online e leva pro
    checkout: /loja/carrinho?add=r5:2,p83:1 (r=receita, p=produto). Cesta
    também entra aqui (kind=produto) — um link só pra avulsos + cesta.

    Determinístico — tira do Claude o risco de montar URL errada. O carrinho
    resolve preço/estoque no SERVIDOR (cliente não dita valor). kind+id vêm do
    consultar_produtos."""
    partes = []
    for it in (itens or []):
        kind = str(it.get('kind') or '').strip().lower()
        letra = {'receita': 'r', 'produto': 'p'}.get(kind)
        if not letra:
            continue
        try:
            iid = int(it.get('id'))
        except (TypeError, ValueError):
            continue
        try:
            qtd = max(1, int(it.get('qtd') or it.get('quantidade') or 1))
        except (TypeError, ValueError):
            qtd = 1
        partes.append(f'{letra}{iid}:{qtd}')
    if not partes:
        return {'erro': 'nenhum item válido'}
    return {'link': f'{_base_loja()}/loja/carrinho?add=' + ','.join(partes)}


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


def _nf_pedido_online(cpf_digits, numero):
    """NF de pedido NATIVO do site (PedidoOnline). Devolve:
      - None: número não é nosso → o caller tenta o Tiny (VNDA legado).
      - (resultado_log, detalhe, payload): caso resolvido (com ou sem NF).
    Autoriza por CPF do comprador (Cliente.cpf) — sem isso NÃO expõe NF de
    outro cliente."""
    from app.models import PedidoOnline
    p = PedidoOnline.query.filter_by(codigo=numero).first()
    if not p:
        return None
    cpf_pedido = ''.join(
        c for c in ((p.cliente.cpf if p.cliente else '') or '') if c.isdigit())
    if not cpf_pedido or cpf_pedido != cpf_digits:
        # Não confirma que o pedido existe — mesma resposta de "não bateu".
        return ('nao_encontrado', 'cpf nao bate (pedido online)',
                {'erro': 'nao_encontrado',
                 'mensagem': 'Não encontrei pedido com esse CPF e número. '
                             'Confere os dados, por favor.'})
    if not p.tiny_nota_fiscal_id:
        return ('sem_nf', f'pedido online {numero} sem NF (status={p.status})',
                {'erro': 'sem_nf_ainda', 'situacao': p.status,
                 'mensagem': 'Achei seu pedido, mas a nota ainda não foi '
                             'emitida. Ela sai junto com o despacho — te aviso '
                             'ou você pode pedir depois.'})
    from app.services import tiny_nf
    link = tiny_nf.link_danfe(p)
    if not link:
        return ('erro', f'link_danfe falhou (pedido online {numero})',
                {'erro': 'link_falhou',
                 'mensagem': 'Achei a nota mas não consegui gerar o link agora. '
                             'Já passo pra um atendente.'})
    return ('enviada', f'pedido online {numero}',
            {'link': link, 'numero_pedido': p.codigo})


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

    # 1. Pedido NATIVO do site (PedidoOnline) primeiro.
    online = _nf_pedido_online(cpf_d, numero)
    if online is not None:
        resultado, detalhe, payload = online
        _log(resultado, detalhe)
        return payload

    # 2. Fallback Tiny (pedido do site antigo/VNDA, por CPF+numero).
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


def _autorizar_pedido(code, telefone_contato, cpf_cliente):
    """Verifica que o cliente da conversa eh DONO do pedido antes de liberar
    dado/edicao. Sem isso, qualquer pessoa que digite um numero VNDA valido
    recebe status/itens/total — vazamento real (14/06/2026: dono confirmou
    como risco alto). Defesa em camadas:

    1. Se temos telefone do contato no canal (Chatwoot WhatsApp) E o pedido
       tem telefone no shipping_address E batem (canonico via
       `telefone_chave`): AUTORIZADO. Sem fricao pro cliente honesto.
    2. Se cliente forneceu CPF E bate com CPF do comprador no pedido:
       AUTORIZADO. Caminho para canal sem telefone (site/IG).
    3. Caso contrario: NAO AUTORIZADO. Retorna instrucao pro bot pedir CPF.

    Fail-closed: se nem telefone nem CPF batem, devolve nao_autorizado
    (e nao 'erro generico' — pra o bot distinguir e pedir CPF, sem revelar
    que o pedido existe).

    Retorna:
      {'ok': True, 'order': dict_normalizado_minimo} — autorizado, pode usar
      {'erro': 'pedido_nao_encontrado'} — pedido nao existe
      {'erro': 'autorizacao_necessaria', 'instrucao': str} — pedido existe
        mas dono nao confirmado; instrucao orienta o bot a pedir CPF.
      {'erro': 'vnda_indisponivel'}
    """
    from app.utils import telefone_chave
    try:
        order = vnda.buscar_pedido_completo(code)
    except Exception:  # noqa: BLE001
        logger.exception('_autorizar_pedido: VNDA falhou code=%r', code)
        return {'erro': 'vnda_indisponivel'}
    if not order:
        return {'erro': 'pedido_nao_encontrado'}

    # 1. Match por telefone do contato (Chatwoot WhatsApp). Canoniza nos 2
    # lados — `telefone_do_pedido` ja canoniza, mas defensivo evita bug
    # silencioso se a impl mudar.
    tel_contato = telefone_chave(telefone_contato or '')
    if tel_contato:
        try:
            tel_pedido = telefone_chave(vnda.telefone_do_pedido(code) or '')
        except Exception:  # noqa: BLE001
            logger.exception('_autorizar_pedido: telefone_do_pedido falhou')
            tel_pedido = ''
        if tel_pedido and tel_pedido == tel_contato:
            return {'ok': True, 'order': order}

    # 2. Match por CPF.
    cpf_digits = ''.join(c for c in (cpf_cliente or '') if c.isdigit())
    if cpf_digits:
        try:
            cpf_pedido = vnda.cpf_do_pedido(code)
        except Exception:  # noqa: BLE001
            logger.exception('_autorizar_pedido: cpf_do_pedido falhou')
            cpf_pedido = ''
        if cpf_pedido and cpf_pedido == cpf_digits:
            return {'ok': True, 'order': order}

    return {
        'erro': 'autorizacao_necessaria',
        'instrucao': ('Peca o CPF do comprador do pedido pra confirmar que '
                       'voce esta falando com o dono. So depois chame a tool '
                       'de novo passando cpf_cliente=<cpf informado>.'),
    }


# Status do PedidoOnline → texto amigável pro cliente final.
_STATUS_ONLINE_CLIENTE = {
    'aguardando_pagamento': 'aguardando pagamento',
    'pago': 'pago, em preparo',
    'em_preparo': 'em preparo',
    'a_caminho': 'saiu para entrega (a caminho)',
    'entregue': 'entregue',
    'cancelado': 'cancelado',
}

_AUTORIZACAO_INSTRUCAO = (
    'Peca o CPF do comprador do pedido pra confirmar que voce esta falando '
    'com o dono. So depois chame a tool de novo passando cpf_cliente=<cpf '
    'informado>.')


def _consultar_pedido_online(code, telefone_contato, cpf_cliente):
    """Pedido NATIVO do site (PedidoOnline). Devolve:
      - None: não é um pedido nosso → o caller tenta o VNDA (transição).
      - {'erro': 'autorizacao_necessaria', ...}: é nosso, mas o dono não bate.
      - dict do pedido: autorizado.
    Autoriza por telefone do canal (cliente OU destinatário) ou pelo CPF do
    comprador (Cliente.cpf) — mesma regra do VNDA."""
    from app.models import PedidoOnline
    from app.utils import telefone_chave
    p = PedidoOnline.query.filter_by(codigo=code).first()
    if not p:
        return None
    autorizado = False
    tel_contato = telefone_chave(telefone_contato or '')
    if tel_contato:
        for tel in (p.telefone_cliente, p.telefone_destinatario):
            if tel and telefone_chave(tel) == tel_contato:
                autorizado = True
                break
    if not autorizado:
        cpf_d = ''.join(c for c in (cpf_cliente or '') if c.isdigit())
        cpf_pedido = ''.join(
            c for c in ((p.cliente.cpf if p.cliente else '') or '')
            if c.isdigit())
        if cpf_d and cpf_pedido and cpf_d == cpf_pedido:
            autorizado = True
    if not autorizado:
        return {'erro': 'autorizacao_necessaria',
                'instrucao': _AUTORIZACAO_INSTRUCAO}
    # Valores DETALHADOS e rotulados (auditor 06/07/2026): o bot mostrava
    # "R$138" e "R$148" sem dizer o que era cada um e o cliente lia como
    # contradição. Com subtotal/frete/preço unitário explícitos, o bot nunca
    # precisa inferir número — só rotular: "itens + frete = total".
    total = float(p.valor_total or 0)
    frete = float(p.frete_valor or 0)
    # Rastreio AO VIVO + link fixo da página do pedido (08/08/2026, dono:
    # "treinar o bot pra instruir o cliente a rastrear"). Só chega aqui
    # AUTORIZADO (mesmo gate da cartinha). Usa o gate CANÔNICO da página
    # (`_rastreio_do_pedido`, fonte única): retirada/cancelado/
    # aguardando_pagamento/divulgação ficam SEM rastreio — achado de
    # revisão: sem o gate, pedido CANCELADO com atribuição viva saía
    # "a_caminho" no mesmo dict do status oficial e o modelo podia ditar
    # posição de entrega cancelada. Best-effort: rastreio é bônus, nunca
    # derruba a consulta. SEM horário estimado (decisão do dono de 08/08).
    rastreio = None
    try:
        from app.blueprints.loja.routes import _rastreio_do_pedido
        rastreio = _rastreio_do_pedido(p)
    except Exception:  # noqa: BLE001
        logger.exception('bot: rastreio do pedido %s falhou', code)
    return {
        'numero': p.codigo,
        'status': _STATUS_ONLINE_CLIENTE.get(p.status, p.status),
        'total': total,
        'frete': frete,
        'subtotal_itens': round(total - frete, 2),
        'data_entrega': (p.data_entrega.strftime('%d/%m/%Y')
                         if p.data_entrega else None),
        'periodo': p.janela_entrega or None,
        'itens': [{'nome': i.nome, 'qtd': i.quantidade,
                   'preco_unit': float(i.preco_unitario or 0)}
                  for i in p.itens],
        # So chega aqui AUTORIZADO (telefone/CPF do dono do pedido) — mostrar
        # a cartinha pro proprio comprador e seguro e resolve a confirmacao
        # pos-compra sem handoff (auditor 06/07/2026: 2 handoffs de cartinha
        # que a tool ja respondia).
        'cartinha': (p.cartinha or '').strip() or None,
        'rastreio': rastreio,
        'link_acompanhamento': f'{_base_loja()}/loja/pedido/{p.codigo}',
        'como_apresentar': ('Ao citar valores, SEMPRE rotule: '
                            '"itens R$ X + frete R$ Y = total R$ Z". '
                            'Nunca mostre dois números sem dizer o que são. '
                            'Pergunta sobre ENTREGA: mande o '
                            'link_acompanhamento (a página atualiza sozinha) '
                            'e, se rastreio.fase=a_caminho com parada, diga '
                            'a posição na rota. NUNCA prometa horário.'),
    }


def _consultar_pedido_vnda(code, telefone_contato, cpf_cliente):
    """Fallback VNDA (pedidos do site antigo, em paralelo na transição).

    A data vem de vnda._extrair_data_entrega, que prioriza a data AGENDADA no
    checkout (extra.DataDeEntrega) — e NAO o expected_delivery_date do VNDA, que
    e o campo bugado por tras do "pedido pode ser entregue hoje" no site."""
    auth = _autorizar_pedido(code, telefone_contato, cpf_cliente)
    if auth.get('erro'):
        return auth
    order = auth['order']
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


# Busca por TELEFONE do canal quando o cliente nao sabe o numero (19/07/2026,
# auditor: handoff por "pedido nao encontrado" sem o bot ter como procurar).
# Fail-closed: SO o telefone verificado do canal (Chatwoot WhatsApp) localiza
# — e a MESMA credencial que ja autoriza a exibicao do pedido. Nome NUNCA
# busca (nao e prova de identidade; furaria o anti-enumeracao) e CPF segue
# apenas como autorizacao de pedido ja localizado.
_PEDIDOS_POR_TELEFONE_DIAS = 90
_PEDIDOS_POR_TELEFONE_MAX = 3


def _pedidos_recentes_por_telefone(telefone_contato, cpf_cliente):
    """Localiza os PedidoOnline recentes do telefone do canal.

    1 achado  -> devolve a ficha completa direto (mesma forma da consulta por
                 numero — o bot responde na hora).
    2-3       -> lista compacta pro bot perguntar qual e.
    0 / sem telefone no canal -> erro orientando a pedir o numero.

    SO o telefone do COMPRADOR (telefone_cliente) localiza — o do
    DESTINATARIO fica FORA da descoberta de proposito (achado da revisao
    19/07/2026): presente-surpresa com o telefone de quem recebe no
    cadastro seria DESCOBERTO pela propria pessoa ("tem pedido pra mim?"
    revelaria itens e cartinha — mesma classe do caso 13/07 que estragou
    surpresa). Destinatario que JA TEM o numero segue autorizado no fluxo
    por numero (`_consultar_pedido_online`, inalterado).

    `telefone_chave` e Python-side (nao SQL) — filtramos em memoria a janela
    recente (streaming `yield_per`, para no cap), mesmo padrao do card CRM
    (`crm/routes._buscar_por_telefone`)."""
    from datetime import timedelta

    from app.models import PedidoOnline
    from app.utils import agora, telefone_chave
    tel = telefone_chave(telefone_contato or '')
    if not tel:
        return {'erro': ('sem telefone verificado neste canal não dá pra '
                          'localizar — peça o número do pedido ao cliente')}
    corte = agora() - timedelta(days=_PEDIDOS_POR_TELEFONE_DIAS)
    achados = []
    q = (PedidoOnline.query
         .filter(PedidoOnline.criado_em >= corte)
         .order_by(PedidoOnline.criado_em.desc())
         .yield_per(200))
    for p in q:
        t = p.telefone_cliente
        if t and telefone_chave(t) == tel:
            achados.append(p)
        if len(achados) >= _PEDIDOS_POR_TELEFONE_MAX:
            break
    if not achados:
        return {'erro': 'nenhum_pedido_para_este_telefone',
                'instrucao': ('Nenhum pedido recente no telefone deste '
                               'WhatsApp. Peça o número do pedido (pode ter '
                               'sido feito com outro telefone). Se o cliente '
                               'não tiver o número, transfira.')}
    if len(achados) == 1:
        # Telefone do canal bate = mesma autorizacao do fluxo por numero.
        return _consultar_pedido_online(
            achados[0].codigo, telefone_contato, cpf_cliente)
    return {
        'pedidos_recentes': [{
            'numero': p.codigo,
            'status': _STATUS_ONLINE_CLIENTE.get(p.status, p.status),
            'feito_em': (p.criado_em.strftime('%d/%m/%Y')
                         if p.criado_em else None),
            'data_entrega': (p.data_entrega.strftime('%d/%m/%Y')
                             if p.data_entrega else None),
        } for p in achados],
        'instrucao': ('Mais de um pedido recente neste telefone. Pergunte '
                       'ao cliente qual é (pelo número ou pela data) e chame '
                       'consultar_pedido com o número escolhido.'),
    }


def consultar_pedido(numero, *, telefone_contato=None, cpf_cliente=None):
    """Status + DATA DE ENTREGA de um pedido pelo número — ou, SEM número,
    pelos pedidos recentes do telefone do canal (fail-closed).

    Com número: procura PRIMEIRO no NOSSO banco (PedidoOnline, opao.online);
    se o número não for de lá, cai pro VNDA (site antigo, em paralelo na
    transição).

    AUTORIZACAO (nos dois): exige que o solicitante seja o dono — match por
    telefone do canal (Chatwoot) OU CPF informado. Sem isso devolve
    autorizacao_necessaria (NAO expoe que o pedido existe)."""
    code = str(numero or '').strip()
    if not code:
        return _pedidos_recentes_por_telefone(telefone_contato, cpf_cliente)
    nativo = _consultar_pedido_online(code, telefone_contato, cpf_cliente)
    if nativo is not None:
        return nativo
    return _consultar_pedido_vnda(code, telefone_contato, cpf_cliente)


# ── Lead B2B / atacado (16/07/2026; fluxo revisto 20/07/2026) ──────────────
# O bot captura contato de quem quer atacado (revenda, cafeteria,
# restaurante), registra o lead, TRANSFERE a conversa pra equipe e o dono
# recebe WhatsApp com o link da conversa. Decisão do dono 20/07/2026: o bot
# NÃO envia catálogo/cardápio B2B — material e preços são só com a equipe.

_RE_EMAIL_LEAD = None  # compilado sob demanda (regex simples de formato)


def _email_lead_valido(email):
    import re
    global _RE_EMAIL_LEAD
    if _RE_EMAIL_LEAD is None:
        _RE_EMAIL_LEAD = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]{2,}$')
    email = (email or '').strip()
    # Limite da coluna (String(200)) — e-mail maior que isso e invalido de
    # qualquer forma; sem o cap, o commit estourava VARCHAR no Postgres e
    # deixava a sessao do webhook quebrada (achado do revisor).
    if len(email) > 200:
        return False
    return bool(_RE_EMAIL_LEAD.match(email))


def registrar_lead_b2b(nome, email, telefone, empresa=None, interesse=None,
                       telefone_contato=None, conversa_id=None):
    """Registra um lead de atacado/B2B e avisa o dono no WhatsApp na hora
    (com o LINK da conversa, pra ele acompanhar o atendente).

    Validações: e-mail com formato real; telefone celular BR com DDD (10-13
    dígitos, com ou sem o 55). `telefone_contato` (injetado pelo canal — o
    WhatsApp de onde o cliente fala) serve de fallback quando o cliente diz
    "esse número mesmo". Dedupe: mesmo e-mail OU telefone nas últimas 24h
    ATUALIZA o lead em vez de duplicar (o cliente corrigir um dado no turno
    seguinte não vira lead novo).
    """
    from datetime import timedelta

    from app.extensions import db
    from app.models import LeadB2B
    from app.utils import agora, normalizar_telefone

    nome = (nome or '').strip()[:150]   # cap da coluna String(150)
    email = (email or '').strip().lower()
    fone = normalizar_telefone(telefone)
    if not fone and telefone_contato:
        fone = normalizar_telefone(telefone_contato)
    if len(nome) < 2:
        return {'erro': 'Peça o nome do cliente antes de registrar.'}
    if not _email_lead_valido(email):
        return {'erro': ('E-mail com formato inválido — confirme com o '
                         'cliente (ex: nome@dominio.com).')}
    # 10-13 digitos DE PROPOSITO (mais frouxo que o _whatsapp_valido do
    # wifi_portal): lead B2B pode passar telefone FIXO comercial da empresa
    # — barrar por "nao e celular" perderia o contato. O dono ve e decide.
    if not (10 <= len(fone) <= 13):
        return {'erro': ('Telefone inválido — peça o WhatsApp com DDD '
                         '(ex: 11 99999-8888).')}

    corte = agora() - timedelta(hours=24)
    # Dedupe EMAIL-primeiro (revisor): o e-mail e a identidade do lead. So
    # cai no match por telefone quando nenhum lead recente tem esse e-mail
    # (caso legitimo: cliente corrigiu o e-mail no turno seguinte). Match
    # "email OU telefone" num filtro so podia casar o lead ERRADO e
    # sobrescrever o e-mail de outro contato.
    lead = (LeadB2B.query
            .filter(LeadB2B.email == email, LeadB2B.criado_em >= corte)
            .order_by(LeadB2B.criado_em.desc()).first())
    if lead is None:
        lead = (LeadB2B.query
                .filter(LeadB2B.telefone == fone,
                        LeadB2B.criado_em >= corte)
                .order_by(LeadB2B.criado_em.desc()).first())
    atualizado = lead is not None
    if lead is None:
        lead = LeadB2B(nome=nome, email=email, telefone=fone)
        db.session.add(lead)
    lead.nome = nome or lead.nome
    lead.email = email
    lead.telefone = fone
    if empresa:
        lead.empresa = (empresa or '').strip()[:200]
    if interesse:
        lead.interesse = (interesse or '').strip()[:2000]
    if conversa_id:
        lead.conversa_id = conversa_id
    db.session.commit()

    _avisar_dono_lead_b2b(lead, atualizado)

    return {'ok': True,
            'lead_id': lead.id,
            'ja_registrado': atualizado,
            'proximo_passo': ('Contato registrado. Agora TRANSFIRA a '
                              'conversa pra equipe (o atendente continua '
                              'com o cliente).')}


def _avisar_dono_lead_b2b(lead, atualizado):
    """WhatsApp pro dono a cada lead novo (best-effort — nunca quebra o
    atendimento). Mesmo padrão do aviso de estoque do wifi_portal."""
    from flask import current_app

    from app.extensions import db
    from app.services import zapi
    try:
        numero = (current_app.config.get('ZAPI_BOT_DONO_NUMERO') or '').strip()
        if not numero:
            return
        rotulo = 'atualizado' if atualizado else 'NOVO'
        partes = [f'🏢 Lead B2B {rotulo} pelo bot:',
                  f'• {lead.nome}' + (f' ({lead.empresa})' if lead.empresa else ''),
                  f'• WhatsApp: {lead.telefone}',
                  f'• E-mail: {lead.email}']
        if lead.interesse:
            partes.append(f'• Interesse: {lead.interesse[:300]}')
        # Link direto da CONVERSA (decisão do dono 20/07/2026): o bot
        # transfere pra equipe logo após registrar — o dono acompanha.
        if lead.conversa_id:
            from app.services.chatbot_vigia import link_chatwoot
            cw = link_chatwoot(lead.conversa_id)
            if cw:
                partes.append(f'Conversa: {cw}')
        base = (current_app.config.get('APP_BASE_URL') or '').rstrip('/')
        partes.append(f'Lista completa: {base}/b2b/leads' if base
                      else 'Lista completa: /b2b/leads')
        zapi.enviar_texto(numero, '\n'.join(partes))
    except Exception:  # noqa: BLE001
        logger.exception('aviso de lead B2B falhou')
        db.session.rollback()
