"""Testes do processamento de itens de NF -> preco + estoque por empresa.

Area de dinheiro/estoque (peso especial): conversao exata, idempotencia,
roteamento por empresa (industria=global, loja=EstoqueLoja).
"""
import json

import pytest

from app.extensions import db
from app.services import conta_pagar_estoque as svc
from app.utils import agora


def _industria():
    from app.models import Loja, SlackCanalLojaMap
    ind = Loja(nome='Industria', ativa=True)
    db.session.add(ind)
    db.session.flush()
    canal = SlackCanalLojaMap(canal_id='C_IND', loja_id=ind.id,
                              eh_industria=True, confirmado_em=agora())
    db.session.add(canal)
    db.session.commit()
    return ind


def _loja_canal(nome='Ribeiro do Vale', canal_id='C_LOJA', confirmado=True):
    from app.models import Loja, SlackCanalLojaMap
    lj = Loja(nome=nome, ativa=True)
    db.session.add(lj)
    db.session.flush()
    canal = SlackCanalLojaMap(canal_id=canal_id, loja_id=lj.id, eh_industria=False,
                              confirmado_em=agora() if confirmado else None)
    db.session.add(canal)
    db.session.commit()
    return lj


def _mp(nome, unidade='un', custo=1.0):
    from app.models import MateriaPrima
    mp = MateriaPrima(nome=nome, unidade=unidade, custo_por_kg=custo, estoque_atual=0)
    db.session.add(mp)
    db.session.commit()
    return mp


def _mapa(nome, mp, fator=1.0, confirmado=True, ignorar=False):
    from app.models import ContaPagarItemMap
    m = ContaPagarItemMap(
        item_nome_norm=svc.normalizar_item_nome(nome),
        item_nome_exemplo=nome,
        materia_prima_id=mp.id if mp else None,
        fator_conversao=fator, ignorar=ignorar,
        confirmado_em=agora() if confirmado else None)
    db.session.add(m)
    db.session.commit()
    return m


def _conta(itens, canal_id='C_IND', fornecedor='Doce Ltda'):
    from app.models import ContaPagar
    c = ContaPagar(origem_canal=canal_id, fornecedor_nome=fornecedor,
                   itens_json=json.dumps(itens), status='aberto')
    db.session.add(c)
    db.session.commit()
    return c


# ── Conversao ──

def test_conversao_industria_un(app):
    _industria()
    mp = _mp('Batom Callebaut', unidade='un', custo=0.0)
    _mapa('BATOM', mp, fator=300)
    c = _conta([{'nome': 'BATOM', 'quantidade': 1, 'valor_total': 210}])

    stats = svc.processar_conta(c)

    assert stats['processados'] == 1
    assert mp.custo_por_kg == pytest.approx(0.70)   # 210/1/300
    assert mp.estoque_atual == pytest.approx(300)


def test_conversao_loja_kg(app):
    lj = _loja_canal()
    mp = _mp('Farinha', unidade='kg', custo=0.0)
    _mapa('FARINHA', mp, fator=25)
    c = _conta([{'nome': 'FARINHA', 'quantidade': 1, 'valor_total': 125}],
               canal_id='C_LOJA')

    stats = svc.processar_conta(c)

    from app.models import EstoqueLoja, MovEstoqueLoja
    assert stats['processados'] == 1
    assert mp.custo_por_kg == pytest.approx(5.0)    # 125/1/25
    el = EstoqueLoja.query.filter_by(loja_id=lj.id, materia_prima_id=mp.id).first()
    assert el is not None and el.quantidade == 25
    assert MovEstoqueLoja.query.filter_by(estoque_loja_id=el.id, tipo='entrada_nf').count() == 1


