"""Composição dos kits do owner, sempre pelo catálogo público atual."""
import json
import re
from decimal import Decimal

from app.models import KitCafe, Produto
from app.services import loja_catalogo, loja_checkout, loja_menu
from app.services.compra_kits import DIAS_AGENDA_KITS

MAX_QUANTIDADE = 999
GRUPOS_OPCOES = {'croissant': 'Croissant', 'sourdough': 'Sourdough'}
MAX_COMBINACOES = 64


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


def itens_fixos_do_kit(kit):
    """Carrinho fixo do owner, sem antecipar a escolha de suco do cliente."""
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


def preparar_sucos(ids, itens):
    """Valida opções publicadas sem gravar nem aceitar preço do navegador."""
    if (not isinstance(ids, (list, tuple))
            or any(type(item_id) is not int or item_id <= 0 for item_id in ids)):
        return [], ['Selecione opções de suco válidas.']
    if not ids:
        return [], []
    if not 2 <= len(ids) <= 10:
        return [], ['Selecione entre 2 e 10 opções de suco, ou deixe todas desmarcadas.']
    if len(set(ids)) != len(ids):
        return [], ['Uma opção de suco foi selecionada mais de uma vez.']
    fixos = {item.get('produto_id') or item.get('id') for item in itens
             if item.get('kind') == 'produto'}
    if fixos.intersection(ids):
        return [], ['Um suco não pode ser opção de escolha e item fixo do mesmo kit.']
    raw_fixos = [
        {'kind': item['kind'],
         'id': item.get('id') or item.get('receita_id') or item.get('produto_id'),
         'qtd': item['qtd'], 'comp': item.get('comp'), 'fatiado': item.get('fatiado')}
        for item in itens
    ]
    opcoes = []
    for item_id in ids:
        cat = loja_catalogo.por_id_venda('produto', item_id)
        if not cat:
            return [], ['Uma opção de suco não está mais à venda no site. Revise o kit.']
        if cat.get('menu'):
            return [], ['As opções de suco devem ser produtos sem menu configurável.']
        normalizados, erros = loja_checkout.montar_itens(
            [*raw_fixos, {'kind': 'produto', 'id': item_id, 'qtd': 1}],
            dias_disponibilidade=DIAS_AGENDA_KITS)
        if erros or len(normalizados) != len(raw_fixos) + 1:
            return [], [f'A opção de suco "{cat["nome"]}" está indisponível. Revise o kit.']
        item = normalizados[-1]
        opcoes.append({'id': item_id, 'nome': item['nome'], 'preco': item['preco']})
    return sorted(opcoes, key=lambda item: (item['preco'], item['id'])), []


def opcoes_suco(kit):
    """Opções atuais do kit, todas válidas; não remove silenciosamente uma opção."""
    opcoes, erros = preparar_sucos([suco.produto_id for suco in kit.sucos], itens_fixos_do_kit(kit))
    if erros:
        raise ValueError(' '.join(erros))
    return opcoes


def preparar_opcoes(selecao, itens, suco_ids=()):
    """Valida grupos fixos de uma unidade, sem receber preços do navegador.

    Seleção: {'croissant': ['receita:4', 'produto:107'], ...}. Grupos
    vazios não são oferecidos. Chaves kind:id distinguem os dois catálogos.
    """
    if not isinstance(selecao, dict) or set(selecao) - GRUPOS_OPCOES.keys():
        return [], ['Escolha apenas os grupos Croissant e Sourdough deste kit.']
    if (not isinstance(suco_ids, (list, tuple))
            or any(type(item_id) is not int or item_id <= 0 for item_id in suco_ids)
            or len(set(suco_ids)) != len(suco_ids)):
        return [], ['Selecione opções de suco válidas.']
    ocupados = {(item['kind'], item.get('id') or item.get('receita_id') or item.get('produto_id'))
                for item in itens}
    if ocupados.intersection(('produto', item_id) for item_id in suco_ids):
        return [], ['Um item não pode se repetir entre itens fixos, sucos e grupos de escolha.']
    ocupados.update(('produto', item_id) for item_id in suco_ids)
    grupos = []
    combinacoes = max(1, len(suco_ids))
    for chave, nome in GRUPOS_OPCOES.items():
        selecionados = selecao.get(chave, [])
        if not isinstance(selecionados, (list, tuple)):
            return [], [f'Selecione opções válidas de {nome}.']
        if not selecionados:
            continue
        if not 2 <= len(selecionados) <= 10:
            return [], [f'Selecione entre 2 e 10 opções de {nome}, ou deixe o grupo vazio.']
        combinacoes *= len(selecionados)
        if combinacoes > MAX_COMBINACOES:
            return [], ['Os sucos e as opções do kit devem formar no máximo 64 combinações.']
        grupo = {'chave': chave, 'nome': nome, 'opcoes': []}
        for selecionado in selecionados:
            match = (re.fullmatch(r'(receita|produto):([1-9][0-9]{0,9})', selecionado)
                     if isinstance(selecionado, str) else None)
            if not match:
                return [], [f'Selecione opções válidas de {nome}.']
            kind, item_id = match.group(1), int(match.group(2))
            if (kind, item_id) in ocupados:
                return [], ['Um item não pode se repetir entre itens fixos, sucos e grupos de escolha.']
            ocupados.add((kind, item_id))
            grupo['opcoes'].append({'chave': selecionado, 'kind': kind, 'id': item_id})
        grupos.append(grupo)
    raw_fixos = [
        {'kind': item['kind'],
         'id': item.get('id') or item.get('receita_id') or item.get('produto_id'),
         'qtd': item['qtd'], 'comp': item.get('comp'), 'fatiado': item.get('fatiado')}
        for item in itens
    ]
    for grupo in grupos:
        for opcao in grupo['opcoes']:
            kind, item_id = opcao['kind'], opcao['id']
            cat = loja_catalogo.por_id_venda(kind, item_id)
            if not cat:
                return [], [f'Uma opção de {grupo["nome"]} não está mais à venda no site. Revise o kit.']
            if cat.get('menu'):
                return [], ['As opções de croissant e sourdough não podem ser menus configuráveis.']
            normalizados, erros = loja_checkout.montar_itens(
                [*raw_fixos, {'kind': kind, 'id': item_id, 'qtd': 1}],
                dias_disponibilidade=DIAS_AGENDA_KITS)
            if erros or len(normalizados) != len(raw_fixos) + 1:
                return [], [f'A opção "{cat["nome"]}" está indisponível. Revise o kit.']
            item = normalizados[-1]
            opcao.update(nome=item['nome'], preco=item['preco'])
        grupo['opcoes'].sort(key=lambda opcao: (opcao['preco'], opcao['id'], opcao['kind']))
    return grupos, []


