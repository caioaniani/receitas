"""Fotos reais dos componentes do kit, separadas de preço, estoque e escolhas."""
import re

from app.models import Produto, Receita


def chave(foto):
    item_id = foto.receita_id if foto.kind == 'receita' else foto.produto_id
    return f'{foto.kind}:{item_id}'


def candidatos(itens, sucos=(), grupos=()):
    todos = [*itens, *({**suco, 'kind': 'produto'} for suco in sucos),
             *(opcao for grupo in grupos for opcao in grupo['opcoes'])]
    return list({(item['kind'], item['id']): item for item in todos}.values())


def capas_dos_itens(itens):
    """Mesma precedência da vitrine; consulta em lote sem carregar blobs."""
    capas = {}
    for kind, modelo in (('receita', Receita), ('produto', Produto)):
        ids = {item['id'] for item in itens if item['kind'] == kind}
        if ids:
            rows = modelo.query.with_entities(
                modelo.id, modelo.imagem_dropbox_url, modelo.imagem_url
            ).filter(modelo.id.in_(ids)).all()
            capas.update({(kind, item_id): dropbox or url or ''
                          for item_id, dropbox, url in rows})
    return capas


def preparar(valores, itens):
    """Valida até três posições; nunca aceita URL ou produto fora do kit."""
    por_chave = {f'{item["kind"]}:{item["id"]}': item for item in itens}
    capas = capas_dos_itens(itens)
    fotos, erros, urls = [], [], set()
    if len(valores) != 3:
        return [], ['Escolha no máximo três fotos para o plano.']
    for ordem, valor in enumerate(valores, 1):
        if not valor:
            continue
        if not re.fullmatch(r'(receita|produto):[1-9][0-9]{0,9}', valor):
            erros.append('Escolha uma foto válida do catálogo.')
            continue
        item = por_chave.get(valor)
        if not item:
            erros.append('As fotos precisam ser de itens ou opções que fazem parte deste kit.')
            continue
        url = capas.get((item['kind'], item['id']))
        if not url:
            erros.append(f'{item["nome"]}: cadastre uma foto no catálogo antes de usá-la na capa.')
        elif url in urls:
            erros.append('Escolha fotos diferentes para cada posição da capa.')
        else:
            urls.add(url)
            fotos.append({'ordem': ordem, 'kind': item['kind'], 'id': item['id']})
    return fotos, erros


def imagens_do_kit(kit, fixos, todos, capas):
    """Ordem manual, com fallback legado se nenhuma foto válida restar."""
    por_chave = {f'{item["kind"]}:{item["id"]}': item for item in todos}
    manuais = [por_chave[chave(foto)] for foto in kit.fotos if chave(foto) in por_chave]

    def imagens(itens):
        resultado, urls = [], set()
        for item in itens:
            url = capas.get((item['kind'], item['id']))
            if url and url not in urls:
                resultado.append({'url': url, 'nome': item['nome']})
                urls.add(url)
            if len(resultado) == 3:
                break
        return resultado

    return imagens(manuais) or imagens(fixos)
