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


_FUZZY_CUTOFF = 86  # score mínimo (rapidfuzz WRatio) pra sugerir um SKU


def _aplicar_pares(pares, user_id=None):
    """Aplica uma lista de (nome_tiny, sku) ao mapeamento dos itens
    publicados. Match EXATO (nome normalizado igual) → confirma automático;
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

    mp = mapa_por_item()
    exatos = sugeridos = sem_match = 0
    for it in loja_catalogo.produtos_publicados():
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
            m = TinyProdutoMap(kind=it['kind'], item_id=it['id'])
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


def sincronizar_sugestoes(user_id=None):
    """Busca o catálogo do Tiny (API) e mapeia por nome. Match exato confirma
    automático; parecido vira sugestão. Devolve contadores ou {erro}."""
    produtos_tiny = tiny.listar_produtos()
    if not produtos_tiny:
        return {'erro': 'Tiny não respondeu (token ausente ou rede). '
                'Dica: você pode importar a planilha de produtos do Tiny.'}
    pares = [(p['nome'], p['sku']) for p in produtos_tiny if p.get('sku')]
    res = _aplicar_pares(pares, user_id=user_id)
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


def _pedido_payload(pedido, itens):
    """Monta o pedido pro Tiny com a natureza de operacao. Sem ela o Tiny
    gera a NF com natOp vazia e a SEFAZ rejeita.

    A SERIE, o CFOP e o indicador de consumidor final NAO vem daqui: sao
    configurados no Tiny (Naturezas de operacao + Numeracao de NF). A v2 do
    `pedido.incluir.php` NAO tem campo de serie — mandar um era ignorado
    (a nota saia em numeracao/serie errada, bug visto em prod: 000009)."""
    from flask import current_app
    natureza = current_app.config.get('NF_NATUREZA_OPERACAO',
                                      'Venda de mercadorias')
    return {
        'data_pedido': (pedido.criado_em or agora()).strftime('%d/%m/%Y'),
        'numero_ordem_compra': pedido.codigo,
        'cliente': _payload_cliente(pedido),
        'itens': itens,
        'valor_frete': float(pedido.frete_valor or 0),
        'natureza_operacao': natureza,
    }


def emitir_nf(pedido, user_id=None, recriar=False):
    """Emite NF pro pedido. Devolve {ok, msg, nota_fiscal_id?}.

    Fluxo: pedido.incluir → gerar.nota.fiscal.pedido → nota.fiscal.emitir.
    Idempotente: NF já emitida COM SUCESSO (nf_emitida_em setado) não refaz.

    `recriar=True`: descarta o vínculo anterior com o Tiny (pedido + nota
    rascunho) e refaz do zero. Necessário quando uma tentativa anterior
    gerou uma NF com dados errados que a SEFAZ rejeitou (ex: pedido criado
    no Tiny ANTES de termos o endereço estruturado) — reemitir a MESMA nota
    não corrige os dados; o rascunho precisa ser refeito do pedido novo."""
    if pedido.nf_emitida_em and pedido.tiny_nota_fiscal_id and not recriar:
        return {'ok': True, 'nota_fiscal_id': pedido.tiny_nota_fiscal_id,
                'msg': 'NF já emitida.'}
    if pedido.status != 'pago':
        return {'ok': False, 'msg': 'Pedido não está pago — não emite NF.'}
    if recriar:
        # Esquece pedido/nota antigos do Tiny (rascunho ruim) e recria com o
        # payload atual. O rascunho velho fica órfão no Tiny (apagável). NÃO
        # reaproveita o pedido velho pelo numero_ordem_compra (ele tem os
        # dados errados) — por isso o `not recriar` na busca abaixo.
        pedido.tiny_pedido_id = None
        pedido.tiny_nota_fiscal_id = None
        pedido.nf_status = None
        pedido.nf_emitida_em = None
        db.session.commit()
    # RESUMÍVEL sem duplicar: usa o tiny_pedido_id que já temos; se ele não
    # existir mais no Tiny ("Pedido não localizado", cod 32), procura pelo
    # nosso código (numero_ordem_compra) antes de criar um novo.
    tiny_pid = pedido.tiny_pedido_id
    if not tiny_pid and not recriar:
        achado = tiny.buscar_pedido_por_numero_ordem(pedido.codigo)
        if achado:
            tiny_pid = achado
            pedido.tiny_pedido_id = tiny_pid
            db.session.commit()
    if not tiny_pid:
        itens, faltando = _payload_itens(pedido)
        if faltando:
            return {'ok': False,
                    'msg': 'Itens sem SKU mapeado no Tiny: '
                           + ', '.join(faltando)}
        incl = tiny.incluir_pedido(_pedido_payload(pedido, itens))
        if not incl.get('ok'):
            return {'ok': False,
                    'msg': f'Falha ao incluir o pedido no Tiny: '
                           f'{incl.get("erro")}'}
        pedido.tiny_pedido_id = tiny_pid = incl['id']
        db.session.commit()
    gerar = tiny.gerar_nota_fiscal_pedido(tiny_pid)
    # "Pedido não localizado" (cod 32): o id velho foi apagado no Tiny.
    # Limpa e tenta UMA vez mais — busca pelo código ou cria novo pedido.
    erro_gerar = (gerar.get('erro') or '').lower()
    if not gerar.get('ok') and ('não localizado' in erro_gerar
                                or 'nao localizado' in erro_gerar
                                or 'cod 32' in erro_gerar):
        pedido.tiny_pedido_id = None
        db.session.commit()
        achado = tiny.buscar_pedido_por_numero_ordem(pedido.codigo)
        if achado:
            tiny_pid = achado
        else:
            itens, faltando = _payload_itens(pedido)
            if faltando:
                return {'ok': False,
                        'msg': 'Itens sem SKU mapeado: '
                               + ', '.join(faltando)}
            incl = tiny.incluir_pedido(_pedido_payload(pedido, itens))
            if not incl.get('ok'):
                return {'ok': False,
                        'msg': f'Falha ao incluir o pedido no Tiny: '
                               f'{incl.get("erro")}'}
            tiny_pid = incl['id']
        pedido.tiny_pedido_id = tiny_pid
        db.session.commit()
        gerar = tiny.gerar_nota_fiscal_pedido(tiny_pid)
    # "Já foi gerada nota fiscal para este pedido": uma tentativa anterior
    # gerou a NF mas não capturamos o id (ex: resposta de lock). Em vez de
    # falhar, busca o id da NF que já existe no pedido.
    erro_g = (gerar.get('erro') or '').lower()
    if not gerar.get('ok') and ('ja foi gerada' in erro_g
                                or 'já foi gerada' in erro_g
                                or 'nota fiscal para este pedido' in erro_g):
        nf_existente = tiny.id_nota_do_pedido(tiny_pid)
        if nf_existente:
            gerar = {'ok': True, 'id_nota_fiscal': nf_existente}
    if not gerar.get('ok'):
        return {'ok': False,
                'msg': f'Pedido criado no Tiny, mas falhou ao gerar a NF: '
                       f'{gerar.get("erro")}'}
    pedido.tiny_nota_fiscal_id = gerar['id_nota_fiscal']
    db.session.commit()
    emitir = tiny.emitir_nota_fiscal(gerar['id_nota_fiscal'])
    pedido.nf_status = emitir.get('status') or 'enviada'
    if emitir.get('ok'):
        pedido.nf_emitida_em = agora()
        db.session.commit()
        return {'ok': True, 'nota_fiscal_id': pedido.tiny_nota_fiscal_id,
                'msg': f'NF emitida (status: {pedido.nf_status}).'}
    db.session.commit()
    return {'ok': False,
            'msg': f'NF gerada (id {gerar["id_nota_fiscal"]}) mas a emissão '
                   f'falhou: {emitir.get("erro")}. Dá pra reenviar.'}


def link_danfe(pedido):
    """URL pro DANFE em PDF (válida por tempo limitado no Tiny)."""
    if not pedido.tiny_nota_fiscal_id:
        return None
    return tiny.obter_link_nota_fiscal(pedido.tiny_nota_fiscal_id)


def importar_planilha(conteudo, filename, user_id=None):
    """Importa o export de produtos do Tiny e mapeia os SKUs por nome.
    Devolve contadores ou {erro}."""
    pares = _parse_planilha(conteudo, filename)
    if not pares:
        return {'erro': 'Não consegui ler a planilha. Use o export de '
                'produtos do Tiny (.xls ou .csv) com as colunas "Descrição" '
                'e "Código (SKU)".'}
    return _aplicar_pares(pares, user_id=user_id)
