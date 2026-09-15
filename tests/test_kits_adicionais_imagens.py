"""Miniaturas usam a capa da vitrine sem novas consultas por adicional."""
import json
import re

import pytest
from sqlalchemy import event
from test_kits_adicionais import extra as extra
from test_kits_checkout import BASE, _abrir
from test_kits_checkout import ambiente as ambiente
from test_kits_checkout import kit as kit

from app.extensions import db
from app.models import CatalogoFoto, Produto, Receita
from app.services import kits_adicionais, loja_leitura

pytestmark = pytest.mark.loja_host
FOTOS = 'https://imagens.example.com/'


@pytest.fixture(autouse=True)
def calendario(congela_hoje):
    congela_hoje(2026, 9, 14, 6)


@pytest.mark.parametrize('kind', ['receita', 'produto'])
@pytest.mark.parametrize('dropbox,legada,esperada', [
    ('capa-atual.jpg', 'capa-legada.jpg', 'capa-atual.jpg'),
    (None, 'capa-legada.jpg', 'capa-legada.jpg'),
    (None, None, ''),
])
def test_oferta_publica_preserva_precedencia_da_capa(app, kit, extra, kind, dropbox, legada, esperada):
    alvo = db.session.get(Receita, kit.itens[0].receita_id) if kind == 'receita' else extra
    alvo.imagem_dropbox_url = FOTOS + dropbox if dropbox else None
    alvo.imagem_url = FOTOS + legada if legada else None
    # A galeria é separada da capa da vitrine, inclusive quando esta falta.
    db.session.add(CatalogoFoto(kind=kind, item_id=alvo.id,
                               dropbox_url=FOTOS + 'foto-extra.jpg', ordem=1))
    db.session.commit()
    _, _, html = _abrir(app, kit)
    config = json.loads(re.search(r'<script id="kits-config"[^>]*>(.*?)</script>', html, re.S)[1])
    oferta = next(it for it in config['adicionais'] if it['kind'] == kind and it['id'] == alvo.id)
    assert oferta['imagem'] == (FOTOS + esperada if esperada else '')
    assert oferta['nome'] == alvo.nome
    assert oferta['precoCentavos'] == int(alvo.preco_site * 100)


def test_catalogo_de_miniaturas_nao_consulta_galerias_blobs_ou_item_por_item(app):
    produtos = [Produto(nome=f'Adicional com foto {indice}', ativo=True, site_ativo=True,
                         preco_site=10, imagem_url=f'{FOTOS}{indice}.jpg',
                         imagem_blob=b'foto-legada-binaria') for indice in range(20)]
    db.session.add_all(produtos)
    db.session.flush()
    esperadas = {('produto', produto.id): produto.imagem_url for produto in produtos}
    db.session.add_all([CatalogoFoto(kind='produto', item_id=produto.id, ordem=1,
                                    dropbox_url=FOTOS + 'foto-extra.jpg') for produto in produtos])
    db.session.commit()
    selects = []

    def registrar(_conn, _cursor, statement, _params, _context, _executemany):
        if statement.lstrip().upper().startswith('SELECT'):
            selects.append(statement.lower())

    event.listen(db.engine, 'before_cursor_execute', registrar)
    try:
        with loja_leitura.catalogo_em_lote(base=BASE):
            consultas_preparo = len(selects)
            ofertas = kits_adicionais.catalogo(base=BASE)
            assert len(selects) == consultas_preparo
    finally:
        event.remove(db.engine, 'before_cursor_execute', registrar)
    assert {(it['kind'], it['id']): it['imagem'] for it in ofertas} == esperadas
    assert not any('catalogo_foto' in sql for sql in selects)
    assert not any('imagem_blob' in sql for sql in selects)
