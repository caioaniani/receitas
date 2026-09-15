"""Contrato da página pública entre escolhas, preço, agenda e compra."""
import json
import re
from itertools import product

import pytest
from test_kits_checkout import _abrir, _post
from test_kits_checkout import ambiente as ambiente
from test_kits_opcoes_cliente import _chave
from test_kits_opcoes_cliente import cenario as cenario
from test_kits_opcoes_cliente import plano as plano

from app.extensions import db
from app.models import CompraKit
from app.services import kits_cafe

pytestmark = pytest.mark.loja_host


def _config(html):
    return json.loads(re.search(r'<script id="kits-config"[^>]*>(.*?)</script>', html, re.S)[1])


def test_todas_combinacoes_da_pagina_usam_precos_do_servidor(app, plano):
    kit, laranja, verde, almond, nutella, tradicional, integral, _ = plano
    _, _, html = _abrir(app, kit)
    cfg = _config(html)
    assert cfg['grupos'] == ['croissant', 'sourdough']
    assert len(cfg['combinacoes']) == 8
    for nome in ['suco_id', 'escolha_croissant', 'escolha_sourdough']:
        seletor = re.search(rf'<select name="{nome}".*?</select>', html, re.S)[0]
        assert 'required' in seletor and 'selected' not in seletor
    for suco, croissant, pao in product([laranja, verde], [almond, nutella], [tradicional, integral]):
        escolhas = {'croissant': _chave(croissant), 'sourdough': _chave(pao)}
        itens, erros = kits_cafe.montar(kit, suco.id, escolhas)
        assert not erros
        chave = '|'.join([str(suco.id), *escolhas.values()])
        assert cfg['combinacoes'][chave]['precoCentavos'] == int(sum(i['subtotal'] for i in itens) * 100)
        combinacao = cfg['combinacoes'][chave]
        assert set(combinacao) == {'precoCentavos', 'leadDias'}
        assert cfg['calendarios'][str(combinacao['leadDias'])]['janelas']


def test_erro_de_formulario_preserva_opcoes_sem_criar_compra(app, plano):
    kit, _, verde, _, nutella, _, integral, _ = plano
    cliente, token, _ = _abrir(app, kit)
    resposta = _post(cliente, kit, token, suco_id=str(verde.id),
                     escolha_croissant=_chave(nutella), escolha_sourdough=_chave(integral), cpf='invalido')
    assert resposta.status_code == 400
    html = resposta.get_data(as_text=True)
    for escolha in [_chave(nutella), _chave(integral)]:
        assert f'value="{escolha}" selected' in html
    assert CompraKit.query.count() == 0


def test_calendario_combina_antecedencia_de_croissant_e_pao(app, plano):
    kit, laranja, _, almond, nutella, tradicional, _, _ = plano
    nutella.sob_encomenda = True
    db.session.commit()
    _, _, html = _abrir(app, kit)
    cfg = _config(html)
    normal = cfg['combinacoes']['|'.join([str(laranja.id), _chave(almond), _chave(tradicional)])]
    encomenda = cfg['combinacoes']['|'.join([str(laranja.id), _chave(nutella), _chave(tradicional)])]
    assert cfg['calendarios'][str(encomenda['leadDias'])]['dataMin'] > (
        cfg['calendarios'][str(normal['leadDias'])]['dataMin'])
