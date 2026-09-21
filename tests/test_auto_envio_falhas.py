"""Falha de cadastro não pode esconder nem interromper a semana inteira."""
import json
from copy import deepcopy
from datetime import timedelta

import pytest

from app.extensions import db
from app.models import (
    AppConfig,
    MateriaPrima,
    MovimentacaoEstoque,
    PlanejamentoProducao,
    PreBaixaMP,
    Receita,
    ReceitaIngrediente,
)
from app.services import auto_pedidos, previsao_producao, producao
from app.utils import hoje


@pytest.fixture(autouse=True)
def segunda(congela_hoje):
    congela_hoje()


def receita(nome, *, azeitonas=False, sub=None):
    rec = Receita(nome=nome, peso_base=1000, peso_unitario=500,
                  rendimento_qtd=2, rendimento_unidade='un')
    rec.ingredientes.append(ReceitaIngrediente(
        tipo='mp', ingrediente_nome='Farinha', porcentagem=100))
    if azeitonas:
        rec.ingredientes.append(ReceitaIngrediente(
            tipo='mp', ingrediente_nome='azeitonas', porcentagem=10))
    if sub is not None:
        rec.ingredientes.append(ReceitaIngrediente(
            tipo='receita', ingrediente_nome=sub.nome, sub_receita=sub,
            porcentagem=1))
    db.session.add(rec)
    db.session.flush()
    return rec


def cenario():
    mp = MateriaPrima(nome='Farinha', unidade='g', custo_por_kg=1,
                      estoque_atual=1000000)
    db.session.add(mp)
    bom = receita('Cookie independente')
    ruim = receita('Sourdough Nozes e Azeitonas', azeitonas=True)
    db.session.commit()
    return mp, bom, ruim


def cronograma(monkeypatch, *linhas):
    crono = {'receitas': [
        {'receita_id': rec.id, 'por_dia': [
            {'data': (hoje() + timedelta(days=offset)).isoformat(), 'qtd': qtd}
            for offset, qtd in dias.items()]}
        for rec, dias in linhas]}
    chamadas = []

    def calcular(**kwargs):
        chamadas.append(kwargs)
        return deepcopy(crono)

    monkeypatch.setattr(previsao_producao, 'cronograma_producao', calcular)
    return chamadas


def planos():
    return {p.data: p for p in PlanejamentoProducao.query.all()}


def test_mp_sem_cadastro_desfaz_dia_mas_envia_dia_independente(app, monkeypatch):
    with app.app_context():
        mp, bom, ruim = cenario()
        chamadas = cronograma(monkeypatch, (bom, {1: 10, 2: 20}), (ruim, {1: 10}))
        out = auto_pedidos.enviar_ordens_da_semana()
        primeiro, segundo = hoje() + timedelta(days=1), hoje() + timedelta(days=2)
        assert primeiro not in planos()
        assert planos()[segundo].enviado_ao_padeiro
        assert [(i.receita_id, i.qtd_alvo) for i in planos()[segundo].itens] == [(bom.id, 20)]
        assert out['enviadas'] == [segundo.isoformat()]
        assert out['falhas'] == [{'data': primeiro.isoformat(),
            'receita_ids': [bom.id, ruim.id],
            'erro': 'Sourdough Nozes e Azeitonas: vincule a matéria-prima '
                    '"azeitonas" a um cadastro único e ativo.'}]
        assert json.loads(AppConfig.get(auto_pedidos.STATUS_ENVIO_PLANO_KEY)) == out
        assert len(chamadas) == 1
        # Somente o dia enviado reserva MP, jamais a aprovação que falhou.
        reservas = PreBaixaMP.query.all()
        assert {r.plano_id for r in reservas} == {planos()[segundo].id}
        assert mp.estoque_atual == 1000000 - sum(r.quantidade for r in reservas)


