"""Perda de produção do padeiro (13/08/2026, pedido do dono: "colocar as
perdas na tela do padeiro, eles precisam ter uma aba para lançar se queimou
algo"). Decisões dele: item PRONTO debita EstoqueProducao (saturando em 0);
FORNADA queimada consome MP+subs da ficha SEM creditar; relatório admin com
custo pela ficha."""
import pytest

from app.extensions import db
from app.models import (
    EstoqueProducao,
    MateriaPrima,
    MovEstoqueProducao,
    MovimentacaoEstoque,
    PerdaProducao,
    Receita,
    ReceitaIngrediente,
    Usuario,
)
from app.services import perda_producao as pp


def _receita(nome='Pão Perda', rendimento=10, peso_base=1000.0):
    r = Receita(nome=nome, categoria='Paes', rendimento_qtd=rendimento,
                rendimento_unidade='un', peso_base=peso_base)
    db.session.add(r)
    db.session.commit()
    return r


def _com_ficha_mp(r, farinha=5000):
    mp = MateriaPrima(nome='Farinha', unidade='g', custo_por_kg=5.0,
                      estoque_atual=farinha)
    db.session.add(mp)
    db.session.flush()
    db.session.add(ReceitaIngrediente(receita_id=r.id, tipo='mp',
                                      ingrediente_nome='Farinha',
                                      porcentagem=100))
    db.session.commit()
    return mp


def _estoque(r, qtd):
    ep = EstoqueProducao(receita_id=r.id, quantidade=qtd)
    db.session.add(ep)
    db.session.commit()
    return ep


def _func_padrao():
    from app.models import Funcionario
    f = Funcionario.query.filter_by(cpf='999.999.999-99').first()
    if not f:
        f = Funcionario(nome='Padeiro Teste RH', cpf='999.999.999-99',
                        funcao='Padeiro', ativo=True)
        db.session.add(f)
        db.session.commit()
    return f


def _registrar(*args, **kw):
    """pp.registrar com responsável default — o contrato do responsável tem
    testes próprios; os demais não precisam repetir o boilerplate."""
    kw.setdefault('funcionario_id', _func_padrao().id)
    return pp.registrar(*args, **kw)


def _padeiro(login='pad_perda'):
    u = Usuario(nome='Padeiro Perda', login=login, papel='padeiro')
    u.set_senha('12345678')
    db.session.add(u)
    db.session.commit()
    return u


def _login(client, user, senha):
    client.post('/auth/login', data={'login': user.login, 'senha': senha},
                follow_redirects=True)


# ── serviço: item pronto ────────────────────────────────────────────

def test_perda_item_pronto_debita_estoque(app, admin_user):
    r = _receita()
    ep = _estoque(r, 30)
    res = _registrar(r.id, 10, 'queimou', admin_user.id)
    db.session.refresh(ep)
    assert ep.quantidade == 20
    assert res['baixado'] == 10 and res['falta'] == 0 and not res['avisos']
    mov = (MovEstoqueProducao.query
           .filter_by(estoque_producao_id=ep.id, tipo='perda_producao').one())
    assert mov.quantidade == 10
    assert mov.referencia.startswith(f'Perda #{res["perda_id"]} — ')
    assert PerdaProducao.query.count() == 1


def test_perda_maior_que_saldo_satura_em_zero_e_avisa(app, admin_user):
    r = _receita()
    ep = _estoque(r, 4)
    res = _registrar(r.id, 10, 'caiu', admin_user.id)
    db.session.refresh(ep)
    assert ep.quantidade == 0                       # nunca negativa
    assert res['baixado'] == 4 and res['falta'] == 6
    assert res['avisos'] and 'conferir' in res['avisos'][0]
    tipos = {m.tipo: m.quantidade for m in
             MovEstoqueProducao.query.filter_by(estoque_producao_id=ep.id)}
    assert tipos == {'perda_producao': 4, 'perda_producao_sem_estoque': 6}


# ── serviço: fornada queimada ───────────────────────────────────────

