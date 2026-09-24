"""Elegibilidade dos minis no pedido de abastecimento das lojas.

Os minis de padaria pertencem às encomendas dos clientes, não ao pedido
loja → indústria recebido pelo motorista. A regra vale também para caixas
que os contenham e independe da flag de sugestão automática da receita.
Não altera venda, produção, estoque nem as guardas já existentes de MPs.
"""

import re
import unicodedata
from collections.abc import Mapping


def _normalizar(texto):
    return ''.join(c for c in unicodedata.normalize('NFKD', texto or '')
                   if not unicodedata.combining(c)).casefold()


def _nome_de_mini(nome, categoria=None):
    nome = _normalizar(nome)
    categoria = _normalizar(categoria)
    # Porções compradas de manteiga/mel são acompanhamentos, não os minis
    # de padaria. A presença de "mini" sozinha não pode bloqueá-las.
    if re.search(r'\bminis?\s+(?:manteigas?|potes?(?:\s+de)?\s+mel)\b', nome):
        return False
    return bool(re.search(r'\bminis?\b', nome)
                or re.search(r'\bminis?\b', categoria))


def _produto_contem_mini(produto, visitados):
    chave = id(produto)
    if chave in visitados:
        return False
    visitados.add(chave)
    if _nome_de_mini(produto.nome, getattr(produto, 'categoria', None)):
        return True
    for item in getattr(produto, 'itens', ()):
        # FK autoritativa: o nome legado do componente pode estar desatualizado.
        receita = getattr(item, 'receita', None)
        componente = getattr(item, 'produto_componente', None)
        if receita is not None:
            if _nome_de_mini(receita.nome, getattr(receita, 'categoria', None)):
                return True
        elif componente is not None:
            if _produto_contem_mini(componente, visitados):
                return True
        elif getattr(item, 'tipo', None) != 'mp' and _nome_de_mini(
                getattr(item, 'item_nome', None)):
            return True
    return False


def permite_item_loja(receita=None, produto=None, materia_prima=None):
    """Se o item passa pela regra dos minis, sem ampliar outras restrições.

    A flag ``sugerir_pedido_loja`` continua controlando a sugestão automática;
    as validações específicas de MP, arquivamento e composição continuam nos
    seus chamadores. Uma MP não é classificada como mini por seu nome.
    """
    if receita is not None and _nome_de_mini(
            receita.nome, getattr(receita, 'categoria', None)):
        return False
    return produto is None or not _produto_contem_mini(produto, set())


def _carregar_produtos(produto_ids):
    """Carrega só os produtos pedidos e suas composições, em lote por nível."""
    from sqlalchemy.orm import selectinload

    from app.models import Produto, ProdutoItem

    produtos = {}
    visitados = set()
    pendentes = set(produto_ids)
    while pendentes:
        lote = pendentes - visitados
        if not lote:
            break
        visitados.update(lote)
        carregados = (Produto.query.filter(Produto.id.in_(lote))
                      .options(
                          selectinload(Produto.itens).selectinload(ProdutoItem.receita),
                          selectinload(Produto.itens).selectinload(ProdutoItem.produto_componente))
                      .all())
        produtos.update((p.id, p) for p in carregados)
        pendentes = {item.produto_componente_id for p in carregados for item in p.itens
                     if item.produto_componente_id not in visitados
                     and item.produto_componente_id is not None}
    return produtos


def validar_itens_loja(itens):
    """Recusa minis antes da escrita, com os nomes dos itens envolvidos.

    Aceita objetos PedidoItem ou dicts com receita_id/produto_id. Os dicts
    também podem fornecer as relações já resolvidas, usadas quando não há FK.
    IDs são autoritativos e resolvidos em lote sem lazy-load de PedidoItem.
    Não aplica grandfather:
    um item enviado novamente para o pedido precisa continuar elegível.
    """
    from app.models import Receita

    itens = list(itens)

    def valor(item, campo):
        return item.get(campo) if isinstance(item, Mapping) else getattr(item, campo, None)

    def relacao_carregada(item, campo):
        # Não ler o descritor SQLAlchemy: mesmo testar se a relação é None
        # dispararia uma consulta para cada linha de PedidoItem.
        return item.get(campo) if isinstance(item, Mapping) else vars(item).get(campo)

    receita_ids = {int(valor(it, 'receita_id')) for it in itens if valor(it, 'receita_id')}
    produto_ids = {int(valor(it, 'produto_id')) for it in itens if valor(it, 'produto_id')}
    receitas = {r.id: r for r in Receita.query.filter(Receita.id.in_(receita_ids)).all()} \
        if receita_ids else {}
    produtos = _carregar_produtos(produto_ids)
    nomes = []
    for item in itens:
        receita_id = valor(item, 'receita_id')
        produto_id = valor(item, 'produto_id')
        receita = (receitas.get(int(receita_id)) if receita_id
                   else relacao_carregada(item, 'receita'))
        produto = (produtos.get(int(produto_id)) if produto_id
                   else relacao_carregada(item, 'produto'))
        if not permite_item_loja(receita=receita, produto=produto):
            nome = receita.nome if receita is not None else produto.nome
            if nome not in nomes:
                nomes.append(nome)
    if nomes:
        raise ValueError(
            'Minis não podem ser pedidos pelas lojas para recebimento pelo motorista: '
            + ', '.join(nomes) + '. Use o pedido de encomenda do cliente.')
