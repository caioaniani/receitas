"""Mapeamento e emissão de NF-e via Tiny (loja online + B2B).

MAPEAMENTO: ligar cada item vendável (Receita/Produto — publicado no site
OU vendido no B2B) ao SKU dele no Tiny. A emissão de NF usa esse mapa pra
mandar a nota pro Tiny por SKU (o Tiny aplica NCM/CFOP/CST do cadastro do
produto — fiscal não mora aqui). O B2B (`tiny_nf_b2b`) reusa o mesmo mapa
e o motor `emitir_nf_generico` daqui.

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


def mapa_por_item(canal='site'):
    """Dict {(kind, item_id): TinyProdutoMap} do que já tem registro no
    canal. O mapeamento é POR CANAL: no Tiny o B2B é outro cadastro/lista
    de preço, então o mesmo item pode ter SKUs diferentes."""
    return {(m.kind, m.item_id): m
            for m in TinyProdutoMap.query.filter_by(canal=canal).all()}


def sku_do_item(kind, item_id, canal='site'):
    """SKU do Tiny pra um item nosso NO CANAL, ou None se não mapeado."""
    m = TinyProdutoMap.query.filter_by(canal=canal, kind=kind,
                                       item_id=item_id).first()
    return (m.tiny_sku or '').strip() if m and m.tiny_sku else None


def definir_sku(kind, item_id, sku, tiny_nome=None, user_id=None,
                canal='site'):
    """Upsert do SKU de um item no canal. SKU vazio = volta a pendente."""
    sku = (sku or '').strip()
    m = TinyProdutoMap.query.filter_by(canal=canal, kind=kind,
                                       item_id=item_id).first()
    if not m:
        m = TinyProdutoMap(canal=canal, kind=kind, item_id=item_id)
        db.session.add(m)
    m.tiny_sku = sku or None
    if tiny_nome is not None:
        m.tiny_nome = tiny_nome
    m.auto_match = False  # definido por humano
    m.confirmado_em = agora() if sku else None
    m.confirmado_por = user_id if sku else None
    db.session.commit()
    return m


def _itens_b2b():
    """Itens vendáveis no B2B: receitas com preço de atacado
    (`Receita.preco_venda`), produtos com `preco_atacado`, e qualquer item
    que já apareceu numa VendaB2B (venda avulsa de item sem preço de
    atacado cadastrado). É o universo da tela /b2b/tiny-skus — sem essa
    lista, item vendido só no atacado nunca apareceria pra mapear e a
    emissão de NF B2B ficava travada sem saída na UI."""
    from app.models import Produto, Receita, VendaB2BItem

    receitas = (Receita.query
                .filter(Receita.arquivada_em.is_(None),
                        Receita.preco_venda.isnot(None),
                        Receita.preco_venda > 0)
                .order_by(Receita.nome.asc()).all())
    produtos = (Produto.query
                .filter(Produto.ativo.is_(True),
                        Produto.preco_atacado.isnot(None),
                        Produto.preco_atacado > 0)
                .order_by(Produto.nome.asc()).all())
    out = [{'kind': 'receita', 'id': r.id, 'nome': r.nome,
            'categoria': r.categoria or ''} for r in receitas]
    out += [{'kind': 'produto', 'id': p.id, 'nome': p.nome,
             'categoria': p.categoria or ''} for p in produtos]
    # Itens já vendidos em VendaB2B (mesmo sem preço de atacado cadastrado):
    # se tem venda, pode precisar de NF — precisa aparecer pra mapear.
    ja = {(it['kind'], it['id']) for it in out}
    vendidos_r = {rid for (rid,) in VendaB2BItem.query
                  .with_entities(VendaB2BItem.receita_id)
                  .filter(VendaB2BItem.receita_id.isnot(None))
                  .distinct().all()}
    vendidos_p = {pid for (pid,) in VendaB2BItem.query
                  .with_entities(VendaB2BItem.produto_id)
                  .filter(VendaB2BItem.produto_id.isnot(None))
                  .distinct().all()}
    for r in Receita.query.filter(Receita.id.in_(vendidos_r)).all():
        if ('receita', r.id) not in ja:
            out.append({'kind': 'receita', 'id': r.id, 'nome': r.nome,
                        'categoria': r.categoria or ''})
    for p in Produto.query.filter(Produto.id.in_(vendidos_p)).all():
        if ('produto', p.id) not in ja:
            out.append({'kind': 'produto', 'id': p.id, 'nome': p.nome,
                        'categoria': p.categoria or ''})
    return out


def _itens_do_canal(canal):
    """Universo de itens mapeáveis do canal: 'site' = publicados na vitrine
    (preco_site > 0); 'b2b' = catálogo de atacado (ver `_itens_b2b`). Cada
    canal tem a própria lista de preço/cadastro no Tiny — por isso telas e
    mapas separados."""
    if canal == 'b2b':
        return _itens_b2b()
    return [{'kind': it['kind'], 'id': it['id'], 'nome': it['nome'],
             'categoria': it.get('categoria') or ''}
            for it in loja_catalogo.produtos_publicados()]


def itens_para_mapear(canal='site'):
    """Lista os itens do canal com o estado do mapeamento Tiny.
    Devolve [{kind,id,nome,categoria,sku,estado,confirmado}]."""
    mp = mapa_por_item(canal)
    out = []
    for it in _itens_do_canal(canal):
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


def _aplicar_pares(pares, user_id=None, canal='site'):
    """Aplica uma lista de (nome_tiny, sku) ao mapeamento dos itens DO
    CANAL. Match EXATO (nome normalizado igual) → confirma automático;
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

    mp = mapa_por_item(canal)
    exatos = sugeridos = sem_match = 0
    for it in _itens_do_canal(canal):
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
            m = TinyProdutoMap(canal=canal, kind=it['kind'], item_id=it['id'])
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