def test_falha_apos_aprovar_e_reservar_nao_deixa_rascunho_ou_movimento(app, monkeypatch):
    with app.app_context():
        mp, bom, _ruim = cenario()
        cronograma(monkeypatch, (bom, {1: 10, 2: 20}))
        original = producao.sincronizar_pre_baixa_mp

        def falhar(plano, user_id=None, criar=False):
            res = original(plano, user_id, criar)
            if plano.data == hoje() + timedelta(days=1):
                raise ValueError('Falha após calcular a reserva do primeiro dia.')
            return res

        monkeypatch.setattr(producao, 'sincronizar_pre_baixa_mp', falhar)
        out = auto_pedidos.enviar_ordens_da_semana()
        assert len(out['falhas']) == 1
        assert set(planos()) == {hoje() + timedelta(days=2)}
        assert {m.referencia for m in MovimentacaoEstoque.query.all()} == {
            'Pré-baixa produção ' + (hoje() + timedelta(days=2)).strftime('%d/%m')}
        assert mp.estoque_atual == 1000000 - sum(
            r.quantidade for r in PreBaixaMP.query.all())


def test_ressincronizacao_recusada_preserva_ordem_e_reserva_existentes(app, monkeypatch):
    with app.app_context():
        _mp, bom, ruim = cenario()
        dia = hoje() + timedelta(days=1)
        anterior = producao.enviar_plano_do_dia(dia, crono={
            'receitas': [{'receita_id': bom.id, 'por_dia': [
                {'data': dia.isoformat(), 'qtd': 5}]}]})
        id_anterior = anterior.id
        reserva_anterior = PreBaixaMP.query.filter_by(plano_id=id_anterior).one().quantidade
        cronograma(monkeypatch, (bom, {1: 50, 2: 20}), (ruim, {1: 10}))
        out = auto_pedidos.enviar_ordens_da_semana()
        assert out['falhas'][0]['data'] == dia.isoformat()
        preservado = planos()[dia]
        assert preservado.id == id_anterior and preservado.enviado_ao_padeiro
        assert [(i.receita_id, i.qtd_alvo) for i in preservado.itens] == [(bom.id, 5)]
        assert PreBaixaMP.query.filter_by(plano_id=id_anterior).one().quantidade == reserva_anterior
        assert out['enviadas'] == [(hoje() + timedelta(days=2)).isoformat()]


def test_falha_de_insumo_bloqueia_dependentes_diretos_e_indiretos(app, monkeypatch):
    with app.app_context():
        _mp, bom, ruim = cenario()
        massa = receita('Massa anterior')
        massa.dias_producao = 1
        filho = receita('Produto da massa', sub=massa)
        neto = receita('Montagem do produto', sub=filho)
        db.session.commit()
        cronograma(monkeypatch, (ruim, {1: 10}), (massa, {1: 10}),
                   (filho, {2: 10}), (neto, {3: 10}), (bom, {4: 10}))
        out = auto_pedidos.enviar_ordens_da_semana()
        dias = [(hoje() + timedelta(days=n)).isoformat() for n in range(1, 5)]
        assert [f['data'] for f in out['falhas']] == dias[:3]
        assert out['falhas'][1]['bloqueada_por'] == [dias[0]]
        assert out['falhas'][2]['bloqueada_por'] == dias[:2]
        assert out['enviadas'] == [dias[3]]
        assert set(planos()) == {hoje() + timedelta(days=4)}


@pytest.mark.parametrize('existente_hoje', [False, True])
def test_recuperar_so_ausentes_preserva_hoje_enviados_e_rascunho_humano(
        app, admin_user, monkeypatch, existente_hoje):
    with app.app_context():
        _mp, bom, _ruim = cenario()
        protegidos = [(1, None, True), (2, admin_user.id, False)]
        if existente_hoje:
            protegidos.append((0, None, False))
        for offset, autor, enviado in protegidos:
            db.session.add(PlanejamentoProducao(
                data=hoje() + timedelta(days=offset), origem='cronograma',
                nome='Intocável', criado_por=autor, enviado_ao_padeiro=enviado))
        db.session.commit()
        cronograma(monkeypatch, (bom, {0: 10, 1: 10, 2: 10, 3: 10}))
        out = auto_pedidos.enviar_ordens_da_semana(
            incluir_hoje=True, somente_pendentes=True)
        for offset, autor, enviado in protegidos:
            p = planos()[hoje() + timedelta(days=offset)]
            assert p.nome == 'Intocável'
            assert p.criado_por == autor and p.enviado_ao_padeiro is enviado
            assert not p.itens
            assert p.data.isoformat() in out['puladas']
        assert planos()[hoje() + timedelta(days=3)].enviado_ao_padeiro
        if not existente_hoje:
            assert planos()[hoje()].enviado_ao_padeiro
            assert hoje().isoformat() in out['enviadas']


