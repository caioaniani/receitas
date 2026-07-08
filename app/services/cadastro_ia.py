"""Cadastro assistido por IA (08/07/2026, pedido do dono).

O dono cola um print/lista de itens novos (ex: cardapio "MISTO CRANBERRY
R$ 30,00"); a IA le nome + preco e PROPOE o cadastro de Produtos usando os
produtos PARECIDOS ja cadastrados como referencia de composicao (ex: um
"MISTO CRANBERRY" herda a estrutura do "MISTO" trocando o pao pelo de
cranberry). Componente que nao existe no catalogo vira proposta de MP nova
(item comprado pronto). Componente que a IA sugerir como RECEITA nova NAO
e criado automaticamente — receita e ficha de producao, vira item orfao
com aviso (vincula em /produtos/cestas/orfaos depois de criar a ficha).

NADA e salvo pela IA: a tela /produtos/cadastro-ia mostra a proposta
editavel e so o POST de salvar grava — humano SEMPRE revisa, porque
componente errado = baixa de estoque errada no motor de vendas.

Modelo: Sonnet 4.6 (padronizacao do dono 25/06/2026), override via env
CADASTRO_IA_MODELO. Custo registrado em UsoIA (funcao='cadastro_ia').
"""

import base64
import json
import logging
import os
import re

from app.extensions import db
from app.models import MateriaPrima, Produto, ProdutoItem, Receita

logger = logging.getLogger(__name__)

MODELO = os.environ.get('CADASTRO_IA_MODELO', 'claude-sonnet-4-6')

# Campos de preco do Produto que a tela pode escolher como destino do
# valor lido (whitelist — nunca aceitar nome de coluna vindo do form).
CAMPOS_PRECO = ('preco_site', 'preco_atacado', 'preco_loja')

# Limites do contexto mandado a IA (evita estourar tokens em catalogos
# grandes). Quando cortar, o resultado carrega um aviso — nunca truncar
# em silencio.
_MAX_PRODUTOS_CTX = 250
_MAX_RECEITAS_CTX = 400
_MAX_MPS_CTX = 400

SYSTEM_PROMPT = """Voce cadastra produtos de uma padaria artesanal.

Recebera uma LISTA DE ITENS NOVOS (imagem de cardapio/planilha ou texto
colado, com nome e preco em reais) e o CATALOGO ATUAL (produtos ja
cadastrados com suas composicoes, receitas e materias-primas).

Para cada item novo, proponha o cadastro:
1. Leia nome e preco (precos em formato brasileiro: "R$ 32,00" = 32.00).
2. Procure produtos PARECIDOS no catalogo e use a composicao deles como
   referencia (ex: "MISTO CRANBERRY" segue a estrutura do "MISTO" ja
   cadastrado, trocando o pao pelo pao de cranberry).
3. Cada componente deve referenciar um item EXISTENTE do catalogo (use o
   id fornecido). Se um componente necessario nao existir em lugar
   nenhum, proponha-o como novo com "novo": true (tipo "mp" para item
   comprado pronto; tipo "receita" apenas se claramente e algo produzido
   na padaria — receitas novas NAO sao criadas automaticamente).
4. Se o item novo JA EXISTE no catalogo (mesmo nome), marque
   "ja_existe_id" com o id dele.

Responda APENAS um JSON valido (sem markdown, sem explicacao):
{"itens": [
  {"nome": "NOME DO ITEM", "preco": 32.0, "categoria": "Lanches",
   "ja_existe_id": null,
   "baseado_em": "nome do produto parecido usado como referencia ou null",
   "confianca": "alta|media|baixa",
   "observacao": "duvida ou decisao que o humano deve conferir, ou null",
   "componentes": [
     {"tipo": "receita|produto|mp", "id": 123, "nome": "Pao de Forma",
      "quantidade": 2, "novo": false},
     {"tipo": "mp", "id": null, "nome": "Queijo mussarela fatiado",
      "quantidade": 2, "novo": true, "unidade": "un"}
   ]}
]}

Regras:
- quantidade e por UNIDADE vendida do item (ex: 2 fatias de pao por misto).
- Nao invente id: so use ids que estao no catalogo fornecido.
- Categoria: reuse as existentes quando fizer sentido.
- Na duvida sobre a composicao, proponha mesmo assim com "confianca":
  "baixa" e explique em "observacao" — o humano revisa tudo."""


