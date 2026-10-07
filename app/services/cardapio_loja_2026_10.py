"""Acerto do cardápio da LOJA pelo cardápio de papel (dono, 07/10/2026).

"Cadastrar e Atualizar preços e cadastrar produtos que não temos ainda,
lembrando que é cardápio de loja". Mexe SÓ em `preco_loja` (nunca site nem
atacado) e cria os itens que o papel tem e o cadastro não tinha.

- Preço: só muda se o valor atual ainda é o que estava no cadastro quando
  conferimos (`antes`) — se alguém já mexeu, a edição manda e o item volta
  no resumo como `divergente`.
- Item novo: nome normalizado exato; se já existe, não cria nem altera.
  "Queijo Quente no Croissant" copia a composição do "Queijo Quente no
  Brioche" trocando o brioche por 1 Croissant Tradicional; sem um brioche
  único na composição, não cria (resumo diz o motivo).
Roda pelo seed one-shot `migrations_legacy._seed_cardapio_loja_2026_10`.
Sem commit — quem chama decide.
"""
from app.extensions import db
from app.models import Produto, ProdutoItem, Receita
from app.utils import normalizar_busca

# (kind, nome, preco_loja antes, preco_loja novo)
PRECOS = (
    ('receita', 'Danish de queijo branco', 25.0, 28.0),
    ('produto', 'Adicional de Banana', 6.0, 7.0),
    ('produto', 'Granola 50g', None, 7.0),
    ('produto', 'Chocolate do padre Quente', 18.0, 15.0),
    ('produto', 'Chocolate do padre Quente Desnatado', 18.0, 15.0),
    ('produto', 'Chocolate do padre quente Leite vegetal', 18.0, 15.0),
    ('produto', 'Chocolate do Toddy Quente', 18.0, 15.0),
    ('produto', 'Chocolate do Toddy Quente leite vegetal', 18.0, 15.0),
)

# (nome, categoria, preco_loja) — revenda/adicional, sem composição
NOVOS_SIMPLES = (
    ('Guaraná', 'Bebidas', 10.0),
    ('Adicional de Manteiga', 'Pães na Chapa', 6.0),
)

NOME_QQ_CROISSANT = 'Queijo Quente no Croissant'
PRECO_QQ_CROISSANT = 35.0
NOME_QQ_BRIOCHE = 'Queijo Quente no Brioche'


def _norm(s):
    return normalizar_busca(s or '').strip()


def _achar(itens, nome):
    alvo = _norm(nome)
    achados = [x for x in itens if _norm(x.nome) == alvo]
    return achados


def _atualizar_precos(resumo):
    receitas = Receita.ativas().all()
    produtos = Produto.query.filter(Produto.ativo.is_(True)).all()
    for kind, nome, antes, novo in PRECOS:
        achados = _achar(receitas if kind == 'receita' else produtos, nome)
        if len(achados) != 1:
            resumo['nao_achados'].append(f'{nome}({len(achados)})')
            continue
        item = achados[0]
        atual = item.preco_loja
        if atual == novo:
            resumo['ja_certos'].append(nome)
        elif atual == antes:
            item.preco_loja = novo
            resumo['precos'].append(f'{nome}:{antes}->{novo}')
        else:
            resumo['divergentes'].append(f'{nome}:atual={atual}')


def _criar_simples(resumo, produtos):
    for nome, categoria, preco in NOVOS_SIMPLES:
        if _achar(produtos, nome):
            resumo['ja_existiam'].append(nome)
            continue
        p = Produto(nome=nome, categoria=categoria, ativo=True, preco_loja=preco)
        db.session.add(p)
        db.session.flush()
        resumo['criados'].append(f'{nome}#{p.id}')


def _criar_queijo_quente_croissant(resumo, produtos):
    if _achar(produtos, NOME_QQ_CROISSANT):
        resumo['ja_existiam'].append(NOME_QQ_CROISSANT)
        return
    base = _achar(produtos, NOME_QQ_BRIOCHE)
    croissant = _achar(Receita.ativas().all(), 'Croissant Tradicional')
    if len(base) != 1 or len(croissant) != 1:
        resumo['qq_croissant'] = (f'nao_criado: base={len(base)} '
                                  f'croissant={len(croissant)}')
        return
    base, croissant = base[0], croissant[0]
    itens = ProdutoItem.query.filter_by(produto_id=base.id).all()

    def _e_brioche(i):
        nome = i.receita.nome if i.receita_id and i.receita else i.item_nome
        return i.tipo == 'receita' and 'brioche' in _norm(nome)

    brioches = [i for i in itens if _e_brioche(i)]
    if len(brioches) != 1:
        resumo['qq_croissant'] = f'nao_criado: brioches_na_composicao={len(brioches)}'
        return
    novo = Produto(nome=NOME_QQ_CROISSANT, categoria=base.categoria, ativo=True,
                   preco_loja=PRECO_QQ_CROISSANT, descricao=base.descricao)
    db.session.add(novo)
    db.session.flush()
    for i in itens:
        if i is brioches[0]:
            db.session.add(ProdutoItem(produto_id=novo.id, tipo='receita',
                                       receita_id=croissant.id,
                                       item_nome=croissant.nome, quantidade=1))
        else:
            db.session.add(ProdutoItem(
                produto_id=novo.id, tipo=i.tipo, receita_id=i.receita_id,
                produto_componente_id=i.produto_componente_id,
                materia_prima_id=i.materia_prima_id, item_nome=i.item_nome,
                quantidade=i.quantidade))
    db.session.flush()
    resumo['criados'].append(f'{NOME_QQ_CROISSANT}#{novo.id}')
    resumo['qq_croissant'] = f'componentes={len(itens)}'


def aplicar():
    resumo = {'precos': [], 'ja_certos': [], 'divergentes': [], 'nao_achados': [],
              'criados': [], 'ja_existiam': [], 'qq_croissant': ''}
    _atualizar_precos(resumo)
    produtos = Produto.query.all()
    _criar_simples(resumo, produtos)
    _criar_queijo_quente_croissant(resumo, produtos)
    db.session.flush()
    return resumo