def test_corrigir_cadastro_e_repetir_limpa_falha_sem_duplicar_ordens(app, monkeypatch):
    with app.app_context():
        _mp, bom, ruim = cenario()
        cronograma(monkeypatch, (ruim, {1: 10}), (bom, {2: 10}))
        primeiro = auto_pedidos.enviar_ordens_da_semana(somente_pendentes=True)
        assert primeiro['falhas']
        id_anterior = planos()[hoje() + timedelta(days=2)].id
        db.session.add(MateriaPrima(nome='Azeitonas', unidade='g', custo_por_kg=2,
                                    estoque_atual=100000))
        db.session.commit()
        segundo = auto_pedidos.enviar_ordens_da_semana(somente_pendentes=True)
        assert not segundo['falhas']
        assert set(planos()) == {hoje() + timedelta(days=1), hoje() + timedelta(days=2)}
        assert planos()[hoje() + timedelta(days=2)].id == id_anterior
        assert not json.loads(AppConfig.get(auto_pedidos.STATUS_ENVIO_PLANO_KEY))['falhas']


def test_rodada_preserva_falhas_de_hoje_e_de_ordens_enviadas_puladas(app, monkeypatch):
    with app.app_context():
        _mp, bom, _ruim = cenario()
        enviado = hoje() + timedelta(days=1)
        fora = hoje() + timedelta(days=10)
        db.session.add(PlanejamentoProducao(data=enviado, origem='cronograma',
                                           enviado_ao_padeiro=True))
        falhas = [{'data': d.isoformat(), 'erro': 'Não resolvido', 'receita_ids': [bom.id]}
                  for d in [hoje(), enviado, fora]]
        AppConfig.set(auto_pedidos.STATUS_ENVIO_PLANO_KEY, json.dumps({'falhas': falhas}))
        db.session.commit()
        cronograma(monkeypatch, (bom, {1: 10, 2: 10}))
        out = auto_pedidos.enviar_ordens_da_semana(somente_pendentes=True)
        assert out['falhas'] == falhas
        assert enviado.isoformat() in out['puladas']


def test_preparo_falho_continua_bloqueando_apos_virada_do_dia(app, monkeypatch, congela_hoje):
    with app.app_context():
        _mp, _bom, ruim = cenario()
        massa = receita('Massa anterior')
        massa.dias_producao = 1
        filho = receita('Produto dependente', sub=massa)
        db.session.commit()
        dia_falho = hoje() + timedelta(days=1)
        dia_filho = hoje() + timedelta(days=2)
        cronograma(monkeypatch, (massa, {1: 10}), (ruim, {1: 10}), (filho, {2: 10}))
        primeiro = auto_pedidos.enviar_ordens_da_semana()
        assert primeiro['falhas'][1]['bloqueada_por'] == [dia_falho.isoformat()]
        # Agora o preparo falho já é ontem. Nem o cron nem a recuperação
        # explícita podem converter passagem do tempo em massa produzida.
        congela_hoje(2026, 8, 19)
        cronograma(monkeypatch, (filho, {0: 10, 1: 10}))
        segundo = auto_pedidos.enviar_ordens_da_semana(
            incluir_hoje=True, somente_pendentes=True)
        por_dia = {f['data']: f for f in segundo['falhas']}
        assert dia_falho.isoformat() in por_dia
        assert por_dia[dia_filho.isoformat()]['bloqueada_por'] == [dia_falho.isoformat()]
        assert not planos()


