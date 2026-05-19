"""Mapeamento one-shot de imagens do Rappi pra produtos/receitas.

Extraido dos cardapios estaticos enviados pelo admin. Roda 1 vez via
rota admin `/admin/popular-imagens-cardapio` (ou shell). Faz match
fuzzy do nome com Receita/Produto e seta `imagem_url` apenas onde tiver
nulo (nao sobrescreve customizacoes manuais).
"""
from rapidfuzz import process, fuzz

from app.extensions import db
from app.models import Receita, Produto


# Catalogo nome → URL extraido dos anexos. Pra agregar fonte unica de
# verdade; depois admin pode editar a URL por receita/produto na ficha.
IMAGENS = {
    # Pães
    'Sourdough Tradicional': 'https://images.rappi.com.br/products/307439b4-f336-41e7-8bb0-1c0621c7eeef.png',
    'Sourdough Integral': 'https://images.rappi.com.br/products/2b9b0261-2e44-433e-b8c8-f27a6750ef50.png',
    'Sourdough De Grãos': 'https://images.rappi.com.br/products/fdc55629-0f25-4a2a-9d13-c894de089e38.png',
    'Sourdough 7 Grãos': 'https://images.rappi.com.br/products/fdc55629-0f25-4a2a-9d13-c894de089e38.png',
    'Sourdough Azeitona Com Nozes': 'https://images.rappi.com.br/products/2c85f3e1-3de2-4b80-8efb-369fdbf24005.png',
    'Sourdough Nozes e Azeitona': 'https://images.rappi.com.br/products/2c85f3e1-3de2-4b80-8efb-369fdbf24005.png',
    'Brioche': 'https://images.rappi.com.br/products/ae4bbd03-33bf-46f5-958a-5f1e7354cf9b.png',
    'Pão Francês': 'https://images.rappi.com.br/products/402ec1f1-12f0-46b1-b8c8-67f3fd314c12.png',
    'Pão Francês Fermentado Naturalmente': 'https://images.rappi.com.br/products/402ec1f1-12f0-46b1-b8c8-67f3fd314c12.png',
    'Cookie Callebaut': 'https://images.rappi.com.br/products/cb843953-be6d-44db-abb5-a6bd4cf3d69f.png',

    # Viennoiserie
    'Pain Au Chocolat': 'https://images.rappi.com.br/products/64bc6b41-c137-4c6f-b769-ff351656dca0.png',
    'Croissant Almond': 'https://images.rappi.com.br/products/db90fd4f-7ca5-4274-bd86-81a4f355bc4d.png',
    'Croissant Nutella': 'https://images.rappi.com.br/products/91d09cdf-ffb9-465e-8f6f-b459af417a6c.png',
    'Croissant Francês': 'https://images.rappi.com.br/products/b4ee5ad2-aa1c-4228-b4f2-ce88cd81b31c.png',
    'Croissant Tradicional': 'https://images.rappi.com.br/products/b4ee5ad2-aa1c-4228-b4f2-ce88cd81b31c.png',
    'Croissant Nutella Com Morango': 'https://images.rappi.com.br/products/790ff523-ba18-44da-93e0-f4f347d4bd3b.png',

    # Lanches & chapa
    'Queijo Quente No Pão Francês': 'https://images.rappi.com.br/products/738d03a2-bca6-415f-92c8-06824db0c69b.png',
    'Misto No Pão Francês': 'https://images.rappi.com.br/products/277946c7-b18d-4d36-965e-ebda4f3aa8bb.png',
    'Peito De Peru Queijo Branco Pão Francês': 'https://images.rappi.com.br/products/9cba4254-691c-4ef5-bb59-b00e9e60b0d6.png',
    'Sourdough Nozes Azeitonas Mant Requeijão': 'https://images.rappi.com.br/products/5d7e78ee-ae3f-4af4-850e-7ef61b31e6ba.png',
    'Croissant Na Chapa Com Manteiga': 'https://images.rappi.com.br/products/eb0f604b-e11a-4b9f-ba41-1d68a4f9ccf9.png',
    'Croissant Chapa Com Manteiga E Requeijão': 'https://images.rappi.com.br/products/4365b01d-5be3-486e-abb1-55af4118227f.png',
    'Pão Francês Manteiga E Requeijão': 'https://images.rappi.com.br/products/1aa9d245-cd48-49f7-a0a7-dfb8929d60c8.png',
    'Pão Francês Com Manteiga': 'https://images.rappi.com.br/products/8a729bd3-b32d-45c6-95de-c70aa3e86857.png',
    'Sourdough Integral Manteiga E Requeijão': 'https://images.rappi.com.br/products/3595b816-0652-419b-a27c-14e2b243b8a1.png',
    'Sourdough Integral Na Chapa Com Manteiga': 'https://images.rappi.com.br/products/85ba3d42-8c41-4d60-9f19-aa1b0f4e8ffe.png',
    'Sourdough Tradicional Chapa Com Manteiga': 'https://images.rappi.com.br/products/3131b010-59a3-487f-9633-f5e5206a34ab.png',
    'Sourdough Tradicional Manteiga Requeijão': 'https://images.rappi.com.br/products/4528fb52-4e86-4c2a-8c01-1a07b28924cc.png',
    'Brioche Com Manteiga (3 Fatias)': 'https://images.rappi.com.br/products/f85b4088-94b9-4efc-96e0-46106de45be1.png',
    'Brioche Na Chapa Manteiga E Requeijão': 'https://images.rappi.com.br/products/b91d0a79-3c87-4a45-af8d-51d4cac70cb7.png',
    'Queijo Quente No Brioche': 'https://images.rappi.com.br/products/81d47bc5-ee5b-4ccd-9a93-f5e4fc50d053.png',
    'Misto No Brioche': 'https://images.rappi.com.br/products/823cd83c-24b3-4863-a666-97c8a13fa85b.png',
    'Peito De Peru Queijo Branco No Brioche': 'https://images.rappi.com.br/products/9e5589bc-becc-44c6-8775-a094219f1e52.png',
    'Misto No Croissant': 'https://images.rappi.com.br/products/88aaaa57-9f8e-4dac-ba44-c1f622fbfa65.png',
    'Queijo Quente No Croissant': 'https://images.rappi.com.br/products/5c914263-6ba1-43c8-9d09-66ce8bcf7d20.png',
    'Queijo Branco No Croissant': 'https://images.rappi.com.br/products/f5917040-5bef-4cb9-8e57-e2904a2cda42.png',
    'Peito De Peru Queijo Branco No Croissant': 'https://images.rappi.com.br/products/7f1b33e1-32f9-4e52-b8df-42494cee9369.png',
    'Misto No Sourdough Tradicional': 'https://images.rappi.com.br/products/275f191e-7a25-4944-a673-cb75ad45ecbb.png',
    'Queijo Quente Sourdough Tradicional': 'https://images.rappi.com.br/products/f96e5002-04f2-4705-a063-f5a75b1ea311.png',
    'Misto No Sourdough Integral': 'https://images.rappi.com.br/products/6aab71a6-571c-479b-bc74-4ce22388a0ad.png',
    'Queijo Quente Sourdough Integral': 'https://images.rappi.com.br/products/901fd960-550f-4b1b-aa3a-85aef73abee0.png',
    'Misto No Sourdough De Grãos': 'https://images.rappi.com.br/products/a42a7460-2b4a-42fe-b372-a8d12029c226.png',
    'Misto No Sourdough De Azeitona E Nozes': 'https://images.rappi.com.br/products/7475db56-22aa-42d2-adba-1ed4cd1194c2.png',
    'Queijo Quente Sourdough Azeitona Nozes': 'https://images.rappi.com.br/products/1d8523db-e28c-4cc4-a114-efaee8c97ae7.png',
    'Queijo Quente Sourdough De Grãos': 'https://images.rappi.com.br/products/98312ccf-31cf-4249-9fcd-9d654d4dff89.png',
    'Sourdough Nozes Azeitona Chapa Manteiga': 'https://images.rappi.com.br/products/b277f514-0d88-4ff3-a858-60c07ed6136f.png',
    'Queijo Branco No Sourdough Integral': 'https://images.rappi.com.br/products/a0d1a70d-2133-4d0f-b079-d2a6b29ea799.png',
    'Queijo Branco No Sourdough Tradicional': 'https://images.rappi.com.br/products/86a37f96-c8a3-48a2-a243-139162b1969a.png',
    'Queijo Branco No Sourdough De Grãos': 'https://images.rappi.com.br/products/2aef6aeb-8ad4-45d1-aa91-fda00156c02f.png',
    'Queijo Branco No Pão Francês': 'https://images.rappi.com.br/products/20e13970-d219-45be-9bbf-f7861bf46d6a.png',
    'Queijo Branco No Brioche': 'https://images.rappi.com.br/products/2ad739a5-a35c-4ae9-9d0e-31474a934e5f.png',
    'Sourdough 7 Grãos Manteiga E Requeijão': 'https://images.rappi.com.br/products/03ce3dff-b9df-48b8-a7fa-d43b49ea4982.png',
    'Sourdough 7 Grãos Na Chapa Com Manteiga': 'https://images.rappi.com.br/products/10699524-33d9-430a-81e9-bf535f837a35.png',

    # Diversos / bowls
    'Granola 500 G': 'https://images.rappi.com.br/products/1c5814f6-ede5-4be1-95d9-7116024b8a66.png',
    'Granola Artesanal 100 Gramas': 'https://images.rappi.com.br/products/852481eb-dfb3-4c56-afb9-93dc74aa3c5e.png',
    'Granola': 'https://images.rappi.com.br/products/852481eb-dfb3-4c56-afb9-93dc74aa3c5e.png',
    'Iogurte Natural 200 Ml': 'https://images.rappi.com.br/products/1feaafb2-c6c0-42b3-b2de-32b2316c51b2.png',
    'Iogurte Natural 600 Ml': 'https://images.rappi.com.br/products/1bec7b05-fab4-4e8e-9d37-dd0740e8f8a6.png',
    'Mel 40 G': 'https://images.rappi.com.br/products/8a3542c6-763a-4af3-ace6-5284f13ebebb.png',
    'Salada De Frutas 200 Ml': 'https://images.rappi.com.br/products/15c2ee7f-8e38-4864-ae8f-9831a6e25a67.png',
    'Salada De Frutas': 'https://images.rappi.com.br/products/15c2ee7f-8e38-4864-ae8f-9831a6e25a67.png',
    'Geleia De Morango 40 G': 'https://images.rappi.com.br/products/7cefecea-e384-4c0a-9cc4-c11f02676f26.png',

    # Bebidas
    'Suco Verde 300ml': 'https://images.rappi.com.br/products/17bd12a0-c24b-4044-9891-f24cc4e14efe.png',
    'Suco Verde': 'https://images.rappi.com.br/products/17bd12a0-c24b-4044-9891-f24cc4e14efe.png',
    'Suco De Laranja De 1lt': 'https://images.rappi.com.br/products/c123501e-aa49-43b5-9fc1-d9dcd640fa74.png',
    'Suco De Laranja Natural': 'https://images.rappi.com.br/products/4667b9de-0a18-4320-8bca-41398dce005a.png',
    'Suco De Laranja 300 Ml': 'https://images.rappi.com.br/products/4667b9de-0a18-4320-8bca-41398dce005a.png',
    'Suco De Abacaxi Com Hortelã': 'https://images.rappi.com.br/products/9d7ddadf-965a-4cd5-bd71-7acf90c6fed6.png',
    'Chocolate Do Padre Batido Gelado': 'https://images.rappi.com.br/products/d93deebe-b441-4396-b252-e27eb7943a99.png',
    'Refrigerante Guaraná Antarctica 350ml': 'https://images.rappi.com.br/products/de842bfe-dd84-4d73-901b-e4d5c26f3a88.png',
    'Toddy Batido Gelado 300ml': 'https://images.rappi.com.br/products/77aaae79-3469-4dbd-bda2-58b9ba32ef44.png',
    'Água Mineral Sem Gás Prata 310ml': 'https://images.rappi.com.br/products/fd08d756-49d5-498f-96de-ccee6c6e65b6.png',
    'Coca-cola Lata 350ml': 'https://images.rappi.com.br/products/389a2d2d-de9a-486d-8a8f-880fb48362f1.png',
    'Coca Cola Zero Lata 350ml': 'https://images.rappi.com.br/products/34362ac7-04b2-478a-a62e-45924fbdfb58.png',
    'Suco Tangerina Villa Piva 300ml': 'https://images.rappi.com.br/products/7925bd06-12f7-4ed4-8da5-5c2fb69d9e58.png',
    'Suco De Açaí 300 Ml': 'https://images.rappi.com.br/products/f30f955c-31ff-45b2-89fd-eac475d01b8e.png',
    'Suco De Açaí': 'https://images.rappi.com.br/products/f30f955c-31ff-45b2-89fd-eac475d01b8e.png',
    'Suco De Uva Integral Villa Piva 300ml': 'https://images.rappi.com.br/products/9d473425-303e-4d44-853b-d2df6e3f3cac.png',
    'Suco De Açai Com Laranja 300ml': 'https://images.rappi.com.br/products/78aa5e46-c955-4b44-aa7b-bbc37431571d.png',
    'Suco De Açaí Com Laranja': 'https://images.rappi.com.br/products/78aa5e46-c955-4b44-aa7b-bbc37431571d.png',
    'Água Com Gás São Lourenço 300ml': 'https://images.rappi.com.br/products/a67a8ad7-3ced-43a7-a65f-98c2a53a01a4.png',
}


