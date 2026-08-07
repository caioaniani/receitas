"""Datas com horario de entrega DIFERENTE do normal (27/07/2026).

Pedido do dono: no Dia dos Pais (09/08/2026) o site so pode oferecer UMA
janela de entrega, das 06:00 as 10:00. Escolha dele (AskUserQuestion,
27/07/2026): **tela pra ele mesmo cadastrar**, em vez de a data ficar cravada
no codigo — assim Natal e Dia das Maes se resolvem sem deploy.

O QUE UMA DATA ESPECIAL FAZ (as tres pontas, decididas pelo dono):
- SUBSTITUI as janelas normais (08:00-18:00 de 1h) pelas cadastradas;
- vale pra ENTREGA AGENDADA **e** pra RETIRADA na loja (a restricao e das
  duas pontas — "no Dia dos Pais so tem a leva da manha");
- BLOQUEIA o express naquele dia quando `express_bloqueado` (default),
  senao "so uma janela" seria mentira: o cliente pediria entrega imediata as
  15h e alguem teria que sair pra rua fora da leva unica.

Lista de janelas VAZIA = **dia fechado** (o dia some do calendario). E um
estado legitimo (Natal), e por isso NAO ha fallback pras janelas normais
quando esta vazia — cair no normal transformaria "fechado" em "aberto o dia
inteiro", o pior erro possivel aqui.

FONTE UNICA: quem consulta isto e o `loja_checkout` (janelas_disponiveis /
express_disponivel / datas_disponiveis). Todo mundo que ja pergunta janela
pra ele — checkout do site, validacao do POST, tela de divulgacao — herda de
graca. NUNCA reimplemente a regra em outro lugar.
"""
import logging
import re
from datetime import date as _date_type

from app.extensions import db
from app.models import LojaDataEspecial

logger = logging.getLogger(__name__)

# EN-DASH. Tem que ser IDENTICO ao separador de `loja_checkout.JANELAS_HORARIAS`
# — o codigo compara janela por STRING, entao um hifen no lugar do en-dash faz
# a janela cadastrada nunca casar com a escolhida (a mesma armadilha ja
# documentada em JANELAS_CORTADAS_LONGE). Por isso `normalizar_janela` aceita
# o que o dono digitar e converte.
TRACO = '–'
_RE_JANELA = re.compile(r'^\s*(\d{1,2})\s*:\s*(\d{2})\s*[-–—]\s*'
                        r'(\d{1,2})\s*:\s*(\d{2})\s*$')
MAX_JANELAS = 24


class JanelaInvalida(ValueError):
    """Texto de janela que o dono digitou e nao da pra interpretar."""


def normalizar_janela(texto):
    """'6:00 - 10:00' → '06:00–10:00'. Levanta `JanelaInvalida` no que nao
    der pra ler.

    Aceita hifen, en-dash, em-dash e espacos soltos DE PROPOSITO: o dono
    digita no teclado do celular, onde o en-dash nem existe. Guardar o que
    ele digitou sem normalizar criaria uma janela que a tela mostra mas o
    checkout recusa — bug mudo e sem pista."""
    m = _RE_JANELA.match(str(texto or ''))
    if not m:
        raise JanelaInvalida(
            f'"{texto}" não parece um horário. Use o formato 06:00-10:00.')
    h1, m1, h2, m2 = (int(g) for g in m.groups())
    for h, mi in ((h1, m1), (h2, m2)):
        if h > 23 or mi > 59:
            raise JanelaInvalida(f'"{texto}" tem hora inválida.')
    if (h1, m1) >= (h2, m2):
        raise JanelaInvalida(
            f'"{texto}": o fim tem que ser depois do começo.')
    return f'{h1:02d}:{m1:02d}{TRACO}{h2:02d}:{m2:02d}'


def normalizar_lista(texto_ou_lista):
    """Texto do textarea (uma janela por linha) → lista normalizada, sem
    repetidas, em ordem de horario. Levanta `JanelaInvalida` na primeira
    linha ruim — cadastro pela metade em horario de entrega e pior que
    recusar (o dono corrige e reenvia)."""
    if isinstance(texto_ou_lista, str):
        linhas = texto_ou_lista.splitlines()
    else:
        linhas = list(texto_ou_lista or [])
    out = []
    for ln in linhas:
        if not str(ln).strip():
            continue
        j = normalizar_janela(ln)
        if j not in out:
            out.append(j)
    if len(out) > MAX_JANELAS:
        raise JanelaInvalida(f'No máximo {MAX_JANELAS} janelas por dia.')
    return sorted(out)