def test_fornada_consome_mp_sem_creditar_estoque(app, admin_user):
    r = _receita()
    mp = _com_ficha_mp(r, farinha=5000)
    _registrar(r.id, 10, 'queimou', admin_user.id, fornada=True)
    db.session.refresh(mp)
    # 10 un / rendimento 10 = 1 base = 1000 g de farinha (mesma conta do
    # produzir — teste espelho de test_produzir_credita_estoque_e_baixa_mp).
    assert mp.estoque_atual == 4000
    mov_mp = (MovimentacaoEstoque.query
              .filter_by(materia_prima_id=mp.id, tipo='saida').one())
    assert 'Fornada queimada' in mov_mp.referencia
    # NADA creditado nem debitado no estoque pronto da receita.
    ep = EstoqueProducao.query.filter_by(receita_id=r.id).first()
    assert ep is None or (ep.quantidade or 0) == 0
    assert MovEstoqueProducao.query.count() == 0


def test_fornada_consome_subreceita_pronta(app, admin_user):
    sub = _receita('Massa Sub Perda')
    _estoque(sub, 10)
    r = _receita('Croissant Perda')
    db.session.add(ReceitaIngrediente(receita_id=r.id, tipo='receita',
                                      ingrediente_nome=sub.nome,
                                      sub_receita_id=sub.id, porcentagem=5))
    db.session.commit()
    _registrar(r.id, 10, 'queimou', admin_user.id, fornada=True)
    ep_sub = EstoqueProducao.query.filter_by(receita_id=sub.id).one()
    # 10 un do pai × 5 subs/lote ÷ rendimento 10 = 5 subs consumidas.
    assert ep_sub.quantidade == 5


# ── serviço: validações ─────────────────────────────────────────────

def test_validacoes_nada_e_gravado(app, admin_user):
    r = _receita()
    _estoque(r, 30)
    with pytest.raises(ValueError):
        _registrar(r.id, 0, 'queimou', admin_user.id)
    with pytest.raises(ValueError):
        _registrar(r.id, 5, 'motivo_inventado', admin_user.id)
    with pytest.raises(ValueError):                 # outro exige observação
        _registrar(r.id, 5, 'outro', admin_user.id)
    with pytest.raises(ValueError):                 # sanidade de dedo errado
        _registrar(r.id, pp.QTD_MAXIMA + 1, 'queimou', admin_user.id)
    with pytest.raises(ValueError):                 # receita inexistente
        _registrar(999999, 5, 'queimou', admin_user.id)
    assert PerdaProducao.query.count() == 0
    assert MovEstoqueProducao.query.count() == 0


# ── serviço: exclusão (admin) ───────────────────────────────────────

def test_excluir_estorna_exato_o_que_saiu(app, admin_user):
    r = _receita()
    ep = _estoque(r, 4)
    res = _registrar(r.id, 10, 'queimou', admin_user.id)   # baixa só 4
    out = pp.excluir(res['perda_id'], admin_user.id)
    assert out['estornado'] == 4                    # nunca os 10 nominais
    db.session.refresh(ep)
    assert ep.quantidade == 4
    est = (MovEstoqueProducao.query
           .filter_by(tipo='perda_producao_estorno').one())
    assert est.quantidade == 4
    assert PerdaProducao.query.count() == 0


def test_excluir_fornada_recusa(app, admin_user):
    r = _receita()
    _com_ficha_mp(r)
    res = _registrar(r.id, 10, 'queimou', admin_user.id, fornada=True)
    with pytest.raises(ValueError):
        pp.excluir(res['perda_id'], admin_user.id)
    assert PerdaProducao.query.count() == 1         # segue registrada


def test_excluir_nao_confunde_perda_1_com_11(app, admin_user):
    """O estorno acha os movimentos por prefixo 'Perda #<id> — ' — o
    delimitador impede #1 de casar #11 (classe do bug ret-1×ret-16).
    Quantidades variadas de propósito: iguais em sequência cairiam na
    guarda de duplo lançamento (30s)."""
    r = _receita()
    ep = _estoque(r, 1000)
    primeira = _registrar(r.id, 3, 'queimou', admin_user.id)
    soma_meio = 0
    for i in range(9):                              # ids intermediários
        _registrar(r.id, 10 + i, 'caiu', admin_user.id)
        soma_meio += 10 + i
    decima_primeira = _registrar(r.id, 5, 'caiu', admin_user.id)
    assert decima_primeira['perda_id'] == primeira['perda_id'] + 10
    pp.excluir(primeira['perda_id'], admin_user.id)
    db.session.refresh(ep)
    assert ep.quantidade == 1000 - 3 - soma_meio - 5 + 3   # só os 3 voltaram


