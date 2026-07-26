"""Menu degustação CONFIGURÁVEL do site (26/07/2026).

Pedido do dono: "no site, preciso colocar uma opção para o menu degustação
dos minis, uma pré-seleção de 5 de cada. Porém se o cliente quiser alterar
as quantidades não tem problema; porém ele deve ser obrigado a selecionar
30 unidades dos minis independente de quais". Decisões dele (AskUserQuestion):

- **preço varia conforme a escolha**, cadastrado POR MINI → o preço do menu
  é a SOMA de `preço do mini x quantidade escolhida`; o ANUNCIADO é o
  MÍNIMO possível ("a partir de", ver `preco_minimo`);
- **sem teto por item** (o "máximo 10 de cada" foi revogado no mesmo dia —
  ver `regras`);
- a regra vale **só pra este menu** (não vira comportamento global de cesta).

MODELAGEM — o menu é um Produto-cesta NORMAL:
- `ProdutoItem` (a composição que o admin já cadastra) = os minis
  disponíveis; `ProdutoItem.quantidade` = a PRÉ-SELEÇÃO (5 de cada);
- `Produto.menu_configuravel` liga o modo; `menu_total_unidades` (30) é a
  trava obrigatória e `menu_max_por_item` (opcional) limita por item;
- `ProdutoItem.preco_menu` = preço por unidade DAQUELE mini DENTRO deste
  menu. Mora no ProdutoItem, não na Receita, de propósito: os minis não são
  vendidos avulsos e um `preco_site` neles os publicaria na vitrine
  (`loja_catalogo.produtos_publicados` usa `preco_site > 0` como flag).

ENDEREÇAMENTO — a escolha do cliente viaja como `{produto_item_id: qtd}`.
O ProdutoItem é o endereço canônico do "slot": carrega a FK do mini E o
preço dentro deste menu. O cliente NUNCA manda `receita_id`, então um POST
forjado não consegue enfiar no menu um item que não faz parte dele.

LEITURA DEFENSIVA das colunas novas (`getattr`): o ALTER e o modelo sobem em
commits separados (CLAUDE.md "Schema migrations"). Enquanto o modelo não tem
as colunas, `eh_menu` é sempre False e o site se comporta exatamente como
antes — sem AttributeError em produção seja qual for a ordem de deploy.
"""
import logging
from decimal import Decimal

logger = logging.getLogger(__name__)

# Fallback do total quando o admin liga o modo e esquece de preencher.
TOTAL_PADRAO = 30

# NÃO existe teto padrão por item: campo em branco = SEM LIMITE (o cliente
# pode fechar as 30 com um mini só). Decisão do dono 26/07/2026 — "quero
# tirar a regra de 30/10, quero que tenha 30 unidades do mini independente
# de quantos sejam". Quem quiser limitar preenche `menu_max_por_item`.

# Teto de slots que um menu pode ter. Protege o cookie de sessão (a escolha
# viaja dentro dele) e a tela. Menu maior que isso é erro de cadastro.
MAX_SLOTS = 40


def eh_menu(produto):
    """True se este Produto é um menu configurável COM composição cadastrada.

    Sem componentes não há o que escolher — trata como cesta comum (evita
    que uma flag ligada por engano numa cesta vazia trave a venda)."""
    if produto is None:
        return False
    if not bool(getattr(produto, 'menu_configuravel', False)):
        return False
    return bool(produto.itens)


def regras(produto):
    """(total_obrigatorio, max_por_item) do menu.

    `menu_max_por_item` em branco = SEM LIMITE por item: o teto vira o
    próprio total (dá pra fechar as 30 com um mini só). Teto acima do total
    também é normalizado pro total — um "máximo 50" num menu de 30 não
    significa nada e só confundiria a mensagem na tela e no cardápio."""
    total = int(getattr(produto, 'menu_total_unidades', None) or TOTAL_PADRAO)
    teto = getattr(produto, 'menu_max_por_item', None)
    teto = int(teto) if teto else total
    return total, min(teto, total)


def _alvo_do_slot(pi):
    """(coluna_estoque, id) do componente — espelha `componentes_de_cesta`.
    None se órfão (FK NULL): slot órfão não entra no menu (não dá pra baixar
    estoque nem produzir o que não tem FK). Admin resolve em
    /produtos/cestas/orfaos."""
    if pi.tipo == 'receita' and pi.receita_id:
        return ('receita_id', pi.receita_id)
    if pi.tipo == 'produto' and pi.produto_componente_id:
        return ('produto_id', pi.produto_componente_id)
    if pi.tipo == 'mp' and pi.materia_prima_id:
        return ('materia_prima_id', pi.materia_prima_id)
    return None