def test_dependencia_do_snapshot_bloqueia_ressincronizacao_apos_editar_ficha(
        app, monkeypatch):
    with app.app_context():
        _mp, _bom, ruim = cenario()
        massa = receita('Massa anterior')
        massa.dias_producao = 1
        filho = receita('Sourdough Tradicional', sub=massa)
        db.session.commit()
        dia_filho = hoje() + timedelta(days=3)
        anterior = producao.enviar_plano_do_dia(dia_filho, crono={'receitas': [
            {'receita_id': filho.id, 'por_dia': [
                {'data': dia_filho.isoformat(), 'qtd': 10}]}]})
        item = anterior.itens[0]
        alvo = item.qtd_alvo
        snapshot = deepcopy(item.batelada_padrao.dados)
        assert snapshot['subs'][0]['id'] == massa.id
        filho.ingredientes[:] = [i for i in filho.ingredientes if i.tipo != 'receita']
        db.session.commit()
        crono = {'receitas': [
            {'receita_id': massa.id, 'por_dia': [
                {'data': (hoje() + timedelta(days=1)).isoformat(), 'qtd': 10}]},
            {'receita_id': ruim.id, 'por_dia': [
                {'data': (hoje() + timedelta(days=1)).isoformat(), 'qtd': 10}]},
            {'receita_id': filho.id, 'por_dia': [
                {'data': dia_filho.isoformat(), 'qtd': 900,
                 'batelada_padrao': snapshot}]}]}
        monkeypatch.setattr(previsao_producao, 'cronograma_producao', lambda **kw: crono)
        out = auto_pedidos.enviar_ordens_da_semana()
        assert out['falhas'][-1]['data'] == dia_filho.isoformat()
        assert out['falhas'][-1]['bloqueada_por'] == [
            (hoje() + timedelta(days=1)).isoformat()]
        assert planos()[dia_filho].itens[0].qtd_alvo == alvo
        assert not out['resincronizadas']


@pytest.mark.parametrize(('quantidades', 'libera'), [([10, 10], True), ([10, 1], False)])
def test_preparo_novo_substitui_falha_antiga_so_se_suficiente_e_com_antecedencia(
        app, monkeypatch, quantidades, libera):
    with app.app_context():
        _mp, _bom, _ruim = cenario()
        massa = receita('Massa anterior')
        massa.dias_producao = 1
        filho = receita('Produto dependente', sub=massa)
        antiga = (hoje() - timedelta(days=1)).isoformat()
        AppConfig.set(auto_pedidos.STATUS_ENVIO_PLANO_KEY, json.dumps({'falhas': [
            {'data': antiga, 'erro': 'Preparo não enviado', 'receita_ids': [massa.id]}]}))
        db.session.commit()
        datas = [(hoje() + timedelta(days=n)).isoformat() for n in (1, 2, 3)]
        crono = {'receitas': [
            {'receita_id': massa.id, '_necessidade_insumo_por_dia': [10, 10],
             'por_dia': [{'data': d, 'qtd': q} for d, q in zip(datas, quantidades)]},
            {'receita_id': filho.id, 'por_dia': [{'data': datas[2], 'qtd': 20}]}]}
        monkeypatch.setattr(previsao_producao, 'cronograma_producao', lambda **kw: crono)
        out = auto_pedidos.enviar_ordens_da_semana(somente_pendentes=True)
        assert (datas[2] in out['enviadas']) is libera
        if libera:
            prova = out['falhas'][0]['preparos_substituidos'][str(massa.id)]
            assert prova['data'] == datas[1]
            assert prova['quantidade'] == prova['necessidade'] == 20
            assert len(prova['ordens']) == 2
            # Uma nova rodada não apaga a prova de envio nem recria o erro.
            repetido = auto_pedidos.enviar_ordens_da_semana()
            assert datas[2] in repetido['resincronizadas']
        else:
            assert out['falhas'][-1]['bloqueada_por'] == [antiga]


