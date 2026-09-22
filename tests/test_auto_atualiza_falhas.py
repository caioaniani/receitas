"""A atualização de 06:45/19:05 não perde dias nem ignora preparos falhos."""
import json
from datetime import timedelta

from app.extensions import db
from app.models import AppConfig, MateriaPrima, PlanejamentoProducao, Receita, ReceitaIngrediente
from app.services import auto_pedidos, previsao_producao, producao
from app.services.auto_envio_status import falhas_atuais
from app.utils import hoje


def _receita(nome, sub=None):
    r = Receita(nome=nome, peso_base=1000, peso_unitario=500,
                rendimento_qtd=2, rendimento_unidade='un')
    r.ingredientes.append(ReceitaIngrediente(
        tipo='mp', ingrediente_nome='Farinha', porcentagem=100))
    if sub is not None:
        r.ingredientes.append(ReceitaIngrediente(
            tipo='receita', ingrediente_nome=sub.nome, sub_receita=sub, porcentagem=1))
    db.session.add(r)
    db.session.flush()
    return r


def _crono(*linhas):
    return {'receitas': [{'receita_id': r.id, 'por_dia': [
        {'data': (hoje() + timedelta(days=n)).isoformat(), 'qtd': q}
        for n, q in dias.items()]} for r, dias in linhas]}


def _preparar():
    db.session.add(MateriaPrima(nome='Farinha', unidade='g', custo_por_kg=1,
                                estoque_atual=1000000))
    bom = _receita('Cookie independente')
    ruim = _receita('Sourdough inválido')
    db.session.commit()
    for n in (1, 2):
        producao.enviar_plano_do_dia(hoje() + timedelta(days=n),
                                     crono=_crono((bom, {n: 5})))
    ruim.ingredientes.append(ReceitaIngrediente(
        tipo='mp', ingrediente_nome='azeitonas', porcentagem=10))
    db.session.commit()
    return bom, ruim


def test_atualizacao_falha_primeiro_dia_preserva_e_atualiza_independente(
        app, monkeypatch, congela_hoje):
    congela_hoje()
    with app.app_context():
        bom, ruim = _preparar()
        crono = _crono((bom, {1: 50, 2: 20}), (ruim, {1: 10}))
        monkeypatch.setattr(previsao_producao, 'cronograma_producao', lambda **kw: crono)
        out = auto_pedidos.atualizar_plano_automatico()
        planos = {p.data: p for p in PlanejamentoProducao.query.all()}
        primeiro, segundo = [hoje() + timedelta(days=n) for n in (1, 2)]
        assert [(i.receita_id, i.qtd_alvo) for i in planos[primeiro].itens] == [(bom.id, 5)]
        assert [(i.receita_id, i.qtd_alvo) for i in planos[segundo].itens] == [(bom.id, 20)]
        assert out['atualizadas'] == [{'data': segundo.isoformat(), 'itens': 1}]
        assert out['falhas'][0]['data'] == primeiro.isoformat()
        assert 'azeitonas' in out['falhas'][0]['erro']
        assert falhas_atuais()[0]['receita_id'] == ruim.id
        assert json.loads(AppConfig.get(auto_pedidos.STATUS_ENVIO_PLANO_KEY)) == out
        # Corrigir e repetir atualiza sem duplicar e limpa o aviso confirmado.
        ids = {p.id for p in planos.values()}
        db.session.add(MateriaPrima(nome='Azeitonas', unidade='g', custo_por_kg=1,
                                    estoque_atual=100000))
        db.session.commit()
        assert not auto_pedidos.atualizar_plano_automatico()['falhas']
        assert {p.id for p in PlanejamentoProducao.query.all()} == ids
        assert falhas_atuais() == []


def test_atualizacao_preserva_dependente_de_preparo_falho(app, monkeypatch, congela_hoje):
    congela_hoje()
    with app.app_context():
        bom, ruim = _preparar()
        massa = _receita('Massa anterior')
        massa.dias_producao = 1
        filho = _receita('Produto da massa', sub=massa)
        db.session.commit()
        crono = _crono((massa, {1: 10}), (ruim, {1: 10}), (filho, {2: 20}))
        monkeypatch.setattr(previsao_producao, 'cronograma_producao', lambda **kw: crono)
        out = auto_pedidos.atualizar_plano_automatico()
        primeiro, segundo = [(hoje() + timedelta(days=n)).isoformat() for n in (1, 2)]
        assert out['falhas'][1]['data'] == segundo
        assert out['falhas'][1]['bloqueada_por'] == [primeiro]
        assert not out['atualizadas']
        assert all([(i.receita_id, i.qtd_alvo) for i in p.itens] == [(bom.id, 5)]
                   for p in PlanejamentoProducao.query.all())


def test_atualizacao_nao_esquece_preparo_falho_fora_da_janela(app, monkeypatch, congela_hoje):
    congela_hoje()
    with app.app_context():
        bom, _ruim = _preparar()
        massa = _receita('Massa não enviada ontem')
        massa.dias_producao = 1
        filho = _receita('Produto da massa', sub=massa)
        antiga = (hoje() - timedelta(days=1)).isoformat()
        AppConfig.set(auto_pedidos.STATUS_ENVIO_PLANO_KEY, json.dumps({'falhas': [
            {'data': antiga, 'erro': 'Preparo recusado', 'receita_ids': [massa.id]}]}))
        db.session.commit()
        monkeypatch.setattr(previsao_producao, 'cronograma_producao',
                            lambda **kw: _crono((filho, {1: 20}), (bom, {2: 20})))
        out = auto_pedidos.atualizar_plano_automatico()
        assert out['falhas'][0]['data'] == antiga
        assert out['falhas'][1]['bloqueada_por'] == [antiga]
        assert out['atualizadas'] == [
            {'data': (hoje() + timedelta(days=2)).isoformat(), 'itens': 1}]