def sincronizar_sugestoes(user_id=None, canal='site'):
    """Busca o catálogo do Tiny (API) e mapeia por nome os itens do canal.
    Match exato confirma automático; parecido vira sugestão. Devolve
    contadores ou {erro}."""
    produtos_tiny = tiny.listar_produtos()
    if not produtos_tiny:
        return {'erro': 'Tiny não respondeu (token ausente ou rede). '
                'Dica: você pode importar a planilha de produtos do Tiny.'}
    pares = [(p['nome'], p['sku']) for p in produtos_tiny if p.get('sku')]
    res = _aplicar_pares(pares, user_id=user_id, canal=canal)
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


# ── Emissão de NF (Fase 5 parte 2) ────────────────────────────────────
# Plano A: botão manual no admin. Quando o admin clica, monta o pedido a
# partir do PedidoOnline (cliente + itens via SKU + valores), cria no
# Tiny, gera a NF (rascunho) e dispara emissão. Em homologação as notas
# não têm valor fiscal — seguro pra testar.

def _so_digitos(s):
    return ''.join(c for c in (s or '') if c.isdigit())


def _payload_cliente(pedido):
    """Cliente da NF, COM endereco estruturado. A SEFAZ exige logradouro/
    numero/bairro/cidade/uf separados — sem eles a NF e' rejeitada
    ('endereco/bairro/cidade em branco'). O endereco vem do snapshot do
    pedido (entrega). Retirada nao coleta endereco -> campos vazios (NF de
    pedido de retirada exigiria coletar o endereco do cliente a parte)."""
    cli = getattr(pedido, 'cliente', None)
    cpf = _so_digitos(getattr(cli, 'cpf', '') if cli else '')
    return {
        'nome': pedido.nome_cliente,
        'tipo_pessoa': 'F',
        'cpf_cnpj': cpf,
        'email': pedido.email_cliente,
        'fone': pedido.telefone_cliente or '',
        'endereco': pedido.endereco_logradouro or '',
        'numero': pedido.endereco_numero or '',
        'complemento': pedido.endereco_complemento or '',
        'bairro': pedido.endereco_bairro or '',
        'cep': pedido.endereco_cep or '',
        'cidade': pedido.endereco_cidade or '',
        'uf': (pedido.endereco_uf or '').upper(),
    }


def _payload_itens(pedido):
    """Cada item -> {item: {codigo, descricao, quantidade, valor_unitario}}.
    Item sem SKU mapeado: ABORTA (não emite NF parcial)."""
    out, faltando = [], []
    for it in pedido.itens:
        sku = sku_do_item(it.kind, it.receita_id or it.produto_id)
        if not sku:
            faltando.append(it.nome)
            continue
        out.append({'item': {
            'codigo': sku,
            'descricao': it.nome[:120],
            'unidade': 'UN',
            'quantidade': float(it.quantidade),
            'valor_unitario': float(it.preco_unitario or 0),
        }})
    return out, faltando


