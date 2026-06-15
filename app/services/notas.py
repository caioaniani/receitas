"""Memoria persistente do agente — busca + registro de notas.

Camada usada pelo copilot (tools `consultar_notas` e `registrar_nota`) e
pelo bot Padeiro (so `consultar_notas`). Pagina admin em /notas mexe nos
mesmos dados via CRUD.

Decisao 15/06/2026: busca por keyword agora, embeddings semanticos depois
quando tiver volume (>200 notas). Tem normalizacao simples (lowercase,
sem acento) pra empatar "anesio" com "Anésio". Ranking simples:
  - titulo bate o termo: 5
  - tag bate o termo: 3
  - conteudo bate o termo: 1
Notas ARQUIVADAS nao aparecem nas buscas dos agentes.
"""
import logging
import re
import unicodedata

from app.extensions import db
from app.utils import agora

logger = logging.getLogger(__name__)

# Limite de notas devolvidas por busca. Acima disso, vira ruido no contexto
# do LLM (ja gastamos token a cada turn). Se um termo retorna 200 notas, eh
# sinal que a tag/categoria precisa virar mais especifica.
MAX_RESULTADOS = 10
# Caracteres minimos pra busca rodar. Evita "a", "o", "e" devolverem tudo.
MIN_TERMO = 2


def _normalizar(texto):
    """Lowercase, sem acento, sem pontuacao — pra empatar termos."""
    if not texto:
        return ''
    t = unicodedata.normalize('NFKD', texto)
    t = ''.join(c for c in t if not unicodedata.combining(c))
    t = t.lower()
    t = re.sub(r'[^a-z0-9\s,]+', ' ', t)
    return re.sub(r'\s+', ' ', t).strip()


def _tokens(texto):
    """Quebra a busca em tokens (>= MIN_TERMO chars). Stopwords pequenas
    saem pelo MIN_TERMO. Mantemos numeros (ex: "5" pra '5 pedacos')."""
    return [t for t in _normalizar(texto).split() if len(t) >= MIN_TERMO]


def _score(nota, tokens):
    """Score = soma de (peso x ocorrencias). Se NENHUM token bate, vira 0
    e a nota sai do resultado."""
    titulo_n = _normalizar(nota.titulo)
    conteudo_n = _normalizar(nota.conteudo)
    tags_n = _normalizar(nota.tags)
    score = 0
    for tok in tokens:
        if tok in titulo_n:
            score += 5
        if tok in tags_n:
            score += 3
        if tok in conteudo_n:
            score += 1
    return score


def buscar(termo, limite=MAX_RESULTADOS):
    """Devolve lista de Notas ativas que batem com o termo, ordenadas por
    score desc (titulo > tag > conteudo). Termo curto/vazio devolve as
    notas MAIS RECENTES (catch-all)."""
    from app.models import Nota

    tokens = _tokens(termo)
    # Catch-all: sem termo, devolve as mais recentes (util quando o
    # copilot abre a conversa e quer "o que andou anotando ultimamente").
    if not tokens:
        return (Nota.query.filter(Nota.arquivada_em.is_(None))
                .order_by(Nota.criada_em.desc()).limit(limite).all())

    # Pre-filtro SQL: pega so candidatas que tenham AO MENOS UM token em
    # qualquer dos campos textuais. Reduz o ranking em memoria a um
    # subconjunto razoavel.
    from sqlalchemy import or_
    clausulas = []
    for tok in tokens:
        like = f'%{tok}%'
        clausulas.append(Nota.titulo.ilike(like))
        clausulas.append(Nota.tags.ilike(like))
        clausulas.append(Nota.conteudo.ilike(like))
    candidatas = (Nota.query
                  .filter(Nota.arquivada_em.is_(None))
                  .filter(or_(*clausulas))
                  .limit(limite * 4).all())
    ranked = [(n, _score(n, tokens)) for n in candidatas]
    ranked = [(n, s) for n, s in ranked if s > 0]
    ranked.sort(key=lambda x: (-x[1], -x[0].criada_em.timestamp()))
    return [n for n, _s in ranked[:limite]]


def registrar(titulo, conteudo, *, tags=None, origem='admin',
              criada_por_id=None):
    """Cria uma nota nova. Tags vem como list[str] ou string CSV — normalizo
    pra CSV lowercased sem acento (pra busca empatar). Devolve a Nota
    criada (commitada)."""
    from app.models import Nota
    if not titulo or not conteudo:
        raise ValueError('titulo e conteudo sao obrigatorios')
    tags_csv = _normalizar_tags(tags)
    n = Nota(
        titulo=titulo.strip()[:200],
        conteudo=conteudo.strip(),
        tags=tags_csv,
        origem=origem,
        criada_por_id=criada_por_id,
    )
    db.session.add(n)
    db.session.commit()
    logger.info('nota registrada id=%s origem=%s titulo=%r',
                n.id, origem, n.titulo[:60])
    return n


def atualizar(nota_id, *, titulo=None, conteudo=None, tags=None):
    """Edita uma nota existente. Campos None nao mudam. Bumpa
    `atualizada_em`. Devolve a Nota ou None se nao achou."""
    from app.models import Nota
    n = db.session.get(Nota, nota_id)
    if not n:
        return None
    if titulo is not None:
        n.titulo = titulo.strip()[:200]
    if conteudo is not None:
        n.conteudo = conteudo.strip()
    if tags is not None:
        n.tags = _normalizar_tags(tags)
    n.atualizada_em = agora()
    db.session.commit()
    return n


def arquivar(nota_id):
    """Soft delete — nota some das buscas dos agentes, mas continua no
    historico. Admin pode restaurar."""
    from app.models import Nota
    n = db.session.get(Nota, nota_id)
    if not n:
        return None
    n.arquivada_em = agora()
    db.session.commit()
    return n


def restaurar(nota_id):
    from app.models import Nota
    n = db.session.get(Nota, nota_id)
    if not n:
        return None
    n.arquivada_em = None
    db.session.commit()
    return n


def _normalizar_tags(tags):
    """list[str] ou 'a, b, c' -> 'a,b,c' (lowercased, sem acento)."""
    if tags is None:
        return ''
    if isinstance(tags, str):
        tags = tags.split(',')
    saida = []
    for t in tags:
        t = _normalizar(str(t))
        if t and t not in saida:
            saida.append(t)
    return ','.join(saida)


def serializar_pro_agente(notas):
    """Formato compacto que o LLM consome bem (markdown). Inclui id pra
    o copilot poder pedir 'apaga a #42' depois."""
    if not notas:
        return ''
    blocos = []
    for n in notas:
        cabecalho = f'### #{n.id} {n.titulo}'
        if n.tags:
            cabecalho += f'  _[{n.tags}]_'
        blocos.append(f'{cabecalho}\n{n.conteudo}')
    return '\n\n'.join(blocos)