def test_valor_total_ausente_usa_unitario(app):
    _industria()
    mp = _mp('Ovo', unidade='un', custo=0.0)
    _mapa('OVO', mp, fator=30)
    c = _conta([{'nome': 'OVO', 'quantidade': 2, 'valor_unitario': 30}])  # sem valor_total

    svc.processar_conta(c)

    # 2 caixas * R$30 = R$60 ; /2 caixas = 30/caixa ; /30 = R$1,00/un
    assert mp.custo_por_kg == pytest.approx(1.0)
    assert mp.estoque_atual == pytest.approx(60)


# ── Idempotencia ──

def test_idempotente_nao_duplica(app):
    _industria()
    mp = _mp('Batom', unidade='un', custo=0.0)
    _mapa('BATOM', mp, fator=300)
    c = _conta([{'nome': 'BATOM', 'quantidade': 1, 'valor_total': 210}])

    svc.processar_conta(c)
    stats2 = svc.processar_conta(c)

    from app.models import ContaPagarItemProcessado, MovimentacaoEstoque
    assert stats2['ja_processados'] == 1
    assert stats2['processados'] == 0
    assert mp.estoque_atual == pytest.approx(300)   # nao 600
    assert MovimentacaoEstoque.query.filter_by(materia_prima_id=mp.id).count() == 1
    assert ContaPagarItemProcessado.query.filter_by(conta_pagar_id=c.id).count() == 1


# ── Fornecedor + historico ──

def test_fornecedor_auto_criado_e_historico(app):
    _industria()
    mp = _mp('Batom', unidade='un', custo=0.0)
    _mapa('BATOM', mp, fator=300)
    c = _conta([{'nome': 'BATOM', 'quantidade': 1, 'valor_total': 210}],
               fornecedor='Doceria Nova')

    svc.processar_conta(c)

    from app.models import Fornecedor, HistoricoPrecoMP
    f = Fornecedor.query.filter_by(nome='Doceria Nova').first()
    assert f is not None
    assert c.fornecedor_id == f.id
    h = HistoricoPrecoMP.query.filter_by(materia_prima_id=mp.id).first()
    assert h is not None and h.fornecedor_id == f.id
    assert h.preco_unitario == pytest.approx(0.70)


# ── Variacao de preco ──

def test_variacao_gerada(app):
    _industria()
    mp = _mp('Batom', unidade='un', custo=0.50)   # custo anterior
    _mapa('BATOM', mp, fator=300)
    c = _conta([{'nome': 'BATOM', 'quantidade': 1, 'valor_total': 210}])  # novo 0.70

    stats = svc.processar_conta(c)

    from app.models import VariacaoPrecoMP
    assert stats['variacoes'] == 1
    v = VariacaoPrecoMP.query.filter_by(materia_prima_id=mp.id).first()
    assert v.status == 'novo'
    assert v.variacao_pct == pytest.approx(40.0)   # (0.70-0.50)/0.50*100


def test_sem_custo_anterior_nao_gera_variacao(app):
    _industria()
    mp = _mp('Batom', unidade='un', custo=0.0)     # primeira compra
    _mapa('BATOM', mp, fator=300)
    c = _conta([{'nome': 'BATOM', 'quantidade': 1, 'valor_total': 210}])

    stats = svc.processar_conta(c)

    from app.models import VariacaoPrecoMP
    assert stats['variacoes'] == 0
    assert VariacaoPrecoMP.query.count() == 0


# ── Salvaguardas ──

def test_mapa_nao_confirmado_nao_processa(app):
    _industria()
    mp = _mp('Batom', unidade='un', custo=0.0)
    _mapa('BATOM', mp, fator=300, confirmado=False)
    c = _conta([{'nome': 'BATOM', 'quantidade': 1, 'valor_total': 210}])

    stats = svc.processar_conta(c)

    assert stats['pendentes'] == 1
    assert stats['processados'] == 0
    assert mp.estoque_atual == 0


def test_item_sem_mapa_cria_pendente(app):
    _industria()
    c = _conta([{'nome': 'PRODUTO NOVO', 'quantidade': 1, 'valor_total': 10,
                 'unidade': 'un', 'fator_embalagem': 12}])

    stats = svc.processar_conta(c)

    from app.models import ContaPagarItemMap
    assert stats['pendentes_novos'] == 1
    m = ContaPagarItemMap.query.filter_by(
        item_nome_norm=svc.normalizar_item_nome('PRODUTO NOVO')).first()
    assert m is not None and m.estado == 'pendente'
    assert m.ia_fator_sugerido == 12