def _achar_match(nome, threshold=70):
    """Retorna (url, score, nome_referencia) ou None."""
    if not nome:
        return None
    nomes_referencia = list(IMAGENS.keys())
    nomes_norm = [_norm(n) for n in nomes_referencia]
    n = _norm(nome)
    # 1. exato
    if n in nomes_norm:
        ref = nomes_referencia[nomes_norm.index(n)]
        return (IMAGENS[ref], 100, ref)
    # 2. fuzzy token_set_ratio (mais permissivo com ordem de palavras)
    match = process.extractOne(n, nomes_norm, scorer=fuzz.token_set_ratio)
    if match and match[1] >= threshold:
        ref = nomes_referencia[match[2]]
        return (IMAGENS[ref], int(match[1]), ref)
    return None


def preview_matches(threshold=70):
    """Retorna [{tipo, id, nome, score, url, ref, ja_tem}] pra admin
    revisar antes de aplicar. Lista ordenada por score decrescente.
    `ja_tem` = True se imagem_url ja esta setada (nao sobrescreve).
    """
    out = []
    for r in Receita.query.order_by(Receita.nome).all():
        m = _achar_match(r.nome, threshold=threshold)
        if m:
            url, score, ref = m
            out.append({
                'tipo': 'receita', 'id': r.id, 'nome': r.nome,
                'score': score, 'url': url, 'ref': ref,
                'ja_tem': bool(r.imagem_url),
                'imagem_atual': r.imagem_url,
            })
        else:
            out.append({
                'tipo': 'receita', 'id': r.id, 'nome': r.nome,
                'score': 0, 'url': None, 'ref': None,
                'ja_tem': bool(r.imagem_url),
                'imagem_atual': r.imagem_url,
            })
    for p in Produto.query.filter_by(ativo=True).order_by(Produto.nome).all():
        m = _achar_match(p.nome, threshold=threshold)
        if m:
            url, score, ref = m
            out.append({
                'tipo': 'produto', 'id': p.id, 'nome': p.nome,
                'score': score, 'url': url, 'ref': ref,
                'ja_tem': bool(p.imagem_url),
                'imagem_atual': p.imagem_url,
            })
        else:
            out.append({
                'tipo': 'produto', 'id': p.id, 'nome': p.nome,
                'score': 0, 'url': None, 'ref': None,
                'ja_tem': bool(p.imagem_url),
                'imagem_atual': p.imagem_url,
            })
    # Com match primeiro, depois ordenado por score desc
    out.sort(key=lambda x: (-(x['score'] or 0), x['nome']))
    return out