def slots(produto):
    """Os componentes escolhíveis do menu, em ordem estável (id do
    ProdutoItem). Cada slot:

        {'pi_id', 'col', 'alvo_id', 'nome', 'preco' (float|None),
         'padrao' (int), 'max' (int)}

    Slot órfão (sem FK) é PULADO com WARNING — mesma política do
    `cestas.componentes_de_cesta`. `preco` None = admin ainda não cadastrou
    o preço daquele mini (o checkout recusa o menu; ver `preco`)."""
    if produto is None or not produto.itens:
        return []
    _total, teto = regras(produto)
    out = []
    for pi in sorted(produto.itens, key=lambda x: x.id or 0):
        alvo = _alvo_do_slot(pi)
        if not alvo:
            logger.warning(
                'menu %s: ProdutoItem #%s órfão (item_nome=%r) — fora do '
                'menu. Resolver em /produtos/cestas/orfaos.',
                getattr(produto, 'id', '?'), pi.id, pi.item_nome)
            continue
        pm = getattr(pi, 'preco_menu', None)
        out.append({
            'pi_id': pi.id,
            'col': alvo[0],
            'alvo_id': alvo[1],
            'nome': pi.nome_resolvido,
            # `preco` (float) é o que vai pro JSON da tela; `preco_dec`
            # (Decimal) é o que a CONTA usa — dinheiro não passa por float
            # (decisão B4 do CLAUDE.md).
            'preco_dec': Decimal(pm) if pm is not None else None,
            'preco': float(pm) if pm is not None else None,
            # A pré-seleção é a quantidade do cadastro, já limitada pelo teto
            # (cadastro incoerente não pode nascer inválido na tela).
            'padrao': max(0, min(int(round(float(pi.quantidade or 0))), teto)),
            'max': teto,
        })
        if len(out) >= MAX_SLOTS:
            logger.warning('menu %s: mais de %d componentes — o excedente '
                           'ficou fora.', getattr(produto, 'id', '?'),
                           MAX_SLOTS)
            break
    return out


def composicao_padrao(produto):
    """A pré-seleção do cadastro: {pi_id: qtd}. É o que a página do produto
    já vem marcada e o que vale quando o cliente não mexe em nada."""
    return {s['pi_id']: s['padrao'] for s in slots(produto) if s['padrao'] > 0}


def normalizar(produto, comp_raw):
    """Sanitiza a escolha do cliente contra o cadastro. NUNCA confia no
    navegador:

    - só `pi_id` que pertence a ESTE menu sobrevive (o resto é descartado);
    - quantidade vira inteiro em [0, max_por_item];
    - quantidade 0 some do dict (slot não escolhido).

    `comp_raw` aceita dict {pi_id: qtd} ou lista de pares [[pi_id, qtd], ...]
    (o formato compacto que viaja no cookie/JSON).

    DUAS situações que NÃO podem se confundir (achado de revisão 26/07/2026,
    era bug de dinheiro):
    - **não veio escolha nenhuma** (`None`/vazio) → composição PADRÃO: o
      cliente não mexeu, compra a pré-seleção;
    - **veio escolha e NADA dela sobreviveu** → devolve `{}` VAZIO, pra o
      `validar` recusar. Cair na pré-seleção aqui seria trocar em silêncio o
      que ele montou (e o preço que ele viu) por outra coisa. Isso acontece
      de verdade: `produtos.salvar_composicao` APAGA e RECRIA os
      `ProdutoItem` a cada salvamento e o Postgres nunca reusa id, então
      QUALQUER edição do menu invalida os `pi_id` de todo carrinho em voo.

    NÃO valida o total — isso é `validar`, pra a mensagem de erro poder dizer
    quanto faltou."""
    _total, teto = regras(produto)
    validos = {s['pi_id']: s for s in slots(produto)}
    pares = []
    if isinstance(comp_raw, dict):
        pares = list(comp_raw.items())
    elif isinstance(comp_raw, (list, tuple)):
        for par in comp_raw:
            if isinstance(par, (list, tuple)) and len(par) == 2:
                pares.append((par[0], par[1]))
            elif isinstance(par, dict):
                pares.append((par.get('pi_id'), par.get('qtd')))
    if not pares:
        return composicao_padrao(produto)     # não escolheu nada
    out = {}
    for pi_id, qtd in pares:
        try:
            pi_id = int(pi_id)
            qtd = int(qtd)
        except (TypeError, ValueError):
            continue
        if pi_id not in validos or qtd <= 0:
            continue
        out[pi_id] = min(qtd, teto)
    return out                                 # pode sair VAZIO — ver acima


