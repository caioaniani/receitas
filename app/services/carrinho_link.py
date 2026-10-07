"""Carrinho montado pelo bot de atendimento e entregue como link curto.

Decisão do dono (07/10/2026): no WhatsApp o bot atende quem quer comprar,
monta o carrinho — inclusive minis (menu configurável), "fatiado" dos
sourdoughs e a cartinha de presente — e manda o link do site; o cliente
confere, preenche entrega e dados e PAGA no site. O bot nunca cria pedido.

Por que um registro (`CarrinhoLink`) e não tudo na URL:
- a composição dos minis e a cartinha não cabem num `?add=` legível;
- os `pi_id` do menu mudam a cada edição do cadastro
  (`produtos.salvar_composicao` apaga e recria os `ProdutoItem`); aqui a
  composição é guardada pelo ALVO do mini (`col` + `alvo_id`) e convertida
  para os `pi_id` correntes no resgate.

O servidor é a autoridade nas DUAS pontas: `criar` recusa o que não pode
ser vendido (com mensagem que o bot repassa), e `resgatar` revalida contra o
catálogo do momento — item que saiu de linha, esgotou ou menu que mudou não
entra no carrinho e vira aviso visível (nunca troca em silêncio). Preço
NUNCA é guardado: carrinho e checkout recalculam.
"""
import logging
import secrets
from datetime import timedelta
from decimal import Decimal

from flask import current_app

from app.extensions import db
from app.utils import agora

logger = logging.getLogger(__name__)

VALIDADE_DIAS = 7
MAX_LINHAS = 20
MAX_QTD = 99


class LinkInvalido(ValueError):
    """Pedido do bot que não pode virar carrinho. A mensagem é para o bot
    repassar ao cliente (ou corrigir a montagem e tentar de novo)."""


def _base_url():
    return (current_app.config.get('LOJA_BASE_URL')
            or 'https://opao.online').rstrip('/')


def _int(valor, nome):
    try:
        n = int(valor)
    except (TypeError, ValueError):
        raise LinkInvalido(f'{nome} inválido: {valor!r}.')
    return n


def _composicao_do_pedido(produto, item, linha):
    """Composição pedida pelo bot → {pi_id: qtd} validada contra o cadastro.
    Levanta LinkInvalido com a regra que faltou (o bot corrige e refaz)."""
    from app.services import loja_menu
    bruto = linha.get('composicao')
    nome = item['nome']
    if not bruto:
        raise LinkInvalido(
            f'{nome} é um menu montável: informe a composição (pi_id e '
            f'quantidade de cada mini) somando {item["menu"]["total"]} '
            f'unidades. Para a seleção padrão, use a composição padrão que '
            f'o consultar_produtos mostrou.')
    if not isinstance(bruto, list):
        raise LinkInvalido(f'Composição do {nome} em formato inválido.')
    pares = []
    for c in bruto:
        if not isinstance(c, dict):
            raise LinkInvalido(f'Composição do {nome} em formato inválido.')
        pares.append([_int(c.get('pi_id'), 'pi_id'),
                      _int(c.get('quantidade'), 'quantidade')])
    validos = {s['pi_id'] for s in loja_menu.slots(produto)}
    alheios = [p for p, _q in pares if p not in validos]
    if alheios:
        raise LinkInvalido(
            f'Os itens {alheios} não fazem parte do {nome}. Consulte o '
            f'produto de novo e use os pi_id que ele mostrar.')
    # Nada de "conserto" silencioso pelo normalizar (que corta no teto e
    # sobrescreve repetidos): o que o bot mostrou ao cliente tem de ser
    # exatamente o que fica gravado.
    ids = [p for p, _q in pares]
    if len(ids) != len(set(ids)):
        raise LinkInvalido(f'Composição do {nome} repete um mini — some as '
                           f'quantidades numa linha só.')
    _total, teto = loja_menu.regras(produto)
    if any(q > teto for _p, q in pares):
        raise LinkInvalido(f'O {nome} aceita no máximo {teto} unidades de '
                           f'cada mini.')
    if any(q <= 0 for _p, q in pares):
        raise LinkInvalido('Quantidade de mini deve ser maior que zero.')
    comp = loja_menu.normalizar(produto, pares)
    erro = loja_menu.validar(produto, comp)
    if erro:
        raise LinkInvalido(erro)
    if loja_menu.preco(produto, comp) is None:
        raise LinkInvalido(f'{nome} está sem preço para algum mini escolhido '
                           f'— não dá para vender agora.')
    return comp