def test_canal_nao_confirmado_nao_da_entrada(app):
    lj = _loja_canal(canal_id='C_X', confirmado=False)
    mp = _mp('Farinha', unidade='kg', custo=0.0)
    _mapa('FARINHA', mp, fator=25)
    c = _conta([{'nome': 'FARINHA', 'quantidade': 1, 'valor_total': 125}],
               canal_id='C_X')

    stats = svc.processar_conta(c)

    assert stats['canal_nao_confirmado'] == 1
    assert mp.custo_por_kg == 0.0   # nao alterou


def test_item_ignorado(app):
    _industria()
    mp = _mp('Batom', unidade='un', custo=0.0)
    _mapa('BATOM', mp, fator=300, ignorar=True)
    c = _conta([{'nome': 'BATOM', 'quantidade': 1, 'valor_total': 210}])

    stats = svc.processar_conta(c)

    assert stats['ignorados'] == 1
    assert mp.estoque_atual == 0


# ── Edge cases ──

def test_dados_invalidos(app):
    _industria()
    mp = _mp('Batom', unidade='un', custo=0.0)
    _mapa('BATOM', mp, fator=300)
    c = _conta([{'nome': 'BATOM', 'quantidade': 0, 'valor_total': 210}])

    stats = svc.processar_conta(c)

    assert stats['dados_invalidos'] == 1
    assert mp.estoque_atual == 0


def test_fator_zero_tratado_como_um(app):
    _industria()
    mp = _mp('Batom', unidade='un', custo=0.0)
    _mapa('BATOM', mp, fator=0)
    c = _conta([{'nome': 'BATOM', 'quantidade': 1, 'valor_total': 5}])

    svc.processar_conta(c)

    assert mp.custo_por_kg == pytest.approx(5.0)    # 5/1/1.0
    assert mp.estoque_atual == pytest.approx(1)


def test_fracao_em_loja_fica_pendente(app):
    lj = _loja_canal()
    mp = _mp('Fermento', unidade='kg', custo=0.0)
    _mapa('FERMENTO', mp, fator=2.5)    # 1 * 2.5 = 2.5 (fracionario)
    c = _conta([{'nome': 'FERMENTO', 'quantidade': 1, 'valor_total': 50}],
               canal_id='C_LOJA')

    stats = svc.processar_conta(c)

    from app.models import EstoqueLoja
    assert stats['fracao_loja_pendente'] == 1
    assert stats['processados'] == 0
    assert EstoqueLoja.query.filter_by(loja_id=lj.id, materia_prima_id=mp.id).count() == 0


# ── Importacao de historico (aovivo=False) ──

def test_historico_nao_mexe_estoque(app):
    _industria()
    mp = _mp('Batom', unidade='un', custo=0.50)
    _mapa('BATOM', mp, fator=300)
    c = _conta([{'nome': 'BATOM', 'quantidade': 1, 'valor_total': 210}])

    stats = svc.processar_conta(c, aovivo=False)

    from app.models import ContaPagarItemProcessado, VariacaoPrecoMP
    assert stats['processados'] == 0
    assert stats['historico_sem_estoque'] == 1
    assert mp.estoque_atual == 0
    assert mp.custo_por_kg == 0.50                  # custo inalterado
    assert ContaPagarItemProcessado.query.count() == 0
    assert VariacaoPrecoMP.query.count() == 0


