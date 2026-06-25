"""Importacao em lote de precos internos (e cadastro automatico).

Fluxo: dono cola lista TSV (Nome Categoria Preco Unidade) → `parse_lista`
extrai → `classificar` casa contra Receita/Produto existentes →
`aplicar` atualiza preco_interno dos casados e cria Produto novo pros
nao-casados (categoria Padaria).

REGRA DE NEGOCIO confirmada (25/06/2026): listas vindas do controle do
dono misturam Padaria (= produtos de venda interna) com Fornecedor
(insumos/embalagens/equipamentos) e Funcionarios (folha/despesa). Apenas
"Padaria" entra no catalogo — as outras sao **ignoradas** com motivo
listado pro dono.

Idempotente: rodar 2x nao duplica. Match via `_norm` (lower + sem
acentos + whitespace colapsado).
"""
import re
import unicodedata
from decimal import Decimal, InvalidOperation

from app.extensions import db
from app.models import Produto, Receita

CATEGORIAS_IGNORAR = {'fornecedor', 'funcionarios'}


def _norm(s):
    if not s:
        return ''
    s = unicodedata.normalize('NFKD', s)
    s = ''.join(c for c in s if not unicodedata.combining(c))
    return re.sub(r'\s+', ' ', s.strip().lower())


def _parse_preco(raw):
    """Converte 'R$ 1.234,56' ou '1234.56' ou '6,50' em float. None se invalido."""
    if not raw:
        return None
    s = re.sub(r'[^\d,.-]', '', raw)
    if not s:
        return None
    # Heuristica BR: se tem , depois do ultimo . (ou so ,), virgula = decimal.
    if ',' in s and ('.' not in s or s.rfind(',') > s.rfind('.')):
        s = s.replace('.', '').replace(',', '.')
    try:
        return float(Decimal(s))
    except (InvalidOperation, ValueError):
        return None


def parse_lista(texto):
    """Parseia texto colado. Retorna [{nome, categoria, preco, unidade}].

    Aceita TSV (`\\t`) ou colunas separadas por 2+ espacos. Pula cabecalho
    (linha que comeca com Produto/Item/Nome). Linhas sem >=3 colunas
    validas sao descartadas em silencio (preview mostra so o aproveitado).
    """
    out = []
    for linha in (texto or '').splitlines():
        cru = linha.rstrip()
        if not cru.strip():
            continue
        if '\t' in cru:
            partes = [p.strip() for p in cru.split('\t')]
        else:
            partes = [p.strip() for p in re.split(r' {2,}', cru.strip())]
        partes = [p for p in partes if p]
        if len(partes) < 3:
            continue
        if partes[0].lower() in ('produto', 'item', 'nome'):
            continue
        preco = _parse_preco(partes[2])
        if preco is None:
            continue
        out.append({
            'nome': partes[0],
            'categoria': partes[1],
            'preco': preco,
            'unidade': partes[3] if len(partes) > 3 else 'un',
        })
    return out


def classificar(linhas):
    """Para cada linha, decide acao (atualizar|criar) e match. Retorna lista
    ordenada com TODAS as linhas + uma flag `sugestao_marcar` que controla
    qual checkbox vem marcado por default no preview.

    O dono marca/desmarca caso a caso na tela antes de confirmar — entao a
    decisao de incluir/excluir Fornecedor e Funcionarios fica com ele, nao
    com o filtro automatico. Mantemos a flag pra ainda DESMARCAR essas
    categorias por default (evitar marcar 47 sem querer).

    Cada item: {idx, linha, acao, tipo, obj, preco_atual, sugestao_marcar}.
    - acao = 'atualizar' se casou com Receita/Produto existente; 'criar' se nao.
    - obj = a entidade casada (None se 'criar').
    - tipo = 'receita' | 'produto' | None.
    - preco_atual = preco_interno atual (pra mostrar mudanca no preview).
    - sugestao_marcar = True se Padaria, False se Fornecedor/Funcionarios.
    """
    receitas = Receita.query.filter(Receita.arquivada_em.is_(None)).all()
    produtos = Produto.query.filter_by(ativo=True).all()
    idx = {}
    for r in receitas:
        idx[_norm(r.nome)] = ('receita', r)
    for p in produtos:
        idx.setdefault(_norm(p.nome), ('produto', p))

    out = []
    for i, linha in enumerate(linhas):
        cat_norm = _norm(linha['categoria'])
        match = idx.get(_norm(linha['nome']))
        if match:
            tipo, obj = match
            acao = 'atualizar'
            preco_atual = getattr(obj, 'preco_interno', None)
        else:
            tipo, obj = None, None
            acao = 'criar'
            preco_atual = None
        out.append({
            'idx': i,
            'linha': linha,
            'acao': acao,
            'tipo': tipo,
            'obj': obj,
            'preco_atual': preco_atual,
            'sugestao_marcar': cat_norm not in CATEGORIAS_IGNORAR,
        })
    return out


def aplicar(plano, indices_marcados):
    """Persiste apenas os indices marcados pelo dono. Retorna contadores.

    `indices_marcados`: iteravel de ints (idx do item no plano).
    """
    marcados = set(indices_marcados)
    n_atual = n_criado = 0
    for item in plano:
        if item['idx'] not in marcados:
            continue
        linha = item['linha']
        if item['acao'] == 'atualizar':
            item['obj'].preco_interno = linha['preco']
            n_atual += 1
        else:
            db.session.add(Produto(
                nome=linha['nome'].strip(),
                categoria=(linha['categoria'].strip() or 'Padaria'),
                preco_interno=linha['preco'],
                ativo=True,
            ))
            n_criado += 1
    db.session.commit()
    return {'atualizados': n_atual, 'criados': n_criado,
            'desmarcados': len(plano) - len(marcados)}
