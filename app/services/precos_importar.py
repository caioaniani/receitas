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
    """Casa cada linha contra Receita/Produto existente por nome normalizado.

    Retorna dict com 3 listas:
    - atualizar: [(linha, 'receita'|'produto', objeto)] — vai setar preco_interno
    - criar: [linha] — vai criar Produto novo (categoria 'Padaria')
    - ignorar: [(linha, motivo)] — Fornecedor/Funcionarios

    Receita tem prioridade sobre Produto em caso de colisao de nome
    (raramente acontece, mas se acontecer atualizamos a Receita).
    """
    receitas = Receita.query.filter(Receita.arquivada_em.is_(None)).all()
    produtos = Produto.query.filter_by(ativo=True).all()
    idx = {}
    for r in receitas:
        idx[_norm(r.nome)] = ('receita', r)
    for p in produtos:
        idx.setdefault(_norm(p.nome), ('produto', p))

    plano = {'atualizar': [], 'criar': [], 'ignorar': []}
    for linha in linhas:
        cat_norm = _norm(linha['categoria'])
        if cat_norm in CATEGORIAS_IGNORAR:
            plano['ignorar'].append(
                (linha, f'categoria "{linha["categoria"]}" — '
                        f'insumo/folha, não é produto de venda'))
            continue
        match = idx.get(_norm(linha['nome']))
        if match:
            plano['atualizar'].append((linha, match[0], match[1]))
        else:
            plano['criar'].append(linha)
    return plano


def aplicar(plano):
    """Persiste o plano. Retorna {atualizados, criados, ignorados}."""
    n_atual = n_criado = 0
    for linha, _tipo, obj in plano['atualizar']:
        obj.preco_interno = linha['preco']
        n_atual += 1
    for linha in plano['criar']:
        db.session.add(Produto(
            nome=linha['nome'].strip(),
            categoria=(linha['categoria'].strip() or 'Padaria'),
            preco_interno=linha['preco'],
            ativo=True,
        ))
        n_criado += 1
    db.session.commit()
    return {'atualizados': n_atual, 'criados': n_criado,
            'ignorados': len(plano['ignorar'])}