def test_conversao_mp_em_gramas_custo_por_kg(app):
    """MP em gramas (ex: acai 10L/cx, usado em g): fator em g por compra.
    Estoque entra em gramas; custo_por_kg sai por KG (nao por grama)."""
    from app.models import ContaPagar, ContaPagarItemMap, MateriaPrima
    with app.app_context():
        _industria()  # canal C_IND confirmado -> estoque global
        mp = _mp('Acai polpa', unidade='g', custo=0)
        mp_id = mp.id
        db.session.add(ContaPagarItemMap(
            item_nome_norm=svc.normalizar_item_nome('ACAI NATURAL 10L'),
            item_nome_exemplo='ACAI NATURAL 10L', materia_prima_id=mp_id,
            confirmado_em=agora(), fator_conversao=10000.0))
        conta = ContaPagar(
            tipo_documento='nota_fiscal', fornecedor_nome='Distribuidora', status='aberto',
            origem_canal='C_IND',
            itens_json=json.dumps([{'nome': 'ACAI NATURAL 10L', 'quantidade': 6,
                                    'valor_total': 906.0}]))
        db.session.add(conta)
        db.session.commit()
        stats = svc.processar_conta(conta, aovivo=True)
        assert stats['processados'] == 1
        mp = db.session.get(MateriaPrima, mp_id)
        assert mp.estoque_atual == 60000          # 6 cx x 10000 g
        assert abs(mp.custo_por_kg - 15.10) < 1e-6   # 906 / 60 kg, NAO /60000 g


def test_normalizar_ignora_validade_lote():
    """Mesmo produto com validade/lote diferentes -> mesma chave de vinculo."""
    n = svc.normalizar_item_nome
    a = n('FARINHA DE TRIGO FRANCE SA BAGATELLE T45 VAL 1 7/12/2026 LOTE GXB12603 17A')
    b = n('FARINHA DE TRIGO FRANCE SA BAGATELLE T45 VAL 3 0/10/2026 LOTE:GXB12601 30A')
    assert a and a == b
    assert 'lote' not in a and 'val' not in a.split()
    # variacao so de pontuacao no lote tambem junta
    assert n('X TIPO 150 LOTE BP22512311B') == n('X TIPO 150 LOTE:BP22512311B')


def test_limpar_nome_item_legivel():
    assert (svc.limpar_nome_item('FARINHA FRANCE BAGATELLE T45 VAL 30/09/2026 LOTE GXB12603')
            == 'FARINHA FRANCE BAGATELLE T45')


def test_migrar_junta_duplicados_preserva_confirmado(app):
    from app.models import ContaPagarItemMap
    with app.app_context():
        mp = _mp('Farinha Bagatelle T45', unidade='kg')
        mp_id = mp.id
        m1 = ContaPagarItemMap(
            item_nome_norm='velho1', materia_prima_id=mp_id,
            confirmado_em=agora(), fator_conversao=1.0,
            item_nome_exemplo='FARINHA BAGATELLE T45 VAL 1/2026 LOTE A1')
        m2 = ContaPagarItemMap(
            item_nome_norm='velho2',
            item_nome_exemplo='FARINHA BAGATELLE T45 VAL 2/2026 LOTE:B2')
        m3 = ContaPagarItemMap(
            item_nome_norm='velho3', item_nome_exemplo='ACUCAR CRISTAL UNIAO')
        db.session.add_all([m1, m2, m3])
        db.session.commit()

        stats = svc.migrar_nomes_itens()
        assert stats['mesclados'] == 1          # m2 mesclado no m1 (confirmado)
        assert stats['conflitos'] == 0
        assert ContaPagarItemMap.query.count() == 2     # farinha + acucar
        farinha = ContaPagarItemMap.query.filter_by(materia_prima_id=mp_id).first()
        assert farinha is not None and farinha.confirmado_em is not None
        assert farinha.item_nome_exemplo == 'FARINHA BAGATELLE T45'