def _nota_payload(pedido, itens):
    """Monta a NF pro `nota.fiscal.incluir` com o cabeçalho fiscal EXPLÍCITO:
    tipo de saída, natureza de operação e série.

    Por que aqui e não no pedido: o `gerar.nota.fiscal.pedido` não aplica a
    natureza do pedido na NF (deixava natOp vazio + série fora de ordem em
    prod). Criando a NF direto, nós mandamos natureza + série e o Tiny
    respeita. NCM/CFOP/CST continuam vindo do cadastro do produto via SKU."""
    from flask import current_app
    cfg = current_app.config
    return {
        'tipo': 'S',  # saída (venda)
        'natureza_operacao': cfg.get('NF_NATUREZA_OPERACAO',
                                     'Venda de mercadorias'),
        'serie': str(cfg.get('NF_SERIE', '1')),
        'data_emissao': agora().strftime('%d/%m/%Y'),
        'cliente': _payload_cliente(pedido),
        'itens': itens,
        'valor_frete': float(pedido.frete_valor or 0),
        # Modalidade do frete — obrigatório no Tiny. Letra, não número
        # ("0" vira vazio no PHP do Tiny). 'R' = por conta do emitente.
        'frete_por_conta': str(cfg.get('NF_FRETE_POR_CONTA', 'R')),
    }


def _sincronizar_situacao(pedido):
    """Consulta a situação REAL da NF no Tiny (fonte de verdade) e atualiza
    o pedido se já autorizou na SEFAZ. Devolve {autorizada, rejeitada,
    situacao} ou None se não temos NF / Tiny não respondeu.

    Por que: o `nota.fiscal.emitir` é assíncrono e o `status_processamento`
    é ambíguo (em prod a 011428 voltou status '2' mesmo já AUTORIZADA na
    SEFAZ). A única fonte confiável é `nota.fiscal.obter`, que reflete o
    que o Tiny tem armazenado. Texto, não número (mais robusto a mudança
    de código)."""
    if not pedido.tiny_nota_fiscal_id:
        return None
    nf = tiny.obter_nota_fiscal(pedido.tiny_nota_fiscal_id) or {}
    # Tiny pode mandar a situação em campos/formatos diferentes:
    # `situacao` (texto curto), `situacao_descricao` (texto longo),
    # `status` (numérico). Vamos varrer tudo pra ser robustos.
    sigs = ' '.join(
        str(nf.get(k) or '').strip().lower()
        for k in ('situacao', 'situacao_descricao', 'status',
                  'status_nfe', 'status_processamento')
    )
    if not sigs.strip():
        logger.info('tiny obter NF %s: sem situacao (campos=%s)',
                    pedido.tiny_nota_fiscal_id, list(nf.keys())[:20])
        return None
    autorizada = ('autoriz' in sigs) or ('emitida' in sigs)
    rejeitada = ('rejeit' in sigs) or ('denegad' in sigs)
    if not (autorizada or rejeitada):
        logger.info('tiny obter NF %s: situacao desconhecida (sigs=%r, '
                    'campos=%s)', pedido.tiny_nota_fiscal_id, sigs[:120],
                    list(nf.keys())[:20])
    if autorizada and not pedido.nf_emitida_em:
        pedido.nf_status = 'autorizada'
        pedido.nf_emitida_em = agora()
        db.session.commit()
    elif rejeitada and not pedido.nf_emitida_em:
        pedido.nf_status = sigs[:40]
        db.session.commit()
    return {'autorizada': autorizada, 'rejeitada': rejeitada, 'situacao': sigs}