def popular_imagens(sobrescrever=False, threshold=70, ids_aprovados=None):
    """Aplica IMAGENS no banco. Match por nome ascii-lowercase, fallback
    rapidfuzz token_set_ratio (score >= threshold).

    sobrescrever=False: so seta onde imagem_url eh nulo.
    ids_aprovados: dict {('receita', id) | ('produto', id) → True} pra
                   aplicar so subset selecionado no preview. None = todos.
    Retorna {receitas_alteradas, produtos_alterados, sem_match}.
    """
    receitas_alt = 0
    sem_match_r = []
    for r in Receita.query.all():
        if r.imagem_url and not sobrescrever:
            continue
        if ids_aprovados is not None and ('receita', r.id) not in ids_aprovados:
            continue
        m = _achar_match(r.nome, threshold=threshold)
        if m:
            r.imagem_url = m[0]
            receitas_alt += 1
        else:
            sem_match_r.append(r.nome)

    produtos_alt = 0
    sem_match_p = []
    for p in Produto.query.all():
        if p.imagem_url and not sobrescrever:
            continue
        if ids_aprovados is not None and ('produto', p.id) not in ids_aprovados:
            continue
        m = _achar_match(p.nome, threshold=threshold)
        if m:
            p.imagem_url = m[0]
            produtos_alt += 1
        else:
            sem_match_p.append(p.nome)

    db.session.commit()
    return {
        'receitas_alteradas': receitas_alt,
        'produtos_alterados': produtos_alt,
        'receitas_sem_match': sem_match_r,
        'produtos_sem_match': sem_match_p,
    }


def _norm(s):
    import unicodedata
    if not s:
        return ''
    nfd = unicodedata.normalize('NFD', s)
    return ''.join(c for c in nfd if unicodedata.category(c) != 'Mn').lower().strip()