def _contexto_catalogo():
    """Snapshot do catalogo pra IA raciocinar por analogia. Devolve
    (dict pro prompt, lista de avisos de truncamento)."""
    avisos = []
    produtos = (Produto.query.filter_by(ativo=True)
                .order_by(Produto.nome).all())
    if len(produtos) > _MAX_PRODUTOS_CTX:
        avisos.append(f'catalogo grande: só os {_MAX_PRODUTOS_CTX} '
                      f'primeiros produtos (de {len(produtos)}) foram '
                      'mostrados à IA')
        produtos = produtos[:_MAX_PRODUTOS_CTX]
    prods_ctx = []
    for p in produtos:
        d = {'id': p.id, 'nome': p.nome, 'categoria': p.categoria}
        comps = [{'tipo': pi.tipo, 'nome': pi.nome_resolvido,
                  'quantidade': pi.quantidade}
                 for pi in (p.itens or [])]
        if comps:
            d['componentes'] = comps
        prods_ctx.append(d)

    receitas = Receita.ativas().order_by(Receita.nome).all()
    if len(receitas) > _MAX_RECEITAS_CTX:
        avisos.append(f'só as {_MAX_RECEITAS_CTX} primeiras receitas '
                      f'(de {len(receitas)}) foram mostradas à IA')
        receitas = receitas[:_MAX_RECEITAS_CTX]
    mps = MateriaPrima.ativas().order_by(MateriaPrima.nome).all()
    if len(mps) > _MAX_MPS_CTX:
        avisos.append(f'só as {_MAX_MPS_CTX} primeiras MPs '
                      f'(de {len(mps)}) foram mostradas à IA')
        mps = mps[:_MAX_MPS_CTX]

    return {
        'produtos': prods_ctx,
        'receitas': [{'id': r.id, 'nome': r.nome, 'categoria': r.categoria}
                     for r in receitas],
        'materias_primas': [{'id': m.id, 'nome': m.nome,
                             'unidade': m.unidade} for m in mps],
    }, avisos


def _bloco_imagem(file_bytes, mimetype):
    b64 = base64.b64encode(file_bytes).decode('ascii')
    mt = mimetype if (mimetype or '').startswith('image/') else 'image/jpeg'
    return {'type': 'image',
            'source': {'type': 'base64', 'media_type': mt, 'data': b64}}


def _resolver_por_nome(tipo, nome):
    """Match exato case-insensitive por tipo. Devolve o objeto ou None."""
    if not nome:
        return None
    alvo = nome.strip().lower()
    if tipo == 'receita':
        q = Receita.ativas()
        col = Receita.nome
    elif tipo == 'produto':
        q = Produto.query.filter_by(ativo=True)
        col = Produto.nome
    elif tipo == 'mp':
        q = MateriaPrima.ativas()
        col = MateriaPrima.nome
    else:
        return None
    return q.filter(db.func.lower(col) == alvo).first()


def _get_por_id(tipo, item_id):
    """Busca por id RESPEITANDO o contrato de "conectar algo novo": MP/
    receita arquivada e produto inativo NAO valem como alvo (mesma regra
    do resolver por nome, que usa .ativas())."""
    modelo = {'receita': Receita, 'produto': Produto,
              'mp': MateriaPrima}.get(tipo)
    if not modelo or not item_id:
        return None
    alvo = db.session.get(modelo, item_id)
    if alvo is None:
        return None
    if tipo == 'produto' and not alvo.ativo:
        return None
    if tipo in ('receita', 'mp') and getattr(alvo, 'arquivada_em', None):
        return None
    return alvo


