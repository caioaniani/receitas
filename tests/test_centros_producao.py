from app.models import Receita
from app.services.centros_producao import (
    CENTRO_PAES,
    CENTRO_VIENNOISERIE,
    centro_trabalho_receita,
    eh_preparo_auxiliar,
    eh_sourdough_final,
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


def test_somente_pao_sourdough_pronto_entra_no_piso_diario(app):
    sourdough = Receita(
        nome='Sourdough Tradicional', categoria='Pães',
        familia='pao_sourdough')
    frances = Receita(
        nome='Pão Francês Fermentado', categoria='Pães')
    brioche = Receita(
        nome='Brioche', categoria='Pães', familia='viennoiserie')

    assert eh_sourdough_final(sourdough) is True
    assert eh_sourdough_final(frances) is True
    assert eh_sourdough_final(brioche) is False


def test_preparos_auxiliares_nao_contam_como_pao_pronto(app):
    preparos = (
        Receita(nome='Granola Artesanal', categoria='Granola'),
        Receita(nome='Levain', categoria='Pães'),
        Receita(nome='Iogurte Natural', categoria='Iogurte'),
        Receita(nome='Base Sourdough', categoria='Pães'),
        Receita(nome='Creme de Amêndoas', categoria='Pães'),
    )

    assert all(eh_preparo_auxiliar(r) for r in preparos)
    assert not any(eh_sourdough_final(r) for r in preparos)