def opcoes_grupos(kit):
    """Grupos atuais completos; opção inválida exige revisão do cadastro."""
    selecao = {}
    for opcao in kit.opcoes:
        item_id = opcao.receita_id if opcao.kind == 'receita' else opcao.produto_id
        selecao.setdefault(opcao.grupo, []).append(f'{opcao.kind}:{item_id}')
    grupos, erros = preparar_opcoes(selecao, itens_fixos_do_kit(kit),
                                    [suco.produto_id for suco in kit.sucos])
    if erros:
        raise ValueError(' '.join(erros))
    return grupos


def itens_do_kit(kit, suco_id=None, escolhas=None):
    """Itens fixos e uma opção por grupo; escolhas=None serve só ao preview."""
    itens = itens_fixos_do_kit(kit)
    opcoes = opcoes_suco(kit)
    if not opcoes:
        if suco_id is not None:
            raise ValueError('Este kit não oferece escolha de suco.')
    else:
        if suco_id is None:
            suco_id = opcoes[0]['id']
        elif type(suco_id) is not int or suco_id not in {item['id'] for item in opcoes}:
            raise ValueError('Escolha um dos sucos disponíveis neste kit.')
        itens.append({'kind': 'produto', 'id': suco_id, 'qtd': 1})
    grupos = opcoes_grupos(kit)
    if escolhas is not None and (not isinstance(escolhas, dict)
            or set(escolhas) != {grupo['chave'] for grupo in grupos}):
        raise ValueError('Escolha uma opção de cada grupo disponível neste kit.')
    for grupo in grupos:
        escolha = grupo['opcoes'][0]['chave'] if escolhas is None else escolhas[grupo['chave']]
        opcao = next((opcao for opcao in grupo['opcoes'] if opcao['chave'] == escolha), None)
        if opcao is None:
            raise ValueError(f'Escolha um dos itens de {grupo["nome"]} disponíveis neste kit.')
        itens.append({'kind': opcao['kind'], 'id': opcao['id'], 'qtd': 1})
    return itens


def _validar_composicoes(itens_raw):
    """Não substitui silenciosamente menus antigos pela pré-seleção atual."""
    from app.services import loja_leitura
    leitura = loja_leitura.atual()
    for raw in itens_raw:
        if raw['kind'] != 'produto':
            continue
        comp = raw.get('comp')
        if leitura is not None:
            cat = leitura['catalogo'].get(('produto', raw['id']))
            if not cat or not cat.get('menu'):
                if comp:
                    return ['Um menu do kit deixou de ser configurável. O dono precisa revisá-lo.']
                continue
        produto = Produto.query.get(raw['id'])
        if loja_menu.eh_menu(produto):
            if not comp or loja_menu.normalizar(produto, comp) != comp:
                return ['A composição de um menu mudou. O dono precisa revisar o kit.']
            erro = loja_menu.validar(produto, comp)
            if erro:
                return [erro]
        elif comp:
            return ['Um menu do kit deixou de ser configurável. O dono precisa revisá-lo.']
    return []


def montar(kit, suco_id=None, escolhas=None):
    """Mesmo preço, publicação e disponibilidade usados pelo checkout normal."""
    try:
        raw = itens_do_kit(kit, suco_id, escolhas)
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
        cat = loja_catalogo.por_id_venda(kind, item_id)
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
        cat = loja_catalogo.por_id_venda(kind, item_id)
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