def test_excluir_duas_vezes_recusa(app, admin_user):
    """Claim atômico (achado A1 da revisão): a 2ª exclusão acha 0 linhas no
    DELETE condicional e desiste — o estoque nunca é creditado 2x."""
    r = _receita()
    ep = _estoque(r, 30)
    res = _registrar(r.id, 10, 'queimou', admin_user.id)
    pp.excluir(res['perda_id'], admin_user.id)
    with pytest.raises(ValueError):
        pp.excluir(res['perda_id'], admin_user.id)
    db.session.refresh(ep)
    assert ep.quantidade == 30                      # voltou UMA vez
    assert (MovEstoqueProducao.query
            .filter_by(tipo='perda_producao_estorno').count()) == 1


def test_duplo_lancamento_em_30s_recusado(app, admin_user):
    """Retry de rede/toque duplo: mesma receita+quantidade+usuário em <30s
    não vira 2ª perda (padrão do checklist). Quantidade diferente passa."""
    r = _receita()
    _estoque(r, 30)
    _registrar(r.id, 5, 'queimou', admin_user.id)
    with pytest.raises(ValueError):
        _registrar(r.id, 5, 'queimou', admin_user.id)
    _registrar(r.id, 6, 'queimou', admin_user.id)  # outra perda de verdade
    assert PerdaProducao.query.count() == 2


def test_fornada_de_receita_arquivada_recusa(app, admin_user):
    """Consumir a FICHA de receita morta não tem justificativa (item pronto
    de arquivada segue podendo se perder — exceção da classe desperdício)."""
    from app.utils import agora
    r = _receita('Pao Morto')
    _com_ficha_mp(r)
    r.arquivada_em = agora()
    db.session.commit()
    with pytest.raises(ValueError):
        _registrar(r.id, 5, 'queimou', admin_user.id, fornada=True)
    # item PRONTO de arquivada ainda escoa
    _estoque(r, 10)
    res = _registrar(r.id, 5, 'queimou', admin_user.id)
    assert res['baixado'] == 5


def test_fornada_avisa_falta_de_subreceita(app, admin_user):
    """Achado A2 da revisão: congelado que não cobre a sub da fornada tem
    que virar aviso visível (antes só ficava no ledger)."""
    sub = _receita('Massa Sub Falta')
    _estoque(sub, 2)
    r = _receita('Croissant Sub Falta')
    db.session.add(ReceitaIngrediente(receita_id=r.id, tipo='receita',
                                      ingrediente_nome=sub.nome,
                                      sub_receita_id=sub.id, porcentagem=5))
    db.session.commit()
    res = _registrar(r.id, 10, 'queimou', admin_user.id, fornada=True)
    # consumo = 10×5/10 = 5; só havia 2 no congelado.
    assert res['avisos'] and 'congelado só tinha 2' in res['avisos'][0]
    ep_sub = EstoqueProducao.query.filter_by(receita_id=sub.id).one()
    assert ep_sub.quantidade == 0


def test_listar_clampa_o_periodo(app, admin_user):
    out = pp.listar(dias=9999)
    assert out['dias'] == 365                       # rótulo honesto


# ── relatório com custo ─────────────────────────────────────────────

def test_listar_traz_custo_pela_ficha(app, admin_user):
    r = _receita()
    _com_ficha_mp(r)                                # 1000g × R$5/kg / 10 un
    _estoque(r, 30)
    _registrar(r.id, 10, 'queimou', admin_user.id)
    out = pp.listar(dias=7)
    assert out['total_qtd'] == 10
    assert out['total_custo'] == pytest.approx(5.0)  # R$ 0,50/un × 10
    assert out['perdas'][0]['receita'] == r.nome
    assert out['perdas'][0]['sem_custo'] is False


# ── rotas ───────────────────────────────────────────────────────────

def test_rota_padeiro_lanca_perda(app):
    r = _receita()
    _estoque(r, 30)
    pad = _padeiro()
    c = app.test_client()
    _login(c, pad, '12345678')
    resp = c.post('/padeiro/perdas', data={
        'item_ref': f'receita:{r.id}', 'quantidade': '5',
        'motivo': 'queimou', 'observacao': '',
        'funcionario_id': str(_func_padrao().id),
    })
    assert resp.status_code == 302
    p = PerdaProducao.query.one()
    assert p.quantidade == 5 and p.criado_por_id == pad.id


