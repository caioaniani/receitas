"""Validação integral dos itens digitados nos pedidos, antes de gravar."""

from app.models import MateriaPrima, Produto, Receita
from app.utils import normalizar_busca


class PedidoSemItens(ValueError):
    """Formulário vazio, sem tentativa de salvar uma linha preenchida."""


def linhas_formulario(form, parse_item_id):
    """Conserva inclusive linhas incompletas para reapresentar um erro."""
    campos = {campo: form.getlist(f'item_{campo}[]')
              for campo in ('id', 'nome', 'qtd', 'estado', 'obs')}
    tamanho = len(campos['id'])
    desalinhado = (len(campos['qtd']) != tamanho
                   or any(valores and len(valores) != tamanho
                          for campo, valores in campos.items() if campo not in ('id', 'qtd')))
    linhas = []
    for i in range(max(map(len, campos.values()), default=0)):
        linha = {campo: valores[i] if i < len(valores) else ''
                 for campo, valores in campos.items()}
        linha['tipo'], linha['alvo_id'] = parse_item_id(linha['id'])
        linha['desalinhado'] = desalinhado
        linhas.append(linha)
    for tipo, model in (('receita', Receita), ('produto', Produto), ('mp', MateriaPrima)):
        ids = {l['alvo_id'] for l in linhas if l['tipo'] == tipo and l['alvo_id']}
        encontrados = {obj.id: obj for obj in model.query.filter(model.id.in_(ids)).all()} if ids else {}
        for linha in linhas:
            if linha['tipo'] != tipo:
                continue
            obj = encontrados.get(linha['alvo_id'])
            linha['encontrado'] = obj is not None
            if obj is not None:
                if linha['nome'] and normalizar_busca(linha['nome']) != normalizar_busca(obj.nome):
                    linha['encontrado'] = False
                linha['nome'] = linha['nome'] or obj.nome
                linha['em_gramas'] = (obj.medida_em_gramas if tipo == 'receita'
                                      else tipo == 'mp' and (obj.unidade or '').lower() in ('g', 'ml', 'kg', 'l'))
                linha['lote'] = (obj.lote_pedido or 0) if tipo == 'receita' and linha['em_gramas'] else 0
    return linhas


def validar_itens(linhas):
    """Uma linha inválida impede salvar o pedido inteiro, sem sucesso parcial."""
    itens = []
    if any(linha['desalinhado'] for linha in linhas):
        raise ValueError('Os dados dos itens chegaram incompletos. Confira cada linha antes de salvar novamente.')
    for numero, linha in enumerate(linhas, 1):
        # Formulários antigos enviavam a linha modelo oculta (vazia, qtd 1).
        # Uma linha ainda sem preenchimento também não representa um item.
        if (not any(linha[c].strip() for c in ('id', 'nome', 'estado', 'obs'))
                and linha['qtd'].strip() in ('', '1')):
            continue
        rotulo = linha['nome'].strip() or f'Item da linha {numero}'
        if not linha.get('encontrado'):
            raise ValueError(f'{rotulo}: selecione o produto na lista de resultados antes de salvar.')
        try:
            qtd = int(linha['qtd'])
        except (TypeError, ValueError):
            qtd = 0
        if qtd <= 0:
            raise ValueError(f'{rotulo}: informe uma quantidade inteira maior que zero.')
        estado = linha['estado'].strip().lower() or None
        if estado not in (None, 'backup', 'assado'):
            raise ValueError(f'{rotulo}: selecione um estado válido.')
        itens.append({
            'receita_id': linha['alvo_id'] if linha['tipo'] == 'receita' else None,
            'produto_id': linha['alvo_id'] if linha['tipo'] == 'produto' else None,
            'materia_prima_id': linha['alvo_id'] if linha['tipo'] == 'mp' else None,
            'quantidade': qtd,
            'observacao': linha['obs'].strip() or None,
            'estado': estado,
        })
    if not itens:
        raise PedidoSemItens('Adicione ao menos um item ao pedido.')
    return itens
