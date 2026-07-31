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
        regra = None
    if cache is not None:
        cache[data] = regra
    return regra


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
    (express segue a regra normal de horário)."""
    regra = regra_do_dia(data)
    return bool(regra is not None and regra.express_bloqueado)


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


def definir(data, janelas, *, express_bloqueado=True, rotulo=None,
            usuario_id=None):
    """Cria ou atualiza a regra da data (upsert). Devolve a linha.

    `janelas` aceita texto do textarea ou lista; passa por
    `normalizar_lista`, então horário torto levanta `JanelaInvalida` e NADA
    é gravado."""
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
    db.session.commit()
    _limpar_cache()
    return regra


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