def criar(itens, *, cartinha=None, conversa_id=None):
    """Valida e grava o carrinho do bot. Devolve
    {'url', 'token', 'itens': [resumo], 'total_itens', 'cartinha'}.

    `itens` = [{'kind': 'receita'|'produto', 'id', 'quantidade',
               'fatiado'?: bool, 'composicao'?: [{'pi_id', 'quantidade'}]}].
    Levanta LinkInvalido (mensagem para o bot). Commita."""
    from app.models import CarrinhoLink, Produto
    from app.services import loja_catalogo, loja_checkout, loja_menu

    if not isinstance(itens, list) or not itens:
        raise LinkInvalido('Informe pelo menos um item.')
    if len(itens) > MAX_LINHAS:
        raise LinkInvalido(f'No máximo {MAX_LINHAS} itens por link.')

    gravar, resumo = [], []
    total = Decimal('0')
    for linha in itens:
        if not isinstance(linha, dict):
            raise LinkInvalido('Item em formato inválido.')
        kind = str(linha.get('kind') or '').strip().lower()
        if kind not in ('receita', 'produto'):
            raise LinkInvalido(f'kind inválido: {kind!r}.')
        iid = _int(linha.get('id'), 'id')
        qtd = _int(linha.get('quantidade') or 1, 'quantidade')
        if not 1 <= qtd <= MAX_QTD:
            raise LinkInvalido(f'Quantidade deve ficar entre 1 e {MAX_QTD}.')
        item = loja_catalogo.por_id_publicado(kind, iid)
        if not item:
            raise LinkInvalido(
                f'O item {kind} {iid} não está à venda no site agora.')
        if not loja_catalogo.tem_estoque_site(kind, iid):
            raise LinkInvalido(f'{item["nome"]} está esgotado no site.')
        fatiado = bool(linha.get('fatiado'))
        if fatiado and not item.get('fatiavel'):
            raise LinkInvalido(f'{item["nome"]} não tem a opção fatiado.')
        reg = {'kind': kind, 'id': iid, 'qtd': qtd, 'fatiado': fatiado}
        linha_resumo = {'nome': item['nome'], 'quantidade': qtd}
        if fatiado:
            linha_resumo['fatiado'] = True
        if item.get('menu'):
            produto = db.session.get(Produto, iid)
            comp = _composicao_do_pedido(produto, item, linha)
            por_pi = {s['pi_id']: s for s in loja_menu.slots(produto)}
            reg['comp'] = [[por_pi[p]['col'], por_pi[p]['alvo_id'], q]
                           for p, q in sorted(comp.items())]
            unit = loja_menu.preco(produto, comp)
            linha_resumo['composicao'] = [
                f'{r["qtd"]}x {r["nome"]}'
                for r in loja_menu.resumo(produto, comp)]
        else:
            if linha.get('composicao'):
                raise LinkInvalido(
                    f'{item["nome"]} não é montável — não informe composição.')
            unit = Decimal(str(item.get('preco') or 0))
        linha_resumo['preco_unitario'] = float(unit)
        total += unit * qtd
        gravar.append(reg)
        resumo.append(linha_resumo)

    texto_cartinha = (cartinha or '').strip() or None
    if texto_cartinha and len(texto_cartinha) > loja_checkout.CARTINHA_MAX_CHARS:
        raise LinkInvalido(
            f'A cartinha tem {len(texto_cartinha)} caracteres; o máximo é '
            f'{loja_checkout.CARTINHA_MAX_CHARS}. Encurte com o cliente.')

    token = secrets.token_urlsafe(9)
    reg = CarrinhoLink(token=token, itens=gravar, cartinha=texto_cartinha,
                       conversa_id=str(conversa_id)[:40] if conversa_id else None,
                       expira_em=agora() + timedelta(days=VALIDADE_DIAS))
    db.session.add(reg)
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise
    return {
        'url': f'{_base_url()}/loja/carrinho?link={token}',
        'token': token,
        'itens': resumo,
        'total_itens': float(total),
        'cartinha': texto_cartinha,
        'validade_dias': VALIDADE_DIAS,
    }


def resgatar(token):
    """Converte o link no formato do carrinho da sessão. Devolve
    {'itens': [{kind,id,qtd,fatiado,comp}], 'esgotados': [nomes],
     'avisos': [texto], 'cartinha': str|None} ou None (link inexistente ou
    vencido). Revalida TUDO contra o catálogo de agora."""
    from app.models import CarrinhoLink, Produto
    from app.services import loja_catalogo, loja_menu

    token = (token or '').strip()
    if not token or len(token) > 24:
        return None
    reg = CarrinhoLink.query.filter_by(token=token).first()
    if not reg or reg.expira_em < agora():
        return None
    itens, esgotados, avisos = [], [], []
    for linha in reg.itens or []:
        try:
            kind, iid = linha['kind'], int(linha['id'])
            qtd = max(1, min(int(linha.get('qtd') or 1), MAX_QTD))
        except (KeyError, TypeError, ValueError):
            continue
        item = loja_catalogo.por_id_publicado(kind, iid)
        if not item:
            avisos.append('Um item do link saiu do site e não entrou no '
                          'carrinho.')
            continue
        if not loja_catalogo.tem_estoque_site(kind, iid):
            esgotados.append(item['nome'])
            continue
        fatiado = bool(linha.get('fatiado')) and bool(item.get('fatiavel'))
        novo = {'kind': kind, 'id': iid, 'qtd': qtd, 'fatiado': fatiado}
        if item.get('menu'):
            produto = db.session.get(Produto, iid)
            por_alvo = {(s['col'], s['alvo_id']): s['pi_id']
                        for s in loja_menu.slots(produto)}
            pares = [[por_alvo[(c[0], c[1])], c[2]]
                     for c in (linha.get('comp') or [])
                     if (c[0], c[1]) in por_alvo]
            comp = loja_menu.normalizar(produto, pares) if pares else {}
            if loja_menu.validar(produto, comp) or \
                    loja_menu.preco(produto, comp) is None:
                avisos.append(f'O {item["nome"]} mudou depois que o link foi '
                              f'enviado e não entrou no carrinho — monte de '
                              f'novo na página do produto.')
                continue
            novo['comp'] = loja_menu.compactar(comp)
        itens.append(novo)
    reg.aberto_em = reg.aberto_em or agora()
    reg.aberturas = (reg.aberturas or 0) + 1
    try:
        db.session.commit()
    except Exception:  # noqa: BLE001 — contador é informativo
        db.session.rollback()
        logger.warning('carrinho_link: não gravou a abertura de %s', token)
    return {'itens': itens, 'esgotados': esgotados, 'avisos': avisos,
            'cartinha': reg.cartinha}