def emitir_nf_generico(alvo, montar_payload, recriar=False):
    """Motor comum da emissão de NF via Tiny — usado pelo site (PedidoOnline)
    e pelo B2B (VendaB2B, ver `tiny_nf_b2b`). `alvo` precisa ter os campos
    `tiny_nota_fiscal_id` / `nf_status` / `nf_emitida_em`.

    `montar_payload` é chamado só quando precisamos CRIAR a nota — devolve
    (payload_dict, None) em sucesso ou (None, 'mensagem de erro').

    Fluxo (Plano B): nota.fiscal.incluir (cria a NF com natureza+série
    explícitas) → nota.fiscal.emitir (autoriza na SEFAZ) → obter (confirma
    a situação real, pq o status_processamento do emitir é ambíguo).
    Idempotente: NF já emitida COM SUCESSO (nf_emitida_em setado) não refaz.

    `recriar=True`: descarta a NF rascunho anterior (que a SEFAZ rejeitou) e
    cria uma nova do zero com o payload atual. Reemitir o MESMO rascunho não
    corrige dados — o rascunho ruim fica órfão no Tiny (apagável)."""
    if alvo.nf_emitida_em and alvo.tiny_nota_fiscal_id and not recriar:
        return {'ok': True, 'nota_fiscal_id': alvo.tiny_nota_fiscal_id,
                'msg': 'NF já emitida.'}
    if recriar:
        if hasattr(alvo, 'tiny_pedido_id'):
            alvo.tiny_pedido_id = None
        alvo.tiny_nota_fiscal_id = None
        alvo.nf_status = None
        alvo.nf_emitida_em = None
        db.session.commit()
    # ANTES de tentar emitir de novo: se já temos NF, ver se ela já autorizou
    # em background (caso da 011428 — status_processamento='2' enganoso). Isso
    # também é o que o botão "Reenviar / verificar" precisa fazer pra
    # sincronizar sem duplicar.
    if alvo.tiny_nota_fiscal_id:
        sit = _sincronizar_situacao(alvo)
        if sit and sit['autorizada']:
            return {'ok': True, 'nota_fiscal_id': alvo.tiny_nota_fiscal_id,
                    'msg': 'NF autorizada na SEFAZ.'}
        if sit and sit['rejeitada']:
            return {'ok': False,
                    'msg': f'NF rejeitada pela SEFAZ ({sit["situacao"]}). '
                           f'Use "Refazer do zero" para criar uma nova.'}
    # 1) Cria a NF (rascunho) com natureza + série explícitas, se ainda não
    #    temos uma. Resumível: se já criamos mas a emissão falhou, reusa o id.
    if not alvo.tiny_nota_fiscal_id:
        payload, erro = montar_payload()
        if erro:
            return {'ok': False, 'msg': erro}
        incl = tiny.incluir_nota_fiscal(payload)
        if not incl.get('ok'):
            return {'ok': False,
                    'msg': f'Falha ao criar a NF no Tiny: {incl.get("erro")}'}
        alvo.tiny_nota_fiscal_id = incl['id']
        # VendaB2B tem `nf_numero` (numero da NF, campo humano) — aproveita o
        # numero que o Tiny devolve. PedidoOnline nao tem o campo.
        if (incl.get('numero') and hasattr(alvo, 'nf_numero')
                and not alvo.nf_numero):
            alvo.nf_numero = incl['numero']
        db.session.commit()
    # 2) Autoriza na SEFAZ.
    emitir = tiny.emitir_nota_fiscal(alvo.tiny_nota_fiscal_id)
    alvo.nf_status = emitir.get('status') or 'enviada'
    if emitir.get('ok'):
        alvo.nf_emitida_em = agora()
        db.session.commit()
        return {'ok': True, 'nota_fiscal_id': alvo.tiny_nota_fiscal_id,
                'msg': f'NF emitida (status: {alvo.nf_status}).'}
    db.session.commit()
    # Emit retornou status ambíguo. Vai DIRETO no obter pra ver a verdade —
    # a NF pode ter autorizado em background mesmo o emitir retornando código
    # ambíguo (visto em prod com a 011428).
    sit = _sincronizar_situacao(alvo)
    if sit and sit['autorizada']:
        return {'ok': True, 'nota_fiscal_id': alvo.tiny_nota_fiscal_id,
                'msg': 'NF autorizada na SEFAZ.'}
    return {'ok': False,
            'msg': f'NF criada (id {alvo.tiny_nota_fiscal_id}) mas a emissão '
                   f'não foi confirmada: {emitir.get("erro")}'}


def emitir_nf(pedido, user_id=None, recriar=False):
    """Emite NF pro pedido da loja online. Devolve {ok, msg, nota_fiscal_id?}.
    Guard próprio do site: só pedido pago emite. O fluxo em si está em
    `emitir_nf_generico`."""
    if pedido.nf_emitida_em and pedido.tiny_nota_fiscal_id and not recriar:
        return {'ok': True, 'nota_fiscal_id': pedido.tiny_nota_fiscal_id,
                'msg': 'NF já emitida.'}
    if pedido.status != 'pago':
        return {'ok': False, 'msg': 'Pedido não está pago — não emite NF.'}

    def _montar():
        itens, faltando = _payload_itens(pedido)
        if faltando:
            return None, ('Itens sem SKU mapeado no Tiny: '
                          + ', '.join(faltando))
        return _nota_payload(pedido, itens), None

    return emitir_nf_generico(pedido, _montar, recriar=recriar)


def link_danfe(pedido):
    """URL pro DANFE em PDF (válida por tempo limitado no Tiny)."""
    if not pedido.tiny_nota_fiscal_id:
        return None
    return tiny.obter_link_nota_fiscal(pedido.tiny_nota_fiscal_id)


def baixar_danfe_pdf(nota_id):
    """Baixa o DANFE (PDF) da NF no Tiny e devolve os bytes, ou None.

    O link do Tiny é temporário — pra ANEXAR o PDF num e-mail a gente
    baixa na hora (link solto expiraria na caixa de entrada do cliente)."""
    pdf, _ = baixar_danfe_pdf_com_motivo(nota_id)
    return pdf