def validar(produto, comp):
    """None se a escolha é válida; senão a mensagem PRO CLIENTE.

    Regra do dono: o total tem que bater EXATO (não "pelo menos"). O JS já
    trava na tela — aqui é a autoridade (carrinho velho, aba parada, POST
    forjado). Nunca "conserta" em silêncio: o cliente refaz a escolha."""
    total, teto = regras(produto)
    if not comp:
        # Escolha inteira invalidada (o admin editou o menu depois que o
        # cliente montou o dele — ver `normalizar`). Mensagem PRÓPRIA: dizer
        # "você escolheu 0" seria confuso, ele escolheu 30.
        return (f'O {produto.nome} mudou depois que você montou o seu. '
                f'Monte de novo, por favor.')
    escolhido = sum(comp.values())
    if escolhido != total:
        return (f'O {produto.nome} precisa somar exatamente {total} unidades '
                f'— você escolheu {escolhido}. Refaça a seleção.')
    for pi_id, qtd in comp.items():
        if qtd > teto:
            return (f'O {produto.nome} aceita no máximo {teto} unidades de '
                    f'cada item.')
    return None


def preco(produto, comp, *, slots_=None):
    """Preço de UMA unidade do menu = soma de `preco_menu x qtd escolhida`.
    Decisão do dono: "cadastrar preço por mini". Em `Decimal` do começo ao
    fim (dinheiro nunca passa por float — decisão B4 do CLAUDE.md).

    Devolve None se algum item ESCOLHIDO está sem preço cadastrado (ou com
    preço <= 0) — fail-close: o checkout recusa o menu em vez de cobrar a
    menos. O admin vê o pendente na tela da cesta.

    `slots_` evita re-varrer o cadastro quando o chamador já tem a lista."""
    if not comp:
        # Composição VAZIA não é "de graça": é escolha invalidada (o admin
        # editou o menu depois que o cliente montou). Devolver Decimal('0')
        # fazia o carrinho exibir R$ 0,00 — achado de revisão 26/07/2026.
        return None
    por_id = {s['pi_id']: s for s in (slots(produto) if slots_ is None
                                      else slots_)}
    total = Decimal('0')
    for pi_id, qtd in comp.items():
        s = por_id.get(pi_id)
        if s is None:
            continue
        if s['preco_dec'] is None or s['preco_dec'] <= 0:
            logger.warning('menu %s: componente %r sem preco_menu — menu '
                           'não pode ser vendido.',
                           getattr(produto, 'id', '?'), s['nome'])
            return None
        total += s['preco_dec'] * qtd
    return total


def preco_minimo(produto, *, slots_=None):
    """Menor preço possível de UMA unidade do menu — o "a partir de" que a
    vitrine e o cardápio exibem (decisão do dono 26/07/2026).

    Enche o total com os itens mais BARATOS, respeitando o teto por item
    (sem teto, cabe tudo no mais barato). É o piso REAL: nenhuma montagem
    válida sai por menos que isso — por isso pode ser anunciado.

    None quando algum slot está sem preço (menu não é vendável) ou quando o
    total NÃO É ALCANÇÁVEL — teto × nº de itens < total. Esse segundo caso é
    cadastro impossível (ex.: 3 minis com teto 5 num menu de 30): o cliente
    nunca fecharia a seleção e ficaria travado na tela."""
    total, teto = regras(produto)
    slots_ = slots(produto) if slots_ is None else slots_
    if not slots_:
        return None
    precos = []
    for s in slots_:
        if s['preco_dec'] is None or s['preco_dec'] <= 0:
            return None
        precos.append(s['preco_dec'])
    if teto * len(precos) < total:
        logger.warning('menu %s: total %d inalcançável — %d itens com teto '
                       '%d dão no máximo %d.', getattr(produto, 'id', '?'),
                       total, len(precos), teto, teto * len(precos))
        return None
    soma = Decimal('0')
    faltam = total
    for p in sorted(precos):
        leva = min(teto, faltam)
        soma += p * leva
        faltam -= leva
        if faltam <= 0:
            break
    return soma


def resumo(produto, comp, *, slots_=None):
    """[{'nome', 'qtd', 'preco'}] na ordem dos slots — pro carrinho, o
    e-mail, o painel de entregas e o PDF do motorista mostrarem O QUE o
    cliente escolheu (o cadastro não conta mais essa história)."""
    return [{'nome': s['nome'], 'qtd': comp[s['pi_id']],
             'preco': s['preco']}
            for s in (slots(produto) if slots_ is None else slots_)
            if comp.get(s['pi_id'])]


def chave(comp):
    """Assinatura estável da composição, pra o carrinho tratar dois menus de
    composições DIFERENTES como linhas separadas (senão somariam quantidade
    na mesma linha e o cliente receberia a composição errada). Espelhada no
    `carrinho.js::_chaveComp` — mudou aqui, muda lá."""
    if not comp:
        return ''
    return ','.join(f'{int(k)}:{int(v)}' for k, v in sorted(comp.items()))


def compactar(comp):
    """{pi_id: qtd} → [[pi_id, qtd], ...] ordenado. Formato que viaja no
    cookie de sessão e no JSON do carrinho (metade dos bytes de um dict com
    chaves string)."""
    return [[int(k), int(v)] for k, v in sorted((comp or {}).items())]