def test_rota_recusa_ref_que_nao_e_receita(app):
    pad = _padeiro()
    c = app.test_client()
    _login(c, pad, '12345678')
    c.post('/padeiro/perdas', data={
        'item_ref': 'produto:5', 'quantidade': '5', 'motivo': 'queimou'})
    c.post('/padeiro/perdas', data={
        'item_ref': '', 'quantidade': '5', 'motivo': 'queimou'})
    assert PerdaProducao.query.count() == 0


def test_rota_funcionario_nao_entra(app):
    u = Usuario(nome='func', login='func_perda', papel='funcionario')
    u.set_senha('12345678')
    db.session.add(u)
    db.session.commit()
    c = app.test_client()
    _login(c, u, '12345678')
    assert c.get('/padeiro/perdas').status_code == 403


def test_relatorio_admin_e_exclusao_via_rota(app, admin_user):
    r = _receita()
    _estoque(r, 30)
    res = _registrar(r.id, 10, 'queimou', admin_user.id)
    c = app.test_client()
    _login(c, admin_user, '123')
    page = c.get('/producao/perdas')
    assert page.status_code == 200
    assert 'Perdas da produ'.encode() in page.data
    resp = c.post(f'/producao/perdas/{res["perda_id"]}/excluir')
    assert resp.status_code == 302
    assert PerdaProducao.query.count() == 0


def test_relatorio_nao_e_do_padeiro(app):
    pad = _padeiro('pad_perda2')
    c = app.test_client()
    _login(c, pad, '12345678')
    assert c.get('/producao/perdas').status_code == 403


def test_link_perdas_no_header_da_tv(app, admin_user):
    c = app.test_client()
    _login(c, admin_user, '123')
    resp = c.get('/padeiro/')
    assert '/padeiro/perdas'.encode() in resp.data


# ── responsável pela perda (dono 13/08/2026, follow-up) ─────────────

def test_responsavel_obrigatorio_e_ativo(app, admin_user):
    r = _receita()
    _estoque(r, 30)
    with pytest.raises(ValueError):                 # sem responsável
        pp.registrar(r.id, 5, 'queimou', admin_user.id)
    f = _func_padrao()
    f.ativo = False
    db.session.commit()
    with pytest.raises(ValueError):                 # desligado no RH
        pp.registrar(r.id, 5, 'queimou', admin_user.id, funcionario_id=f.id)
    assert PerdaProducao.query.count() == 0
    f.ativo = True
    db.session.commit()
    pp.registrar(r.id, 5, 'queimou', admin_user.id, funcionario_id=f.id)
    assert PerdaProducao.query.one().funcionario_id == f.id


def test_responsaveis_filtra_por_funcao(app):
    from app.models import Funcionario
    db.session.add_all([
        Funcionario(nome='Ana Padeira', cpf='001', funcao='Padeiro',
                    ativo=True),
        Funcionario(nome='Beto Ajudante', cpf='002',
                    funcao='Ajudante de Padeiro', ativo=True),
        Funcionario(nome='Caua Auxiliar', cpf='003',
                    funcao='Auxiliar de Produção', ativo=True),
        Funcionario(nome='Dani Atendente', cpf='004', funcao='Atendente',
                    ativo=True),
        Funcionario(nome='Eva Desligada', cpf='005', funcao='Padeiro',
                    ativo=False),
    ])
    db.session.commit()
    nomes = [f.nome for f in pp.responsaveis_producao()]
    assert nomes == ['Ana Padeira', 'Beto Ajudante', 'Caua Auxiliar']


def test_responsaveis_fallback_quando_nenhuma_funcao_casa(app):
    """RH com funções renomeadas nunca trava a perda: sem ninguém casando o
    filtro, a lista cai pra TODOS os ativos (fail-open deliberado)."""
    from app.models import Funcionario
    db.session.add(Funcionario(nome='Zeca Atendente', cpf='006',
                               funcao='Atendente', ativo=True))
    db.session.commit()
    assert [f.nome for f in pp.responsaveis_producao()] == ['Zeca Atendente']


def test_relatorio_mostra_responsavel(app, admin_user):
    r = _receita()
    _estoque(r, 30)
    _registrar(r.id, 10, 'queimou', admin_user.id)
    out = pp.listar(dias=7)
    assert out['perdas'][0]['responsavel'] == 'Padeiro Teste RH'
