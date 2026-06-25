"""Gera descricoes SEO pra produtos do catalogo da loja online.

Descricoes SEO sao curtas (200-280 chars), apetitosas, com palavras-chave
naturais ("artesanal", "fermentacao natural", "padaria em Sao Paulo") sem
soar exagerado. Vao no `<meta description>`, no JSON-LD `Product` e no
card do site.

Fluxo: a tela admin (/admin/seo/descricoes) chama `sugerir_para_receita`
ou `sugerir_para_produto`; o resultado e MOSTRADO num textarea editavel —
o dono revisa e clica Salvar. So entao vira publico.

NUNCA publica automaticamente — controle total do texto eh requisito do
dono (decisao 22/06/2026).
"""
import logging
import os

logger = logging.getLogger(__name__)

# Sonnet 4.6 por decisao do dono (25/06/2026) — padronizacao dos modelos.
# Geracao de descricao e tarefa simples (baixo volume: ~1x por receita), o
# custo extra vs Haiku e marginal aqui.
MODELO = 'claude-sonnet-4-6'

_INSTRUCOES_BASE = (
    "Escreva uma descricao curta (2 frases, MAXIMO 220 caracteres) "
    "para a vitrine de uma padaria artesanal em Sao Paulo.\n\n"
    "Regras:\n"
    "- Portugues correto (sem erros, sem giria).\n"
    "- Apetitosa mas SEM exagero ('delicioso', 'incrivel' sao proibidos).\n"
    "- Use FATOS (textura, processo, origem dos ingredientes) e SENSACAO\n"
    "  (massa aerada, casca crocante, sabor amanteigado).\n"
    "- Mencione naturalmente palavras-chave quando fizer sentido: "
    "'artesanal', 'fermentacao natural', 'massa folhada', etc.\n"
    "- NAO comece com 'O', 'A', 'Um', 'Uma' nem com o nome do produto.\n"
    "- NAO termine com ponto de exclamacao.\n"
    "- NAO use emoji.\n"
    "- Devolva SOMENTE o texto da descricao, sem aspas, sem prefixos.\n"
)


def _chamar_claude(prompt):
    """Faz a chamada e devolve string limpa, ou None se falhar.
    Best-effort — caller mostra erro pro admin mas nunca quebra a tela."""
    api_key = os.environ.get('ANTHROPIC_API_KEY')
    if not api_key:
        return None
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        resp = client.messages.create(
            model=MODELO,
            max_tokens=400,
            messages=[{'role': 'user', 'content': prompt}],
        )
        from app.services import uso_ia
        uso_ia.registrar('seo', MODELO, getattr(resp, 'usage', None))
        # resp.content e' lista de blocks; pega o texto.
        partes = [b.text for b in resp.content
                  if getattr(b, 'type', '') == 'text']
        texto = ' '.join(p.strip() for p in partes).strip()
        # Defesa: remove aspas que a IA insiste em colocar as vezes.
        if texto.startswith(('"', "'")) and texto.endswith(('"', "'")):
            texto = texto[1:-1].strip()
        # Trunca em 280 (margem sobre o limite de 220 pra erros).
        return texto[:280] if texto else None
    except Exception:  # noqa: BLE001
        logger.exception('seo_descricoes._chamar_claude falhou')
        return None


def sugerir_para_receita(receita):
    """Gera sugestao de descricao SEO pra uma receita. Usa nome, categoria
    e os 5 ingredientes principais (>= 1% da receita). Devolve string ou
    None se a API falhar/nao estiver configurada."""
    ings = []
    for ing in (receita.ingredientes or []):
        try:
            pct = float(ing.porcentagem or 0)
        except (TypeError, ValueError):
            pct = 0
        if pct < 1.0:
            continue
        nome = (ing.ingrediente_nome or '').strip()
        if nome:
            ings.append((pct, nome))
    ings.sort(reverse=True)
    nomes = ', '.join(n for _p, n in ings[:5]) or '(sem ingredientes cadastrados)'

    prompt = (
        f"{_INSTRUCOES_BASE}\n"
        f"Produto: {receita.nome}\n"
        f"Categoria: {receita.categoria or 'panificacao'}\n"
        f"Ingredientes principais: {nomes}\n"
    )
    return _chamar_claude(prompt)


def sugerir_para_produto(produto):
    """Gera sugestao de descricao SEO pra um produto (cesta, kit). Usa
    nome, categoria e os itens da cesta (se houver). Devolve string ou
    None se a API falhar/nao estiver configurada."""
    itens_str = ''
    if produto.itens:
        nomes = []
        for it in produto.itens[:8]:
            try:
                nomes.append(it.nome_resolvido)
            except Exception:  # noqa: BLE001
                continue
        if nomes:
            itens_str = f"Composicao: {', '.join(nomes)}\n"

    prompt = (
        f"{_INSTRUCOES_BASE}\n"
        f"Produto: {produto.nome}\n"
        f"Categoria: {produto.categoria or 'cestas e kits'}\n"
        f"{itens_str}"
    )
    return _chamar_claude(prompt)


def disponivel():
    """True se a API key esta configurada (pra a tela admin avisar)."""
    return bool(os.environ.get('ANTHROPIC_API_KEY'))
