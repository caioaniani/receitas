"""Composição dos kits do owner, sempre pelo catálogo público atual."""
import json
from decimal import Decimal

from app.models import KitCafe, Produto
from app.services import loja_catalogo, loja_checkout, loja_menu
from app.services.compra_kits import DIAS_AGENDA_KITS

MAX_QUANTIDADE = 999


def _composicao(item):
    if not item.comp_json:
        return None
    try:
        comp = json.loads(item.comp_json)
        if not isinstance(comp, dict) or not comp:
            raise ValueError
        normal = {int(k): int(v) for k, v in comp.items()}
        if any(k <= 0 or v <= 0 or str(v) != str(comp[str(k)])
               for k, v in normal.items()):
            raise ValueError
        return normal
    except (ValueError, TypeError, KeyError) as exc:
        raise ValueError('A composição de um menu precisa ser revisada pelo dono.') from exc


def itens_do_kit(kit):
    """Carrinho do kit; composição de menu fixada quando o owner o montou."""
    itens = []
    for item in kit.itens:
        raw = {'kind': item.kind,
               'id': item.receita_id if item.kind == 'receita' else item.produto_id,
               'qtd': item.quantidade}
        comp = _composicao(item)
        if comp is not None:
            raw['comp'] = comp
        itens.append(raw)
    return itens


def _validar_composicoes(itens_raw):
    """Não substitui silenciosamente menus antigos pela pré-seleção atual."""
    for raw in itens_raw:
        if raw['kind'] != 'produto':
            continue
        produto = Produto.query.get(raw['id'])
        comp = raw.get('comp')
        if loja_menu.eh_menu(produto):
            if not comp or loja_menu.normalizar(produto, comp) != comp:
                return ['A composição de um menu mudou. O dono precisa revisar o kit.']
            erro = loja_menu.validar(produto, comp)
            if erro:
                return [erro]
        elif comp:
            return ['Um menu do kit deixou de ser configurável. O dono precisa revisá-lo.']
    return []


def montar(kit):
    """Mesmo preço, publicação e disponibilidade usados pelo checkout normal."""
    try:
        raw = itens_do_kit(kit)
    except ValueError as exc:
        return [], [str(exc)]
    if not raw:
        return [], ['Escolha pelo menos um item para o kit.']
    if any(r['kind'] not in ('receita', 'produto') or not r['id']
           or not isinstance(r['qtd'], int) or r['qtd'] < 1
           or r['qtd'] > MAX_QUANTIDADE for r in raw):
        return [], ['Um item do kit tem quantidade ou cadastro inválido.']
    erros = _validar_composicoes(raw)
    if erros:
        return [], erros
    return loja_checkout.montar_itens(raw, dias_disponibilidade=DIAS_AGENDA_KITS)


def publicados():
    """Kits ativos que ainda podem ser comprados integralmente."""
    kits = KitCafe.query.filter_by(ativo=True).order_by(KitCafe.id).all()
    return [kit for kit in kits if not montar(kit)[1]]


def preco_kit(kit):
    itens, erros = montar(kit)
    if erros:
        raise ValueError(' '.join(erros))
    return sum((item['subtotal'] for item in itens), Decimal('0.00'))


def catalogo_para_editor():
    """Inclui os menus vendáveis e seu preço da composição padrão, sem custos."""
    catalogo = []
    for publicado in loja_catalogo.produtos_publicados():
        kind, item_id = publicado['kind'], publicado['id']
        cat = loja_catalogo.por_id_publicado(kind, item_id)
        if not cat:
            continue
        raw = {'kind': kind, 'id': item_id, 'qtd': 1}
        itens, erros = loja_checkout.montar_itens([raw], dias_disponibilidade=DIAS_AGENDA_KITS)
        if erros or not itens:
            continue
        cat['preco_kit'] = itens[0]['preco']
        catalogo.append(cat)
    return sorted(catalogo, key=lambda item: (item['nome'].casefold(), item['kind']))


def preparar_itens(selecao, *, kit=None, atualizar_menus=()):
    """Valida a seleção do owner, sem aceitar preços ou composições do POST.

    Seleção: [{kind, id, qtd}]. Menu novo usa a pré-seleção válida do
    catálogo; o já escolhido preserva sua composição, salvo pedido explícito
    para atualizar. Devolve (itens normalizados, erros), sem gravar nada.
    """
    atuais = {(item.kind, item.receita_id if item.kind == 'receita'
               else item.produto_id): item for item in (kit.itens if kit else [])}
    vistos = set()
    raw = []
    for selecionado in selecao:
        kind = selecionado.get('kind')
        item_id, qtd = selecionado.get('id'), selecionado.get('qtd')
        if (kind not in ('receita', 'produto') or type(item_id) is not int
                or item_id <= 0 or type(qtd) is not int
                or qtd < 1 or qtd > MAX_QUANTIDADE):
            return [], ['Informe quantidades inteiras entre 1 e 999.']
        chave = (kind, item_id)
        if chave in vistos:
            return [], ['Um item foi selecionado mais de uma vez.']
        vistos.add(chave)
        cat = loja_catalogo.por_id_publicado(kind, item_id)
        if not cat:
            return [], ['Um item selecionado não está mais à venda no site. Revise o kit.']
        item = {'kind': kind, 'id': item_id, 'qtd': qtd}
        anterior = atuais.get(chave)
        if anterior and chave not in atualizar_menus:
            try:
                comp = _composicao(anterior)
            except ValueError as exc:
                return [], [str(exc)]
            if comp:
                item['comp'] = comp
        if cat.get('menu') and 'comp' not in item:
            if anterior and chave not in atualizar_menus:
                return [], ['Um item virou menu configurável. Adote a composição padrão '
                            'atual explicitamente ou remova-o do kit.']
            item['comp'] = loja_menu.composicao_padrao(Produto.query.get(item_id))
        raw.append(item)
    if not raw:
        return [], ['Escolha pelo menos um item para o kit.']
    erros = _validar_composicoes(raw)
    if erros:
        return [], erros
    return loja_checkout.montar_itens(raw, dias_disponibilidade=DIAS_AGENDA_KITS)