def _sanitizar_proposta(dados):
    """Valida/normaliza o JSON da IA contra o banco REAL: id que nao
    existe (ou de nome divergente) e re-resolvido por nome; sem match e
    sem 'novo' vira componente orfao sinalizado. A IA so propoe — quem
    manda e o cadastro."""
    itens_ok = []
    for it in (dados.get('itens') or []):
        nome = (it.get('nome') or '').strip()
        if not nome:
            continue
        try:
            preco = float(it.get('preco') or 0)
        except (TypeError, ValueError):
            preco = 0.0
        existente = _resolver_por_nome('produto', nome)
        comps = []
        for c in (it.get('componentes') or []):
            tipo = c.get('tipo')
            if tipo not in ('receita', 'produto', 'mp'):
                continue
            try:
                qtd = float(c.get('quantidade') or 1)
            except (TypeError, ValueError):
                qtd = 1.0
            from app.utils import normalizar_busca
            c_nome = (c.get('nome') or '').strip()
            alvo = _get_por_id(tipo, c.get('id'))
            # id so vale se o nome bate (normalizado: acento/caixa nao
            # derrubam um match legitimo) — divergencia real re-resolve
            # por nome; o banco manda, a IA so propoe.
            if alvo is None or (c_nome and normalizar_busca(alvo.nome)
                                != normalizar_busca(c_nome)):
                alvo = _resolver_por_nome(tipo, c_nome)
            # 'novo' so vale pra MP: receita/produto novos NAO sao criados
            # automaticamente (receita e ficha de producao) — viram orfaos.
            novo = bool(c.get('novo')) and alvo is None and tipo == 'mp'
            comps.append({
                'tipo': tipo,
                'id': alvo.id if alvo else None,
                'nome': alvo.nome if alvo else c_nome,
                'quantidade': qtd,
                'novo': novo,
                'unidade': (c.get('unidade') or 'un') if novo else None,
                # sem match e sem MP nova: vira orfao no salvar (mesmo
                # destino da tela de composicao — resolve em /cestas/orfaos)
                'orfao': alvo is None and not novo,
            })
        itens_ok.append({
            'nome': nome,
            'preco': preco,
            'categoria': (it.get('categoria') or '').strip() or None,
            'ja_existe_id': existente.id if existente else None,
            'baseado_em': (it.get('baseado_em') or None),
            'confianca': it.get('confianca') or 'media',
            'observacao': (it.get('observacao') or None),
            'componentes': comps,
        })
    return itens_ok


def analisar(*, file_bytes=None, mimetype=None, texto=None):
    """Roda a IA sobre a imagem/texto + catalogo e devolve
    {'itens': [proposta sanitizada], 'avisos': [...], 'modelo_usado': ...}
    ou {'erro': '...'}. NAO grava nada."""
    api_key = os.environ.get('ANTHROPIC_API_KEY')
    if not api_key:
        return {'erro': 'ANTHROPIC_API_KEY nao configurada'}
    try:
        import anthropic
    except ImportError:
        return {'erro': 'biblioteca anthropic nao instalada'}
    if not file_bytes and not (texto or '').strip():
        return {'erro': 'mande uma imagem ou cole o texto da lista'}

    contexto, avisos = _contexto_catalogo()
    content = []
    if file_bytes:
        content.append(_bloco_imagem(file_bytes, mimetype))
    instrucao = ('CATALOGO ATUAL:\n' + json.dumps(contexto, ensure_ascii=False)
                 + '\n\nITENS NOVOS A CADASTRAR')
    if (texto or '').strip():
        instrucao += ' (texto colado):\n' + texto.strip()
    else:
        instrucao += ': estao na imagem acima.'
    content.append({'type': 'text', 'text': instrucao})

    client = anthropic.Anthropic(api_key=api_key, timeout=120,
                                 max_retries=1)
    try:
        response = client.messages.create(
            model=MODELO, max_tokens=4000, system=SYSTEM_PROMPT,
            messages=[{'role': 'user', 'content': content}])
        from app.services import uso_ia
        uso_ia.registrar('cadastro_ia', MODELO,
                         getattr(response, 'usage', None))
        bruto = ''.join(b.text for b in response.content
                        if getattr(b, 'type', '') == 'text')
        bruto = re.sub(r'^```(?:json)?\s*|\s*```$', '', bruto.strip(),
                       flags=re.MULTILINE)
        dados = json.loads(bruto)
    except json.JSONDecodeError:
        logger.warning('cadastro_ia: resposta nao-JSON do modelo')
        return {'erro': 'a IA devolveu uma resposta invalida — tente de novo'}
    except Exception as exc:
        logger.warning('cadastro_ia: falha na chamada: %s', exc)
        return {'erro': f'falha na IA: {exc}'}

    itens = _sanitizar_proposta(dados)
    if not itens:
        return {'erro': 'a IA nao encontrou itens na lista enviada'}
    return {'itens': itens, 'avisos': avisos, 'modelo_usado': MODELO}