def test_preparo_pequeno_nao_libera_consumo_acumulado_maior_no_mesmo_cronograma(
        app, monkeypatch):
    with app.app_context():
        _mp, _bom, _ruim = cenario()
        massa = receita('Massa anterior')
        massa.dias_producao = 1
        filho = receita('Produto dependente', sub=massa)
        antiga = (hoje() - timedelta(days=1)).isoformat()
        AppConfig.set(auto_pedidos.STATUS_ENVIO_PLANO_KEY, json.dumps({'falhas': [
            {'data': antiga, 'erro': 'Preparo não enviado', 'receita_ids': [massa.id]}]}))
        db.session.commit()
        datas = [(hoje() + timedelta(days=n)).isoformat() for n in (1, 2, 3)]
        crono = {'receitas': [
            {'receita_id': massa.id, '_necessidade_insumo_por_dia': [10, 10],
             'por_dia': [{'data': datas[0], 'qtd': 10}, {'data': datas[1], 'qtd': 0}]},
            {'receita_id': filho.id, 'por_dia': [
                {'data': datas[1], 'qtd': 20}, {'data': datas[2], 'qtd': 20}]}]}
        monkeypatch.setattr(previsao_producao, 'cronograma_producao', lambda **kw: crono)
        out = auto_pedidos.enviar_ordens_da_semana()
        assert datas[1] in out['enviadas']
        assert datas[2] not in out['enviadas']
        assert out['falhas'][-1]['bloqueada_por'] == [antiga]


@pytest.mark.parametrize('invalidacao', [
    'excluido', 'produzido', 'reduzido', 'dispensado', 'encerrado',
    'outra_janela', 'novo_consumo',
])
def test_prova_nao_reutiliza_preparo_ausente_consumido_ou_de_outra_janela(
        app, monkeypatch, congela_hoje, invalidacao):
    with app.app_context():
        cenario()
        massa = receita('Massa substituta')
        massa.dias_producao = 1
        filho = receita('Produto dependente', sub=massa)
        antiga = (hoje() - timedelta(days=1)).isoformat()
        AppConfig.set(auto_pedidos.STATUS_ENVIO_PLANO_KEY, json.dumps({'falhas': [
            {'data': antiga, 'erro': 'Preparo não enviado', 'receita_ids': [massa.id]}]}))
        db.session.commit()
        dia_preparo = hoje() + timedelta(days=1)
        dia_filho = hoje() + timedelta(days=2)
        crono = {'receitas': [
            {'receita_id': massa.id, '_necessidade_insumo_por_dia': [20],
             'por_dia': [{'data': dia_preparo.isoformat(), 'qtd': 20}]},
            {'receita_id': filho.id, 'por_dia': [
                {'data': dia_filho.isoformat(), 'qtd': 20}]}]}
        monkeypatch.setattr(previsao_producao, 'cronograma_producao', lambda **kw: crono)
        primeiro = auto_pedidos.enviar_ordens_da_semana(somente_pendentes=True)
        assert dia_filho.isoformat() in primeiro['enviadas']
        fonte = planos()[dia_preparo]
        db.session.delete(planos()[dia_filho])
        if invalidacao == 'excluido':
            db.session.delete(fonte)
        elif invalidacao == 'produzido':
            fonte.itens[0].produzido_qtd = fonte.itens[0].qtd_alvo
        elif invalidacao == 'reduzido':
            fonte.itens[0].qtd_alvo = 1
        elif invalidacao in ('dispensado', 'encerrado'):
            from app.utils import agora
            campo = 'dispensada_em' if invalidacao == 'dispensado' else 'falta_encerrada_em'
            setattr(fonte.itens[0], campo, agora())
        elif invalidacao == 'outra_janela':
            congela_hoje(dia_preparo.year, dia_preparo.month, dia_preparo.day)
        else:
            dia_filho += timedelta(days=1)
            crono['receitas'][1]['por_dia'][0]['data'] = dia_filho.isoformat()
        crono['receitas'][0]['por_dia'][0]['qtd'] = 0
        db.session.commit()
        segundo = auto_pedidos.enviar_ordens_da_semana(somente_pendentes=True)
        assert dia_filho.isoformat() not in segundo['enviadas']
        assert dia_filho not in planos()
        falha = next(f for f in segundo['falhas'] if f['data'] == dia_filho.isoformat())
        assert falha['bloqueada_por'] == [antiga]
