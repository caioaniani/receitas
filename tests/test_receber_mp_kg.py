"""receber_mp com NF em KG: conversão pra unidade do cadastro via peso_unidade
(pão de queijo: saco 2kg, bolinha 18g → quantidade_kg 8 = 444 un), rótulo da
conversão no preview/referência, e sincronia do mp.estoque_atual (a saída de
pedido pra loja baixa o denormalizado — a entrada via bot precisa creditá-lo).
"""
from app.extensions import db
from app.models import MateriaPrima, MovimentacaoEstoque
from app.services.copilot import (
    _quantidade_recebimento_mp,
    _resolver_mp,
    executar_ajuste_estoque,
    executar_receber_mp,
)


def _mp(nome='Pão de Queijo (congelado)', unidade='un', peso=18.0,
        observacoes='saco de 2kg ≈ 111 un'):
    m = MateriaPrima(nome=nome, unidade=unidade, custo_por_kg=0.4662,
                     peso_unidade=peso, observacoes=observacoes,
                     estoque_atual=0)
    db.session.add(m)
    db.session.commit()
    return m


def _resolvida(m):
    return {'id': m.id, 'nome': m.nome, 'unidade': m.unidade,
            'peso_unidade': m.peso_unidade, 'observacoes': m.observacoes}


# ── _resolver_mp expõe os campos da conversão ───────────────────────────────
def test_resolver_mp_expoe_peso_e_observacoes(app):
    m = _mp()
    match = _resolver_mp('Pão de Queijo (congelado)')[0]
    assert match['match'] == 'exato'
    assert match['peso_unidade'] == 18.0
    assert 'saco de 2kg' in match['observacoes']


# ── conversão ───────────────────────────────────────────────────────────────
def test_converte_kg_para_unidades(app):
    m = _mp()
    qtd, rotulo, erro = _quantidade_recebimento_mp(
        {'mp_resolvida': _resolvida(m), 'quantidade_kg': 8})
    assert erro is None
    assert qtd == 444.0                      # 8000g / 18g = 444,4 → 444
    assert '8' in rotulo and '444 un' in rotulo and '18' in rotulo


def test_quantidade_normal_passa_direto(app):
    m = _mp()
    qtd, rotulo, erro = _quantidade_recebimento_mp(
        {'mp_resolvida': _resolvida(m), 'quantidade': 222})
    assert (qtd, rotulo, erro) == (222.0, None, None)


def test_ambos_os_campos_e_erro(app):
    m = _mp()
    qtd, _, erro = _quantidade_recebimento_mp(
        {'mp_resolvida': _resolvida(m), 'quantidade': 10, 'quantidade_kg': 8})
    assert qtd is None and 'OU' in erro


def test_kg_sem_peso_unidade_e_erro(app):
    m = _mp('Coco Ralado Un', peso=None)
    qtd, _, erro = _quantidade_recebimento_mp(
        {'mp_resolvida': _resolvida(m), 'quantidade_kg': 2})
    assert qtd is None and 'peso por unidade' in erro


def test_kg_para_mp_em_gramas_multiplica_por_mil(app):
    m = _mp('Farinha', unidade='g', peso=None)
    qtd, rotulo, erro = _quantidade_recebimento_mp(
        {'mp_resolvida': _resolvida(m), 'quantidade_kg': 25})
    assert erro is None
    assert qtd == 25000.0
    assert 'kg' in rotulo


# ── executor ────────────────────────────────────────────────────────────────
def test_executor_converte_e_credita_estoque_atual(app, admin_user):
    m = _mp()
    res = executar_receber_mp({
        'mp_resolvida': _resolvida(m), 'quantidade_kg': 8,
        'preco_total': 207.20,               # 4 sacos de R$ 51,80
        'referencia': 'NF 123',
    }, admin_user)
    assert res['ok'] is True
    mov = db.session.get(MovimentacaoEstoque, res['mov_id'])
    assert mov.quantidade == 444.0
    assert '444 un' in mov.referencia        # rótulo da conversão na auditoria
    assert abs(mov.preco_unitario - 207.20 / 444) < 1e-9   # por BOLINHA
    # estoque_atual sincronizado (a saída de pedido pra loja baixa daqui)
    assert db.session.get(MateriaPrima, m.id).estoque_atual == 444.0


def test_executor_quantidade_normal_tambem_credita(app, admin_user):
    """Regressão do fix: entrada via bot em unidades também sincroniza o
    denormalizado (antes só criava o movimento)."""
    m = _mp()
    res = executar_receber_mp({
        'mp_resolvida': _resolvida(m), 'quantidade': 111}, admin_user)
    assert res['ok'] is True
    assert db.session.get(MateriaPrima, m.id).estoque_atual == 111.0


def test_executor_rejeita_ambiguidade(app, admin_user):
    m = _mp()
    res = executar_receber_mp({
        'mp_resolvida': _resolvida(m), 'quantidade': 10, 'quantidade_kg': 8},
        admin_user)
    assert res['ok'] is False
    assert MovimentacaoEstoque.query.count() == 0


def test_ajuste_estoque_sincroniza_denormalizado(app, admin_user):
    m = _mp()
    m.estoque_atual = 100
    db.session.commit()
    executar_ajuste_estoque({'mp_resolvida': _resolvida(m), 'quantidade': 30,
                             'tipo': 'saida', 'motivo': 'quebra'}, admin_user)
    assert db.session.get(MateriaPrima, m.id).estoque_atual == 70.0
    executar_ajuste_estoque({'mp_resolvida': _resolvida(m), 'quantidade': 5,
                             'tipo': 'entrada', 'motivo': 'contagem'}, admin_user)
    assert db.session.get(MateriaPrima, m.id).estoque_atual == 75.0


# ── preview Slack mostra a conversão ────────────────────────────────────────
def test_preview_mostra_conversao(app):
    from app.services.copilot import _enriquecer_params
    from app.services.slack_blocks import _preview_receber_mp
    m = _mp()
    params = _enriquecer_params('receber_mp', {
        'mp_nome': m.nome, 'quantidade_kg': 8}, None)
    assert params['quantidade_convertida'] == 444.0
    blocks = _preview_receber_mp(params, 'tok123')
    texto = str(blocks)
    assert '444 un' in texto and '8' in texto