def salvar_lote(itens, campo_preco, user=None):
    """Grava as propostas REVISADAS pelo humano. `itens` = lista no formato
    de `_sanitizar_proposta` (apos edicao na tela). `campo_preco` diz em
    qual campo do Produto o preco lido entra (whitelist CAMPOS_PRECO).

    - Produto ja existente (nome exato) e PULADO — esta tela so cria.
    - Componente novo tipo 'mp' cria a MateriaPrima (custo_por_kg=0 com
      aviso: definir o custo real no Banco de MPs, senao o custo do
      produto sai subestimado).
    - Componente sem vinculo vira ProdutoItem orfao (mesmo destino da tela
      de composicao; resolve em /produtos/cestas/orfaos).

    Devolve {'criados': [...], 'pulados': [...], 'mps_criadas': [...],
    'avisos': [...]} — commit unico no fim.
    """
    if campo_preco not in CAMPOS_PRECO:
        raise ValueError(f'campo de preco invalido: {campo_preco}')
    criados, pulados, mps_criadas, avisos = [], [], [], []
    mps_novas = {}          # nome.lower() -> MateriaPrima (dedupe no lote)
    for it in itens:
        nome = (it.get('nome') or '').strip()
        if not nome:
            continue
        if _resolver_por_nome('produto', nome):
            pulados.append(nome)
            continue
        prod = Produto(nome=nome,
                       categoria=(it.get('categoria') or '').strip() or None,
                       ativo=True)
        try:
            preco = float(it.get('preco') or 0)
        except (TypeError, ValueError):
            preco = 0.0
        if preco > 0:
            setattr(prod, campo_preco, preco)
        db.session.add(prod)
        db.session.flush()
        for c in (it.get('componentes') or []):
            tipo = c.get('tipo')
            if tipo not in ('receita', 'produto', 'mp'):
                continue
            try:
                qtd = float(c.get('quantidade') or 1)
            except (TypeError, ValueError):
                qtd = 1.0
            if qtd <= 0:
                continue
            c_nome = (c.get('nome') or '').strip()
            alvo = _get_por_id(tipo, c.get('id'))
            if alvo is None:
                alvo = _resolver_por_nome(tipo, c_nome)
            if alvo is None and tipo == 'mp' and c.get('novo') and c_nome:
                alvo = mps_novas.get(c_nome.lower())
                if alvo is None:
                    alvo = MateriaPrima(
                        nome=c_nome,
                        unidade=(c.get('unidade') or 'un'),
                        custo_por_kg=0)
                    db.session.add(alvo)
                    db.session.flush()
                    mps_novas[c_nome.lower()] = alvo
                    mps_criadas.append(c_nome)
                    avisos.append(f'MP "{c_nome}" criada com custo 0 — '
                                  'defina o custo real no Banco de MPs')
            if alvo is None:
                avisos.append(f'{nome}: componente "{c_nome}" sem vínculo '
                              '— resolva em Produtos → cestas órfãos')
            db.session.add(ProdutoItem(
                produto_id=prod.id,
                tipo=tipo,
                receita_id=alvo.id if (alvo and tipo == 'receita') else None,
                produto_componente_id=(alvo.id if (alvo and tipo == 'produto')
                                       else None),
                materia_prima_id=alvo.id if (alvo and tipo == 'mp') else None,
                # FK manda; item_nome espelha o alvo pra nao orfanar num
                # save futuro da tela de composicao (convencao do projeto)
                item_nome=alvo.nome if alvo else c_nome,
                quantidade=qtd,
            ))
        criados.append(nome)
    db.session.commit()
    return {'criados': criados, 'pulados': pulados,
            'mps_criadas': mps_criadas, 'avisos': avisos}
