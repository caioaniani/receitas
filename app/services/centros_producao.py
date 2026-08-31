"""Centros humanos independentes da producao.

O forno e a amassadeira continuam compartilhados, mas a padaria tem uma
pessoa dedicada aos paes e outra a viennoiserie.  O nivelamento semanal e o
Gantt usam esta classificacao para uma fila nao ocupar a capacidade humana da
outra.
"""

import unicodedata

CENTRO_PAES = 'paes'
CENTRO_VIENNOISERIE = 'viennoiserie'

ROTULOS_CENTRO = {
    CENTRO_PAES: 'Padeiro de pães',
    CENTRO_VIENNOISERIE: 'Padeiro de viennoiserie',
}


def _normalizar(valor):
    texto = unicodedata.normalize('NFKD', str(valor or ''))
    return ''.join(c for c in texto if not unicodedata.combining(c)).strip().lower()


_PREPAROS_AUXILIARES = (
    'granola',
    'iogurte',
    'levain',
    'massa ',
    'massa de ',
    'massa para ',
    'base ',
    'creme ',
    'recheio ',
    'calda ',
    'molho ',
)


def eh_preparo_auxiliar(receita):
    """Se a receita e um insumo/preparo, e nao uma unidade de pao pronta."""
    nome = _normalizar(getattr(receita, 'nome', None))
    categoria = _normalizar(getattr(receita, 'categoria', None))
    if categoria in ('granola', 'iogurte'):
        return True
    return any(token in nome for token in _PREPAROS_AUXILIARES)


def eh_sourdough_final(receita):
    """Se conta nas 200 unidades diarias de sourdough pronto.

    Usa a familia de dominio quando preenchida e cobre os nomes legados que
    ainda nao receberam familia. Preparos auxiliares sao excluidos primeiro:
    ``familia`` nula tem default historico de sourdough e, sem esta guarda,
    granola/iogurte/levain seriam contados como pao.
    """
    if receita is None or eh_preparo_auxiliar(receita):
        return False
    nome = _normalizar(getattr(receita, 'nome', None))
    categoria = _normalizar(getattr(receita, 'categoria', None))
    familia = _normalizar(getattr(receita, 'familia', None))
    if familia == 'pao_sourdough':
        return True
    if categoria != CENTRO_PAES:
        return False
    return ('sourdough' in nome
            or 'pao frances fermentado' in nome)


def centro_trabalho_receita(receita):
    """Devolve o centro humano que executa ``receita``.

    A categoria operacional tem precedencia sobre ``familia``: Brioche esta
    cadastrado em Pães e pertence ao padeiro de pães, mesmo que alguma ficha
    legada tenha familia inconsistente.  Sub-receitas de croissant/danish ja
    usam categoria Viennoiserie.  O legado sem classificacao cai em Pães, que
    preserva o comportamento operacional anterior sem criar uma terceira
    equipe inexistente.
    """
    categoria = _normalizar(getattr(receita, 'categoria', None))
    familia = _normalizar(getattr(receita, 'familia', None))
    if categoria == CENTRO_VIENNOISERIE:
        return CENTRO_VIENNOISERIE
    if categoria in ('paes', 'fornadas especiais'):
        return CENTRO_PAES
    if familia == CENTRO_VIENNOISERIE:
        return CENTRO_VIENNOISERIE
    return CENTRO_PAES


def rotulo_centro(centro):
    return ROTULOS_CENTRO.get(centro, ROTULOS_CENTRO[CENTRO_PAES])