# User-Agent de navegador: o link do Tiny hoje aponta pro visualizador do
# Olist (erp.olist.com/doc.view), que pode servir HTML pra bot e PDF pra
# navegador — mandamos UA de browser pra pegar o PDF direto quando dá.
_UA_NAVEGADOR = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                 'AppleWebKit/537.36 (KHTML, like Gecko) '
                 'Chrome/124.0 Safari/537.36')


def _candidatos_pdf_na_pagina(html, base_url):
    """URLs de PDF citadas numa página HTML (o visualizador do Olist embute
    o DANFE via <embed>/<iframe>/link). Resolve relativas e tira duplicatas,
    mantendo a ordem de aparição."""
    import re
    from urllib.parse import urljoin
    achados = re.findall(r"""(?:href|src|data)\s*=\s*["']([^"']+)["']""",
                         html or '', re.I)
    vistos, out = set(), []
    for u in achados:
        if 'pdf' not in u.lower():
            continue
        full = urljoin(base_url, u)
        if full not in vistos:
            vistos.add(full)
            out.append(full)
    return out


def _html_para_pdf(html, base_url):
    """Converte o HTML do DANFE (visualizador do Olist) em PDF com o
    weasyprint. Import LAZY + try/except: se a lib/infra faltar, devolve
    None e o caller degrada pro aviso — nunca derruba o app."""
    try:
        from weasyprint import HTML
    except Exception as exc:  # noqa: BLE001 — ImportError ou erro de lib nativa
        logger.warning('danfe: weasyprint indisponivel (%s)', exc)
        return None
    try:
        return HTML(string=html, base_url=base_url).write_pdf()
    except Exception as exc:  # noqa: BLE001
        logger.warning('danfe: falha ao converter HTML->PDF: %s', exc)
        return None


def baixar_danfe_pdf_com_motivo(nota_id):
    """Como `baixar_danfe_pdf`, mas devolve `(bytes, motivo)` — o motivo
    carrega a causa REAL (erro do Tiny, link vazio, ou download não-PDF)
    pra tela mostrar em vez do genérico 'precisa estar autorizada'.

    O link do Tiny hoje é o visualizador do Olist (HTML). Se o download não
    vier em PDF, segue a página atrás do PDF embutido (embed/iframe)."""
    import requests
    url, motivo = tiny.obter_link_nota_fiscal_com_motivo(nota_id)
    if not url:
        return None, motivo
    try:
        r = requests.get(url, timeout=20,
                         headers={'User-Agent': _UA_NAVEGADOR})
    except requests.RequestException:
        logger.warning('danfe download falhou (rede) nota=%s', nota_id)
        return None, 'falha de rede ao baixar o PDF do Tiny'
    ctype = (r.headers.get('Content-Type') or '').lower()
    if r.status_code == 200 and 'pdf' in ctype:
        return r.content, None
    # Veio HTML: hoje o link do Tiny é o visualizador do Olist, que
    # RENDERIZA o DANFE em HTML+CSS (não há PDF nativo — confirmado). Então
    # convertemos o HTML em PDF do nosso lado com o weasyprint (base_url =
    # a URL do doc.view, pra a CSS/imagens externas do Olist resolverem).
    if r.status_code == 200 and 'html' in ctype:
        pdf = _html_para_pdf(r.text, r.url)
        if pdf:
            logger.info('danfe: HTML do Olist convertido em PDF nota=%s '
                        '(%d bytes)', nota_id, len(pdf))
            return pdf, None
        return None, ('o link do Tiny abriu o visualizador do Olist (HTML) '
                      'e a conversão pra PDF falhou — use "Ver DANFE" e '
                      'baixe pelo navegador enquanto eu verifico')
    logger.warning('danfe download invalido nota=%s (HTTP %s, %s)',
                   nota_id, r.status_code, ctype)
    return None, (f'o link do Tiny não devolveu um PDF (HTTP '
                  f'{r.status_code}, tipo {ctype or "?"})')


def importar_planilha(conteudo, filename, user_id=None, canal='site'):
    """Importa o export de produtos do Tiny e mapeia os SKUs por nome nos
    itens do canal. Devolve contadores ou {erro}."""
    pares = _parse_planilha(conteudo, filename)
    if not pares:
        return {'erro': 'Não consegui ler a planilha. Use o export de '
                'produtos do Tiny (.xls ou .csv) com as colunas "Descrição" '
                'e "Código (SKU)".'}
    return _aplicar_pares(pares, user_id=user_id, canal=canal)