def _cache():
    """Cache por request (`flask.g`): `janelas_disponiveis` e chamado uma vez
    por DATA do calendario (14+ datas por render do checkout) e sem isso
    seriam 14 SELECTs por tela. Fora de contexto Flask (thread do bot, cron)
    devolve None e cada consulta vai ao banco — correto, so nao cacheado."""
    try:
        from flask import g, has_app_context
        if not has_app_context():
            return None
        if not hasattr(g, '_datas_especiais_cache'):
            g._datas_especiais_cache = {}
        return g._datas_especiais_cache
    except Exception:  # noqa: BLE001
        return None


def regra_do_dia(data):
    """A `LojaDataEspecial` da data, ou None se o dia é normal.

    Best-effort: qualquer erro de banco devolve None (= dia normal). Um
    problema na consulta NUNCA pode derrubar o checkout — o pior caso vira
    "o site ofereceu o horário de sempre", e não "ninguém consegue comprar".
    """
    if isinstance(data, str):
        try:
            data = _date_type.fromisoformat(data)
        except ValueError:
            return None
    if not isinstance(data, _date_type):
        return None
    cache = _cache()
    if cache is not None and data in cache:
        return cache[data]
    try:
        regra = LojaDataEspecial.query.filter_by(data=data).first()
    except Exception:  # noqa: BLE001
        logger.exception('data especial: consulta falhou (%s)', data)
        # ROLLBACK obrigatório: no Postgres um statement que falha deixa a
        # transação ABORTADA e toda query seguinte da mesma request morre —
        # o "pior caso vira o horário de sempre" prometido acima só é
        # verdade com isto (lição já registrada duas vezes no CLAUDE.md).
        try:
            db.session.rollback()
        except Exception:  # noqa: BLE001
            pass
        regra = None
    if cache is not None:
        cache[data] = regra
    return regra


def regras_do_periodo(datas):
    """`{date: regra}` das datas que TÊM regra, em UMA query.

    `_sem_dias_fechados` e o payload do checkout perguntam por ~15 datas a
    cada render; data a data isso vira 15 SELECTs (e 15 `logger.exception`
    quando o banco está intermitente). Popula o cache de request de quebra."""
    datas = [d for d in (datas or []) if isinstance(d, _date_type)]
    if not datas:
        return {}
    cache = _cache()
    try:
        linhas = (LojaDataEspecial.query
                  .filter(LojaDataEspecial.data.in_(datas)).all())
    except Exception:  # noqa: BLE001
        logger.exception('data especial: consulta do período falhou')
        try:
            db.session.rollback()
        except Exception:  # noqa: BLE001
            pass
        return {}
    achadas = {r.data: r for r in linhas}
    if cache is not None:
        for d in datas:
            cache[d] = achadas.get(d)
    return achadas


def janelas_do_dia(data):
    """`(tem_regra, janelas)`.

    - `(False, [])` — dia normal: o chamador usa a lista de sempre.
    - `(True, ['06:00–10:00'])` — dia especial: usa EXATAMENTE essas.
    - `(True, [])` — dia FECHADO: nenhuma janela, e nada de fallback.

    A tupla existe pra `[]` nunca ser confundido com "não há regra" — é a
    diferença entre fechar a loja e abri-la o dia inteiro.
    """
    regra = regra_do_dia(data)
    if regra is None:
        return False, []
    return True, regra.lista_janelas()


def express_bloqueado_em(data):
    """True se o express está bloqueado nesse dia. Dia sem cadastro = False
    (express segue a regra normal de horário).

    DIA FECHADO bloqueia SEMPRE, mesmo com a caixa desmarcada: o contrato
    (modelo e manual) promete que a data "some do site e ninguém consegue
    comprar pra ela", e o express é um canal de venda que não olha a lista
    de janelas — sem esta linha, um dia fechado com a caixa desmarcada
    continuaria vendendo (achado de revisão 27/07/2026)."""
    regra = regra_do_dia(data)
    if regra is None:
        return False
    return bool(regra.express_bloqueado or regra.fechado)


def dia_fechado(data):
    """True se a data está cadastrada SEM nenhuma janela (loja fechada)."""
    tem, janelas = janelas_do_dia(data)
    return tem and not janelas


def _limpar_cache():
    cache = _cache()
    if cache is not None:
        cache.clear()


def listar(desde=None):
    """Datas especiais cadastradas, mais próxima primeiro. `desde` filtra o
    passado (a tela mostra só o que ainda vale, por padrão)."""
    q = LojaDataEspecial.query
    if desde is not None:
        q = q.filter(LojaDataEspecial.data >= desde)
    return q.order_by(LojaDataEspecial.data).all()


