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
