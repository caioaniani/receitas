"""Migracao para o motor unico: backfill VendaMapa + migracao de fracoes."""
from app.extensions import db
from app.models import (
    DebitoEstoque,
    EstoqueLoja,
    LojaDebito,
    LojaProdutoMap,
    Receita,
    SeruDebito,
    SeruProdutoMap,
)
from app.services.venda_mapa_migracao import backfill_venda_mapa, migrar_fracoes_para_debito_estoque


def _receita(nome):
    r = Receita(nome=nome, categoria='Paes', rendimento_qtd=1,
                rendimento_unidade='un', peso_base=100.0)
    db.session.add(r)
    db.session.flush()
    return r


def test_backfill_copia_e_e_idempotente(app, loja):
    from app.models import VendaMapa
    cookie = _receita('Cookie')
    db.session.add(SeruProdutoMap(seru_nome='CAFE', receita_id=cookie.id,
                                  fator_quantidade=0.2))
    db.session.add(LojaProdutoMap(nome_digitado='Pao Frances',
                                  receita_id=cookie.id, fator_quantidade=1.0))
    db.session.commit()

    r1 = backfill_venda_mapa()
    assert r1 == {'seru_novos': 1, 'lote_novos': 1, 'uso_novos': 0}
    seru = VendaMapa.query.filter_by(canal='seru', nome_externo='CAFE').first()
    assert seru.receita_id == cookie.id and abs(seru.fator_quantidade - 0.2) < 1e-9
    assert VendaMapa.query.filter_by(canal='lote',
                                     nome_externo='Pao Frances').first()
    # idempotente: roda de novo, nao duplica
    r2 = backfill_venda_mapa()
    assert r2 == {'seru_novos': 0, 'lote_novos': 0, 'uso_novos': 0}
    assert VendaMapa.query.count() == 2


def test_backfill_nao_reverte_conciliacao_existente(app, loja):
    """REGRESSAO: conciliacao feita na UI (VendaMapa) NAO pode ser revertida
    pelo backfill do startup a partir do SeruProdutoMap congelado.

    Bug do "volta pra ignorado em seguida": o produto estava ignorado no mapa
    velho; o admin vincula na tela de itens-vendidos (grava so no VendaMapa);
    no proximo deploy o backfill reaplicava o snapshot velho por cima."""
    from app.models import VendaMapa
    cookie = _receita('Cookie')
    # Snapshot velho CONGELADO: OVOS AO PONTO estava 'ignorado'.
    db.session.add(SeruProdutoMap(seru_nome='OVOS AO PONTO', ignorar=True,
                                  fator_quantidade=1.0))
    # Conciliacao feita na UI (api_mapear grava so no VendaMapa): vinculado.
    db.session.add(VendaMapa(canal='seru', nome_externo='OVOS AO PONTO',
                             receita_id=cookie.id, ignorar=False,
                             fator_quantidade=1.0))
    db.session.commit()

    r = backfill_venda_mapa()
    assert r['seru_novos'] == 0                      # ja existia: nao recria
    vm = VendaMapa.query.filter_by(canal='seru',
                                   nome_externo='OVOS AO PONTO').first()
    assert vm.ignorar is False                       # conciliacao preservada
    assert vm.receita_id == cookie.id
    assert vm.estado == 'mapeado'                     # nao voltou pra 'ignorado'
    assert VendaMapa.query.filter_by(canal='seru').count() == 1


def test_backfill_recria_uso_de_loja_debito(app, loja):
    """LojaDebito (marcador 'loja usou o mapa') vira VendaMapaUso ligado ao
    VendaMapa de lote equivalente. Idempotente."""
    from app.models import VendaMapa, VendaMapaUso
    cookie = _receita('Cookie')
    lm = LojaProdutoMap(nome_digitado='PAO LOTE', receita_id=cookie.id,
                        fator_quantidade=1.0)
    db.session.add(lm)
    db.session.flush()
    db.session.add(LojaDebito(loja_id=loja.id, loja_produto_map_id=lm.id,
                              fracao_pendente=0.0))
    db.session.commit()

    r1 = backfill_venda_mapa()
    assert r1['lote_novos'] == 1 and r1['uso_novos'] == 1
    vm = VendaMapa.query.filter_by(canal='lote', nome_externo='PAO LOTE').first()
    assert vm is not None
    uso = VendaMapaUso.query.filter_by(venda_mapa_id=vm.id, loja_id=loja.id).first()
    assert uso is not None
    # idempotente: nao duplica o marcador
    r2 = backfill_venda_mapa()
    assert r2['uso_novos'] == 0
    assert VendaMapaUso.query.count() == 1


def test_rota_backfill_owner(app, owner_user, loja):
    """A rota owner /admin/venda-mapa/backfill roda o backfill."""
    cookie = _receita('Cookie')
    db.session.add(SeruProdutoMap(seru_nome='CAFE', receita_id=cookie.id,
                                  fator_quantidade=0.2))
    db.session.commit()
    client = app.test_client()
    client.post('/auth/login', data={'login': owner_user.login, 'senha': '123'},
                follow_redirects=True)
    resp = client.post('/admin/venda-mapa/backfill')
    assert resp.status_code in (302, 303)
    from app.models import VendaMapa
    assert VendaMapa.query.filter_by(canal='seru', nome_externo='CAFE').first()


def test_migrar_fracoes_soma_por_item_e_baixa_inteiro(app, loja):
    """SeruDebito 0.6 + LojaDebito 0.7 do MESMO cookie -> 1.3: baixa 1, sobra
    0.3 no DebitoEstoque; fontes zeradas."""
    cookie = _receita('Cookie')
    el = EstoqueLoja(loja_id=loja.id, receita_id=cookie.id, quantidade=5)
    db.session.add(el)
    sm = SeruProdutoMap(seru_nome='CAFE', receita_id=cookie.id,
                        fator_quantidade=0.2)
    lm = LojaProdutoMap(nome_digitado='CAFE LOTE', receita_id=cookie.id,
                        fator_quantidade=0.2)
    db.session.add_all([sm, lm])
    db.session.flush()
    db.session.add(SeruDebito(loja_id=loja.id, seru_produto_map_id=sm.id,
                              fracao_pendente=0.6))
    db.session.add(LojaDebito(loja_id=loja.id, loja_produto_map_id=lm.id,
                              fracao_pendente=0.7))
    db.session.commit()

    res = migrar_fracoes_para_debito_estoque()
    assert res['inteiros_baixados'] == 1
    deb = DebitoEstoque.query.filter_by(loja_id=loja.id,
                                        receita_id=cookie.id).first()
    assert abs(deb.fracao_pendente - 0.3) < 1e-6
    el = EstoqueLoja.query.filter_by(loja_id=loja.id,
                                     receita_id=cookie.id).first()
    assert el.quantidade == 4                       # 5 - 1
    # fontes zeradas (idempotencia)
    assert SeruDebito.query.first().fracao_pendente == 0.0
    assert LojaDebito.query.first().fracao_pendente == 0.0
    # roda de novo: nada muda
    res2 = migrar_fracoes_para_debito_estoque()
    assert res2['inteiros_baixados'] == 0
    assert abs(DebitoEstoque.query.filter_by(
        loja_id=loja.id, receita_id=cookie.id).first().fracao_pendente
        - 0.3) < 1e-6