def test_limpar_mapas_orfaos(app):
    """Pendente cujo item nao existe em NENHUMA conta (sobra de leitura da IA
    re-extraida — ex: destinatario lido como item) e removido. Pendente com
    NF, confirmado e ignorado ficam (decisao humana nao se apaga)."""
    import json

    from app.models import ContaPagar, ContaPagarItemMap
    with app.app_context():
        mp = _mp('Farinha', unidade='g')
        db.session.add(ContaPagar(
            tipo_documento='nota_fiscal', fornecedor_nome='Moinho',
            status='aberto',
            itens_json=json.dumps([{'nome': 'FARINHA GRANEL',
                                    'quantidade': 1, 'valor_total': 10}])))
        db.session.add_all([
            # pendente COM nota -> fica
            ContaPagarItemMap(
                item_nome_norm=svc.normalizar_item_nome('FARINHA GRANEL'),
                item_nome_exemplo='FARINHA GRANEL'),
            # pendente orfao (nenhuma nota tem) -> sai
            ContaPagarItemMap(
                item_nome_norm=svc.normalizar_item_nome('BROOKFIELD PAULISTA'),
                item_nome_exemplo='BROOKFIELD PAULISTA'),
            # orfaos com decisao humana -> ficam
            ContaPagarItemMap(
                item_nome_norm='orfao confirmado', item_nome_exemplo='X',
                materia_prima_id=mp.id, confirmado_em=agora()),
            ContaPagarItemMap(
                item_nome_norm='orfao ignorado', item_nome_exemplo='Y',
                ignorar=True),
        ])
        db.session.commit()

        assert svc.limpar_mapas_orfaos() == 1
        restantes = {m.item_nome_norm for m in ContaPagarItemMap.query.all()}
        assert svc.normalizar_item_nome('BROOKFIELD PAULISTA') not in restantes
        assert svc.normalizar_item_nome('FARINHA GRANEL') in restantes
        assert 'orfao confirmado' in restantes
        assert 'orfao ignorado' in restantes


def test_conversao_metrica():
    assert svc.conversao_metrica('kg', 'g') == 1000.0
    assert svc.conversao_metrica(' KG ', 'g') == 1000.0   # espacos/caixa
    assert svc.conversao_metrica('l', 'ml') == 1000.0
    assert svc.conversao_metrica('g', 'g') == 1.0
    assert svc.conversao_metrica('g', 'kg') == 0.001
    assert svc.conversao_metrica('kg', 'ml') is None      # massa vs volume
    assert svc.conversao_metrica('cx', 'g') is None       # embalagem nao e metrica
    assert svc.conversao_metrica('', 'g') is None
    assert svc.conversao_metrica(None, None) is None


def test_prefill_sugestao_regras():
    """Traducao da sugestao da IA pra unidade da MP (caso Toddy 2026-06-10:
    'CX 1,8KG' -> IA sugere 1.8/kg, MP em g, NF conta em cx -> 1800/cx).
    Assinatura: prefill_sugestao(ia_fator, ia_unidade, unidade_mp, unidade_nf)."""
    # Caso Toddy: IA leu o conteudo da embalagem em kg, NF em cx, MP em g.
    assert svc.prefill_sugestao(1.8, 'kg', 'g', 'cx') == (1800.0, 'cx')

    # Acai: IA ja sugeriu na unidade da MP (ml) — fator fica, unidade vira a
    # da NF (cx).
    assert svc.prefill_sugestao(10000.0, 'ml', 'ml', 'CX') == (10000.0, 'CX')

    # Farinha a granel: a PROPRIA NF conta em kg -> fisica pura (1 kg =
    # 1000 g); o tamanho de embalagem que a IA leu no nome e irrelevante.
    assert svc.prefill_sugestao(25.0, 'kg', 'g', 'kg') == (1000.0, 'kg')

    # Abacaxi: nada metrico envolvido -> sugestao crua da IA, como antes.
    assert svc.prefill_sugestao(None, 'un', 'g', 'un') == (None, 'un')

    # Fator vindo do JSON da NF como string nao quebra (detalhe da conta).
    assert svc.prefill_sugestao('2.01', 'kg', 'g', 'cx') == (2010.0, 'cx')
    assert svc.prefill_sugestao('lixo', 'kg', 'g', 'cx') == (None, 'cx')
