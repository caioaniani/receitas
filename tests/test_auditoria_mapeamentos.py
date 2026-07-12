"""Auditoria de mapeamentos venda→estoque (12/07/2026): cada verificação
acusa a situação que gera diferença de estoque — e fica quieta no saudável.
"""
from datetime import timedelta

from app.extensions import db
from app.models import (
    EstoqueLoja,
    Loja,
    MovEstoqueLoja,
    Produto,
    ProdutoItem,
    Receita,
    SeruDebito,
    SeruLojaMap,
    SeruPedidoProcessado,
    VendaMapa,
    VendaSeruDiaria,
)
from app.services.auditoria_mapeamentos import auditar
from app.utils import agora, hoje

TOKEN = 'tok-auditoria'


def _loja(nome='Loja A'):
    loja = Loja(nome=nome, ativa=True)
    db.session.add(loja)
    db.session.commit()
    return loja


def _receita(nome='Croissant', arquivada=False):
    r = Receita(nome=nome, categoria='X', rendimento_qtd=1,
                rendimento_unidade='un', peso_base=100.0)
    if arquivada:
        r.arquivada_em = agora()
    db.session.add(r)
    db.session.commit()
    return r


def _venda_seru(nome, qtd=10, loja_seru='PADARIA CENTRO'):
    db.session.add(VendaSeruDiaria(
        data=hoje() - timedelta(days=1), loja_seru=loja_seru,
        seru_nome=nome, qtd=qtd, faturamento=qtd * 10, n_pedidos=1))
    db.session.commit()


def test_pendente_ignorado_e_sem_mapa_com_venda(app):
    with app.app_context():
        _venda_seru('BOLO PENDENTE', qtd=30)
        _venda_seru('AGUA IGNORADA', qtd=25)
        _venda_seru('NUNCA VISTO', qtd=7)
        db.session.add(VendaMapa(canal='seru', nome_externo='BOLO PENDENTE'))
        db.session.add(VendaMapa(canal='seru', nome_externo='AGUA IGNORADA',
                                 ignorar=True))
        db.session.commit()
        out = auditar(dias=7)
    assert out['pendentes_com_venda'][0]['nome_externo'] == 'BOLO PENDENTE'
    assert out['pendentes_com_venda'][0]['qtd'] == 30.0
    assert out['ignorados_com_venda'][0]['nome_externo'] == 'AGUA IGNORADA'
    assert out['nomes_sem_mapa'][0]['nome_externo'] == 'NUNCA VISTO'


def test_loja_sem_vinculo_e_confirmacao(app):
    with app.app_context():
        loja = _loja()
        _venda_seru('X', qtd=5, loja_seru='FILIAL NOVA')
        _venda_seru('X', qtd=3, loja_seru='FILIAL FANTASMA')
        db.session.add(SeruLojaMap(seru_company_name='FILIAL NOVA',
                                   loja_id=loja.id))   # sem confirmado_em
        db.session.commit()
        out = auditar(dias=7)
    problemas = {x['loja_seru']: x['problema']
                 for x in out['lojas_sem_vinculo']}
    assert 'NAO confirmado' in problemas['FILIAL NOVA']
    assert 'sem linha' in problemas['FILIAL FANTASMA']


def test_alvo_morto_fator_zero_e_fracionario_informativo(app):
    with app.app_context():
        r_arq = _receita('Antiga', arquivada=True)
        r_ok = _receita('Cookie')
        db.session.add(VendaMapa(canal='seru', nome_externo='ITEM MORTO',
                                 receita_id=r_arq.id))
        db.session.add(VendaMapa(canal='seru', nome_externo='ITEM ZERO',
                                 receita_id=r_ok.id, fator_quantidade=0))
        db.session.add(VendaMapa(canal='seru', nome_externo='CAFE',
                                 receita_id=r_ok.id, fator_quantidade=0.2))
        db.session.commit()
        out = auditar(dias=7)
    assert any(x['nome_externo'] == 'ITEM MORTO' and 'arquivada' in x['problema']
               for x in out['alvos_mortos'])
    assert out['fator_zero'][0]['nome_externo'] == 'ITEM ZERO'
    # fracionario e INFORMATIVO (regra de negocio do cookie) — lista, nao acusa
    assert any(x['nome_externo'] == 'CAFE'
               for x in out['fatores_fracionarios_informativo'])


def test_duplicata_cesta_vazia_e_componente_orfao(app):
    with app.app_context():
        r = _receita()
        db.session.add(VendaMapa(canal='seru', nome_externo='DUPLA',
                                 receita_id=r.id))
        db.session.add(VendaMapa(canal='seru', nome_externo='DUPLA'))
        p_vazio = Produto(nome='Cesta Vazia', preco_atacado=10)
        p_orfao = Produto(nome='Cesta Orfa', preco_atacado=10)
        db.session.add_all([p_vazio, p_orfao])
        db.session.flush()
        db.session.add(ProdutoItem(produto_id=p_orfao.id,
                                   item_nome='Nome Velho', quantidade=1))
        db.session.add(VendaMapa(canal='seru', nome_externo='CESTA V',
                                 produto_id=p_vazio.id))
        db.session.add(VendaMapa(canal='seru', nome_externo='CESTA O',
                                 produto_id=p_orfao.id))
        db.session.commit()
        out = auditar(dias=7)
    assert {'canal': 'seru', 'nome_externo': 'DUPLA',
            'linhas': 2} in out['duplicatas']
    assert out['cestas_vazias'][0]['produto'] == 'Cesta Vazia'
    assert out['componentes_orfaos'][0]['item_nome'] == 'Nome Velho'


def test_sem_estoque_debito_travado_e_itens_nao_baixados(app):
    with app.app_context():
        loja = _loja()
        r = _receita()
        el = EstoqueLoja(loja_id=loja.id, receita_id=r.id, quantidade=0)
        db.session.add(el)
        db.session.flush()
        db.session.add(MovEstoqueLoja(
            estoque_loja_id=el.id, tipo='venda_seru_sem_estoque',
            quantidade=4, data=agora(), referencia='t'))
        db.session.add(SeruDebito(loja_id=loja.id, seru_produto_map_id=1,
                                  fracao_pendente=1.4))
        db.session.add(SeruPedidoProcessado(
            seru_pedido_id='p1', loja_id=loja.id,
            n_itens_total=3, n_itens_baixados=1))
        db.session.commit()
        out = auditar(dias=7)
    assert out['sem_estoque_recente'][0]['item'] == 'Croissant'
    assert out['sem_estoque_recente'][0]['qtd'] == 4.0
    assert out['debitos_travados'][0]['fracao_pendente'] == 1.4
    assert out['pedidos_com_itens_nao_baixados'][0]['itens_nao_baixados'] == 2


def test_banco_saudavel_fica_quieto_e_rota_exige_token(app):
    with app.app_context():
        out = auditar(dias=7)
        chaves_lista = [k for k, v in out.items() if isinstance(v, list)]
        assert all(out[k] == [] for k in chaves_lista)
    app.config['CLAUDE_API_TOKEN'] = TOKEN
    c = app.test_client()
    assert c.get('/api/claude/auditoria-mapeamentos').status_code == 401
    resp = c.get('/api/claude/auditoria-mapeamentos?dias=7',
                 headers={'Authorization': f'Bearer {TOKEN}'})
    assert resp.status_code == 200
    assert resp.get_json()['ok'] is True
