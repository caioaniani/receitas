"""Disponibilidade POR DATA no bot e no vigia (04/08/2026).

Caso real (conversa 1134, Giovana, véspera do Dia dos Pais): o plano-do-dia
de 09/08 só vendia cestas; o bot disse CERTO que croissant/cinnamon/pão
francês não estavam disponíveis pra 09/08, e o vigia — que só enxergava a
disponibilidade GERAL do catálogo — acusou "erro real de informação" (falso
ALTA). Estas travas garantem que a verdade POR DATA do plano-do-dia chega
ao bot (`consultar_produtos.indisponivel_em`) e ao vigia (linha
"INDISPONIVEL para entrega em: DD/MM" + regra no prompt).
"""
from datetime import timedelta
from unittest.mock import patch

from app.extensions import db
from app.utils import hoje


def _receita_publicada(nome='Croissant de Nutella'):
    from app.models import Receita
    r = Receita(nome=nome, categoria='Paes', preco_site=18.0,
                imagem_dropbox_url='https://x/f.jpg',
                rendimento_qtd=1, rendimento_unidade='un', peso_base=90.0)
    db.session.add(r)
    db.session.commit()
    return r


def test_saldos_no_periodo_agrupa_por_data(app):
    from app.services import loja_plano_dia
    d1, d2 = hoje(), hoje() + timedelta(days=5)
    loja_plano_dia.definir('receita', 1, d1, 3)
    loja_plano_dia.definir('receita', 1, d2, 0)
    out = loja_plano_dia.saldos_no_periodo(d1, d2)
    assert out[d1][('receita', 1)] == 3
    assert out[d2][('receita', 1)] == 0
    # Fora do intervalo não entra
    loja_plano_dia.definir('receita', 1, d2 + timedelta(days=1), 9)
    out = loja_plano_dia.saldos_no_periodo(d1, d2)
    assert d2 + timedelta(days=1) not in out


def test_catalogo_disponibilidade_lista_datas_zeradas(app):
    """DISPONÍVEL no geral + plano zerado pra uma data futura => a data
    aparece em `indisponivel_em` (era exatamente o cenário do falso ALTA)."""
    from app.services import bot_tools, loja_plano_dia
    r = _receita_publicada()
    alvo = hoje() + timedelta(days=5)
    loja_plano_dia.definir('receita', r.id, alvo, 0)
    loja_plano_dia.definir('receita', r.id, alvo + timedelta(days=1), 10)
    cat = bot_tools.catalogo_disponibilidade()
    item = next(c for c in cat if c['nome'] == 'Croissant de Nutella')
    assert item['disponivel'] is True          # geral segue disponível
    assert item['indisponivel_em'] == [alvo.strftime('%d/%m')]


def test_sob_encomenda_tambem_fica_indisponivel_por_data(app):
    """CONTRATO NOVO 07/08/2026 (decisão do dono, caso Caixa de Mini no Dia
    dos Pais — SUBSTITUI o "nunca fica indisponível" de 04/08): o plano-do-
    dia zerado vale pra sob encomenda também, e o bot/vigia enxergam a data
    curada em `indisponivel_em`."""
    from app.services import bot_tools, loja_plano_dia
    r = _receita_publicada('Mini Pain Encomenda')
    r.sob_encomenda = True
    db.session.commit()
    alvo = hoje() + timedelta(days=5)
    loja_plano_dia.definir('receita', r.id, alvo, 0)
    cat = bot_tools.catalogo_disponibilidade()
    item = next(c for c in cat if c['nome'] == 'Mini Pain Encomenda')
    assert item['indisponivel_em'] == [alvo.strftime('%d/%m')]


def test_consultar_produtos_expoe_indisponivel_em(app):
    """O bot precisa NEGAR por data com base em dado, não em eco do
    cliente — o match focado traz as datas zeradas do plano."""
    from app.services import bot_tools, loja_plano_dia
    r = _receita_publicada()
    alvo = hoje() + timedelta(days=5)
    loja_plano_dia.definir('receita', r.id, alvo, 0)
    out = bot_tools.consultar_produtos('croissant nutella')
    p = next(x for x in out['produtos'] if x['nome'] == 'Croissant de Nutella')
    assert p['indisponivel_em'] == [alvo.strftime('%d/%m')]
    # Sem plano zerado, o campo nem aparece (token-light)
    loja_plano_dia.definir('receita', r.id, alvo, 10)
    out = bot_tools.consultar_produtos('croissant nutella')
    p = next(x for x in out['produtos'] if x['nome'] == 'Croissant de Nutella')
    assert 'indisponivel_em' not in p


def test_resumo_do_vigia_mostra_as_datas(app):
    from app.services import chatbot_vigia
    catalogo = [
        {'nome': 'Croissant de Nutella', 'disponivel': True,
         'indisponivel_em': ['09/08']},
        {'nome': 'Cesta Dia dos Pais', 'disponivel': True,
         'indisponivel_em': []},
    ]
    with patch('app.services.bot_tools.catalogo_disponibilidade',
               return_value=catalogo):
        with app.app_context():
            resumo = chatbot_vigia._resumo_catalogo_site()
    assert ('Croissant de Nutella: DISPONIVEL — '
            'INDISPONIVEL para entrega em: 09/08') in resumo
    assert 'Cesta Dia dos Pais: DISPONIVEL' in resumo
    assert 'Cesta Dia dos Pais: DISPONIVEL —' not in resumo


def test_resumo_dedup_homonimo_intersecta_datas(app):
    """Receita e produto homônimos: a data só fica indisponível se TODAS as
    entradas do nome estiverem — uma entrada livre libera o nome."""
    from app.services import chatbot_vigia
    catalogo = [
        {'nome': 'Pao Frances', 'disponivel': True,
         'indisponivel_em': ['09/08', '10/08']},
        {'nome': 'Pao Frances', 'disponivel': False,
         'indisponivel_em': ['09/08']},
    ]
    with patch('app.services.bot_tools.catalogo_disponibilidade',
               return_value=catalogo):
        with app.app_context():
            resumo = chatbot_vigia._resumo_catalogo_site()
    assert 'Pao Frances: DISPONIVEL — INDISPONIVEL para entrega em: 09/08' \
        in resumo
    assert '10/08' not in resumo


def test_prompt_do_vigia_tem_a_regra_por_data(app):
    from app.services import chatbot_vigia
    assert 'INDISPONIVEL para entrega em' in chatbot_vigia.PROMPT_VIGIA
    assert 'POR DATA' in chatbot_vigia.PROMPT_VIGIA


def test_catalogo_sem_plano_segue_sem_datas(app):
    """Regressão: sem nenhuma linha de plano, nada muda no contrato."""
    from app.services import bot_tools
    _receita_publicada('Baguete Simples')
    cat = bot_tools.catalogo_disponibilidade()
    item = next(c for c in cat if c['nome'] == 'Baguete Simples')
    assert item == {'nome': 'Baguete Simples', 'disponivel': True,
                    'indisponivel_em': []}
