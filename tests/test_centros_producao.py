from app.models import Receita
from app.services.centros_producao import (
    CENTRO_PAES,
    CENTRO_VIENNOISERIE,
    centro_trabalho_receita,
)


def test_brioche_fica_com_padeiro_de_paes_mesmo_com_familia_legada(app):
    brioche = Receita(
        nome='Brioche',
        categoria='Pães',
        familia='viennoiserie',
    )

    assert centro_trabalho_receita(brioche) == CENTRO_PAES


def test_categoria_viennoiserie_vai_para_equipe_propria(app):
    croissant = Receita(
        nome='Croissant',
        categoria='Viennoiserie',
        familia='viennoiserie',
    )

    assert centro_trabalho_receita(croissant) == CENTRO_VIENNOISERIE
