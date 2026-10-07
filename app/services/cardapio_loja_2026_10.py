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
    ja_croissant = [i for i in itens if i.tipo == 'receita' and i.receita_id == croissant.id]
    # Em prod (07/10/2026) a base já tinha Croissant Tradicional no lugar do
    # brioche (cadastro do dono, intocado aqui): copia como está.
    if not (len(brioches) == 1 or (not brioches and len(ja_croissant) == 1)):
        resumo['qq_croissant'] = (f'nao_criado: brioches_na_composicao={len(brioches)} '
                                  f'croissants={len(ja_croissant)}')
        return
    trocar = brioches[0] if brioches else ja_croissant[0]
    novo = Produto(nome=NOME_QQ_CROISSANT, categoria=base.categoria, ativo=True,
                   preco_loja=PRECO_QQ_CROISSANT, descricao=base.descricao)
    db.session.add(novo)
    db.session.flush()
    for i in itens:
        if i is trocar:
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


def criar_queijo_quente_croissant():
    """Só a criação do Queijo Quente no Croissant (seed v2: a 1ª rodada não
    criou porque a base não tinha brioche na composição)."""
    resumo = {'criados': [], 'ja_existiam': [], 'qq_croissant': ''}
    _criar_queijo_quente_croissant(resumo, Produto.query.all())
    db.session.flush()
    return resumo


def aplicar():
    resumo = {'precos': [], 'ja_certos': [], 'divergentes': [], 'nao_achados': [],
              'criados': [], 'ja_existiam': [], 'qq_croissant': ''}
    _atualizar_precos(resumo)
    produtos = Produto.query.all()
    _criar_simples(resumo, produtos)
    _criar_queijo_quente_croissant(resumo, produtos)
    db.session.flush()
    return resumo


NOME_DUPLICADO = 'Queijo Quente no Croissant (duplicado)'


def unificar_queijo_quente_croissant():
    """Dono 07/10/2026: "O queijo quente é os lanches, tem ele em todas as
    opções de pães sourdough e croissant tradicional" — não existe queijo
    quente no brioche; o "Queijo Quente no Brioche" (que já baixava 1
    Croissant Tradicional) É o do croissant. Renomeia ele (preserva vínculos
    do PDV e histórico de vendas) e desativa a cópia criada no seed v2.
    Só age se a base tem Croissant Tradicional e nenhum brioche."""
    resumo = {'renomeado': '', 'desativado': '', 'motivo': ''}
    produtos = Produto.query.all()
    base = _achar(produtos, NOME_QQ_BRIOCHE)
    croissant = _achar(Receita.ativas().all(), 'Croissant Tradicional')
    if len(base) != 1 or len(croissant) != 1:
        resumo['motivo'] = f'base={len(base)} croissant={len(croissant)}'
        return resumo
    base, croissant = base[0], croissant[0]
    itens = ProdutoItem.query.filter_by(produto_id=base.id).all()
    tem_croissant = any(i.tipo == 'receita' and i.receita_id == croissant.id for i in itens)
    tem_brioche = any(i.tipo == 'receita' and 'brioche' in _norm(
        i.receita.nome if i.receita else i.item_nome) for i in itens)
    if not tem_croissant or tem_brioche:
        resumo['motivo'] = f'composicao_inesperada croissant={tem_croissant} brioche={tem_brioche}'
        return resumo
    for copia in _achar(produtos, NOME_QQ_CROISSANT):
        if copia.id != base.id:
            copia.ativo = False
            copia.nome = NOME_DUPLICADO
            resumo['desativado'] = f'#{copia.id}'
    base.nome = NOME_QQ_CROISSANT
    for ref in ProdutoItem.query.filter_by(produto_componente_id=base.id).all():
        ref.item_nome = NOME_QQ_CROISSANT
    db.session.flush()
    resumo['renomeado'] = f'#{base.id}'
    return resumo


NOME_QQ_CRANBERRY = 'Queijo quente no cranberry'


