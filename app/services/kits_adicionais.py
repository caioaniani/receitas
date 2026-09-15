"""Adicionais escolhidos pelo cliente, com preço e publicação do catálogo."""
import json
import re

from app.models import Produto
from app.services import kits_cafe, loja_catalogo, loja_checkout, loja_menu
from app.services.compra_kits import DIAS_AGENDA_KITS

MAX_ITENS = 50


def ler(form, *, conferir_catalogo=True):
    """Valida antes do checkout; nunca corrige ou descarta uma escolha em silêncio."""
    valores = (form.getlist('adicionais_json') if hasattr(form, 'getlist') else
               [form['adicionais_json']] if 'adicionais_json' in form else [])
    if not valores:
        return [], []
    if len(valores) != 1 or not isinstance(valores[0], str) or len(valores[0]) > 64000:
        return [], ['Revise os produtos adicionais do kit.']
    try:
        selecao = json.loads(valores[0])
    except (ValueError, TypeError):
        return [], ['Revise os produtos adicionais do kit.']
    if not isinstance(selecao, list) or len(selecao) > MAX_ITENS:
        return [], [f'Escolha até {MAX_ITENS} produtos adicionais por kit.']
    vistos, raw = set(), []
    for item in selecao:
        if (not isinstance(item, dict) or item.get('kind') not in ('receita', 'produto')
                or type(item.get('id')) is not int or not 1 <= item['id'] <= 9999999999
                or type(item.get('qtd')) is not int
                or not 1 <= item['qtd'] <= kits_cafe.MAX_QUANTIDADE):
            return [], ['Informe quantidades inteiras entre 1 e 999 para os adicionais.']
        chave = (item['kind'], item['id'])
        if chave in vistos:
            return [], ['Um adicional está repetido. Ajuste sua quantidade na mesma linha.']
        vistos.add(chave)
        normal = {k: item[k] for k in ('kind', 'id', 'qtd')}
        comp = item.get('comp')
        if comp is not None:
            if (not isinstance(comp, dict) or not comp or len(comp) > 100
                    or any(not isinstance(k, str) or not re.fullmatch(r'[1-9][0-9]{0,9}', k)
                           or type(v) is not int or v < 1 for k, v in comp.items())):
                return [], ['Confira a composição do menu adicional e adicione-o novamente.']
            normal['comp'] = {int(k): v for k, v in comp.items()}
        # Recuperação do formulário conserva inclusive o item que saiu de venda,
        # para o cliente removê-lo sem perder todos os demais. Nunca usada na compra.
        if not conferir_catalogo:
            raw.append(normal)
            continue
        cat = loja_catalogo.por_id_venda(*chave)
        if not cat:
            return [], ['Um adicional não está mais à venda. Revise os produtos escolhidos.']
        if loja_catalogo.eh_cesta(cat):
            return [], ['Cestas não podem ser acrescentadas como adicionais do kit. Remova a cesta para continuar.']
        if cat.get('menu'):
            if not comp:
                return [], ['Confira a composição do menu adicional e adicione-o novamente.']
            produto = Produto.query.get(item['id'])
            if (loja_menu.normalizar(produto, comp) != normal['comp']
                    or loja_menu.validar(produto, normal['comp'])):
                return [], ['A composição de um adicional mudou. Remova-o para continuar '
                            'ou reabra o kit para escolher a composição atual.']
        elif comp is not None:
            return [], ['Este adicional não oferece composição de menu. Escolha-o novamente.']
        raw.append(normal)
    return raw, []


def catalogo(*, base=None, selecionados=()):
    """Produtos públicos vendáveis no mês; menus mostram a composição cobrada."""
    ofertas = []
    escolhas = {(item['kind'], item['id']): item for item in selecionados}
    from app.services import loja_leitura
    leitura = loja_leitura.atual()
    catalogo_atual = (leitura['catalogo'].values() if leitura is not None else
                      loja_catalogo.produtos_publicados())
    for cat in catalogo_atual:
        if loja_catalogo.eh_cesta(cat):
            continue
        escolhido = escolhas.get((cat['kind'], cat['id']))
        raw = {**(escolhido or {}), 'kind': cat['kind'], 'id': cat['id'], 'qtd': 1}
        if escolhido:
            _, erros = ler({'adicionais_json': json.dumps([escolhido])})
            if erros:
                continue
        itens, erros = loja_checkout.montar_itens(
            [raw], dias_disponibilidade=DIAS_AGENDA_KITS, base=base)
        if erros or not itens:
            continue
        item = itens[0]
        oferta = {'kind': item['kind'], 'id': item['id'], 'nome': item['nome'],
                  'chave': f'{item["kind"]}:{item["id"]}',
                  'precoCentavos': int(item['preco'] * 100),
                  # Capa canônica já carregada pela vitrine: Dropbox, URL
                  # legada ou vazio. Não consulta galeria nem blob por item.
                  'imagem': cat.get('imagem') or '',
                  'comp': item.get('comp'), 'descricao': ''}
        if item.get('comp'):
            oferta['descricao'] = loja_menu.resumo(
                Produto.query.get(item['id']), item['comp'])
        ofertas.append(oferta)
    return sorted(ofertas, key=lambda item: (item['nome'].casefold(), item['chave']))