def pedidos_fora_do_horario(regras):
    """`{data_iso: [(codigo, janela), ...]}` dos pedidos JA PAGOS marcados
    pra essas datas com horario que a regra nova NAO oferece mais.

    A agenda do site e de 14 dias: quando a regra e cadastrada (ou o seed
    roda no deploy), pode JA existir venda pra aquele dia com a janela
    antiga — cadastrar nao migra nem avisa ninguem, e a operacao so
    descobriria no dia, no painel de entregas. Read-only e best-effort:
    falha aqui nunca pode derrubar a tela."""
    try:
        from app.models import PedidoOnline
        alvos = {r.data: set(r.lista_janelas()) for r in (regras or [])}
        if not alvos:
            return {}
        pedidos = (PedidoOnline.query
                   .filter(PedidoOnline.data_entrega.in_(list(alvos)),
                           PedidoOnline.status.notin_(
                               ['cancelado', 'aguardando_pagamento']))
                   .all())
        out = {}
        for p in pedidos:
            ok = alvos.get(p.data_entrega) or set()
            if (p.janela_entrega or '') in ok:
                continue
            out.setdefault(p.data_entrega.isoformat(), []).append(
                (p.codigo, p.janela_entrega or '—'))
        return out
    except Exception:  # noqa: BLE001 — aviso e bonus, nao pode quebrar a tela
        logger.exception('data especial: contagem de pedidos falhou')
        return {}


def definir(data, janelas, *, express_bloqueado=True, rotulo=None,
            usuario_id=None, bloquear_itens=None):
    """Cria ou atualiza a regra da data (upsert). Devolve a linha.

    `janelas` aceita texto do textarea ou lista; passa por
    `normalizar_lista`, então horário torto levanta `JanelaInvalida` e NADA
    é gravado. `bloquear_itens`: texto (uma regra por linha — categoria ou
    nome de item); None = NÃO mexe no que está gravado (compat com
    chamadores antigos, ex. o seed do 09/08); '' = limpa de propósito."""
    if isinstance(data, str):
        data = _date_type.fromisoformat(data)
    janelas_norm = normalizar_lista(janelas)
    regra = LojaDataEspecial.query.filter_by(data=data).first()
    if regra is None:
        regra = LojaDataEspecial(data=data, criado_por_id=usuario_id)
        db.session.add(regra)
    regra.janelas = '\n'.join(janelas_norm)
    regra.express_bloqueado = bool(express_bloqueado)
    regra.rotulo = (rotulo or '').strip()[:80] or None
    if bloquear_itens is not None:
        linhas = [ln.strip() for ln in str(bloquear_itens).splitlines()
                  if ln.strip()]
        regra.bloquear_itens = '\n'.join(linhas) or None
    db.session.commit()
    _limpar_cache()
    return regra


def _norm_regra(s):
    """Normaliza pra comparação: sem acento, caixa baixa, espaços únicos.
    'Mini Pães' == 'mini paes' — o dono digita no celular, sem acento."""
    import unicodedata
    s = unicodedata.normalize('NFKD', str(s or ''))
    s = ''.join(c for c in s if not unicodedata.combining(c))
    return ' '.join(s.casefold().split())


def itens_bloqueados(data, itens):
    """Nomes dos itens do carrinho BARRADOS pra entrega nesta data.

    Cada linha de `bloquear_itens` casa (sem acento/caixa) com a CATEGORIA
    do item no catálogo ou com o NOME do item. `itens` = lista de dicts do
    `loja_checkout.montar_itens` (kind/id/nome). Dia sem regra ou sem
    bloqueio = []. Best-effort: erro de consulta devolve [] — bloquear é
    curadoria, e um problema aqui NUNCA pode derrubar o checkout (mesmo
    contrato do `regra_do_dia`; o fail-open é deliberado)."""
    try:
        regra = regra_do_dia(data)
        if regra is None:
            return []
        regras = {_norm_regra(ln) for ln in regra.lista_bloqueios()}
        if not regras:
            return []
        from app.models import Produto, Receita
        barrados = []
        for it in itens:
            nome = it.get('nome') or ''
            alvo = None
            if it.get('kind') == 'receita':
                alvo = Receita.query.get(it.get('id'))
            elif it.get('kind') == 'produto':
                alvo = Produto.query.get(it.get('id'))
            categoria = getattr(alvo, 'categoria', None)
            if (_norm_regra(nome) in regras
                    or (categoria and _norm_regra(categoria) in regras)):
                barrados.append(nome)
        return barrados
    except Exception:  # noqa: BLE001 — fail-open documentado acima
        logger.exception('data especial: bloqueio de itens falhou (%s)', data)
        try:
            db.session.rollback()
        except Exception:  # noqa: BLE001
            pass
        return []


def remover(data):
    """Apaga a regra (o dia volta ao horário normal). True se havia algo."""
    if isinstance(data, str):
        data = _date_type.fromisoformat(data)
    regra = LojaDataEspecial.query.filter_by(data=data).first()
    if regra is None:
        return False
    db.session.delete(regra)
    db.session.commit()
    _limpar_cache()
    return True