def acertar_recheio_cranberry():
    """Dono 07/10/2026: o queijo quente no cranberry "leva os mesmos 60g" dos
    outros (60 g de queijo prato + 60 g de queijo branco). O cadastro tinha
    40 g de prato e nenhum queijo branco. Só age se o prato ainda é 40 e não
    há queijo branco (edição do dono manda)."""
    from app.models import MateriaPrima
    resumo = {'prato': '', 'branco': '', 'motivo': ''}
    base = _achar(Produto.query.filter(Produto.ativo.is_(True)).all(), NOME_QQ_CRANBERRY)
    mps = MateriaPrima.query.filter(MateriaPrima.arquivada_em.is_(None)).all()
    prato, branco = _achar(mps, 'Queijo Prato'), _achar(mps, 'Queijo Branco')
    if len(base) != 1 or len(prato) != 1 or len(branco) != 1:
        resumo['motivo'] = f'base={len(base)} prato={len(prato)} branco={len(branco)}'
        return resumo
    base, prato, branco = base[0], prato[0], branco[0]
    itens = ProdutoItem.query.filter_by(produto_id=base.id, tipo='mp').all()
    i_prato = [i for i in itens if i.materia_prima_id == prato.id]
    i_branco = [i for i in itens if i.materia_prima_id == branco.id]
    if len(i_prato) != 1 or i_prato[0].quantidade != 40 or i_branco:
        resumo['motivo'] = (f'composicao_inesperada prato={[i.quantidade for i in i_prato]} '
                            f'branco={len(i_branco)}')
        return resumo
    i_prato[0].quantidade = 60
    db.session.add(ProdutoItem(produto_id=base.id, tipo='mp', materia_prima_id=branco.id,
                               item_nome=branco.nome, quantidade=60))
    db.session.flush()
    resumo.update(prato='40->60', branco='+60')
    return resumo


# Dono 07/10/2026: "Cestas só tem que estar as cestas, o restante é lanche".
# Só move quem ainda está em 'Cestas' (mudança posterior do dono manda).
NOVA_CATEGORIA = {
    'Lanches': (
        'Misto branco no cranberry', 'Misto no Brioche', 'Misto no cranberry',
        'Misto no Croissant', 'Misto no Pão Francês', 'Misto no Sourdough 7 Grãos',
        'Misto no Sourdough Integral', 'Misto no Sourdough Nozes e Azeitonas',
        'Misto no Sourdough Tradicional', 'Peito de Peru no Brioche',
        'Peito de Peru no Croissant', 'Peito de Peru no Pão Francês',
        'Peito de Peru no Sourdough 7 Grãos', 'Peito de Peru no Sourdough Integral',
        'Peito de Peru no Sourdough Nozes', 'Peito de Peru no Sourdough Tradicional',
        'Peito de peru queijo branco no cranberry', 'Queijo branco no cranberry',
        'Queijo quente no cranberry', 'Queijo Quente no Croissant',
        'Queijo Quente no Francês', 'Queijo Quente no Sourdough 7 grãos',
        'Queijo Quente no Sourdough Integral', 'Queijo Quente no Sourdough nozes',
        'Queijo Quente no Sourdough Tradicional', 'Ovos Orgânicos Mexidos (3 ovos)',
        'Cone de Pão de Queijo (10un)', 'Cone de Pão de Queijo (5un)'),
    'Pães na Chapa': (
        'Brioche com Manteiga e Requeijão', 'Brioche na Chapa (3 fatias)',
        'Cranberry na chapa com manteiga', 'Cranberry na chapa com manteiga e requeijão',
        'Croissant Francês com Manteiga e Requeijão', 'Croissant Francês na Chapa',
        'Pão Francês com Manteiga (2 fatias)', 'Pão Francês com Manteiga e Requeijão',
        'Sourdough com Manteiga (2 fatias)', 'Sourdough com Manteiga e Requeijão'),
    'Bowls': ('Açaí com Banana Batido', 'Adicional de Banana'),
    'Acompanhamentos': ('Granola 50g', 'Nozes caramelizadas'),
    'Bebidas': ('Água Prata 370ml',),
    'molhos': ('Molho Pesto 600ml',),
}


def separar_cestas_de_lanches():
    resumo = {'movidos': 0, 'ja_fora': [], 'nao_achados': []}
    produtos = Produto.query.filter(Produto.ativo.is_(True)).all()
    for categoria, nomes in NOVA_CATEGORIA.items():
        for nome in nomes:
            achados = _achar(produtos, nome)
            if not achados:
                resumo['nao_achados'].append(nome)
            for p in achados:  # homônimos (ex.: 2x Misto branco no cranberry) vão juntos
                if (p.categoria or '').strip() == 'Cestas':
                    p.categoria = categoria
                    resumo['movidos'] += 1
                else:
                    resumo['ja_fora'].append(nome)
    db.session.flush()
    resumo['ficam_em_cestas'] = sorted(p.nome for p in produtos
                                       if (p.categoria or '').strip() == 'Cestas')
    return resumo
