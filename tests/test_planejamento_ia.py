"""Planejamento assistido por IA (Opus 4.8, 08/07/2026): a IA propõe por
cima dos motores determinísticos — pedido da loja (grade da média) e
ajustes de célula do cronograma. A Anthropic é SEMPRE mockada; o que
estes testes travam é a SANITIZAÇÃO contra o banco/motor real e o
caminho de aplicar (override de rascunho, nunca envio ao padeiro).
"""
import json
from datetime import timedelta
from unittest.mock import MagicMock, patch

import pytest

from app.extensions import db
from app.models import EstoqueLoja, Loja, PedidoItem, PedidoLoja, Receita
from app.services import planejamento_ia as svc
from app.utils import hoje


@pytest.fixture(autouse=True)
def _hoje_e_segunda_fixa(congela_hoje):
    """Producao seg-sex + janela semanal tornaram o motor weekday-sensivel
    — congela numa SEGUNDA fixa (mesma fixture dos arquivos do cronograma;
    caso real 19/08/2026: test_cronograma_edit quebrou na QUARTA porque o
    indice 3 do grid caiu no sabado bloqueado)."""
    congela_hoje()



class _FakeBlock:
    type = 'text'

    def __init__(self, text):
        self.text = text


def _fake_client(payload):
    client = MagicMock()
    resp = MagicMock()
    resp.content = [_FakeBlock(json.dumps(payload))]
    resp.usage = None
    client.messages.create.return_value = resp
    return client


def _receita(nome='Croissant'):
    r = Receita(nome=nome, categoria='Croissants', rendimento_qtd=1,
                rendimento_unidade='un', peso_base=100.0)
    db.session.add(r)
    db.session.commit()
    return r


def _loja(nome='Loja A'):
    loja = Loja(nome=nome, ativa=True)
    db.session.add(loja)
    db.session.commit()
    return loja


def _pedido(loja, data_entrega, receita, qtd, status='recebido'):
    p = PedidoLoja(loja_id=loja.id, status=status, data_entrega=data_entrega,
                   data_pedido=data_entrega)
    db.session.add(p)
    db.session.flush()
    db.session.add(PedidoItem(pedido_id=p.id, receita_id=receita.id,
                              quantidade=qtd))
    db.session.commit()
    return p


def _historico_semanal(loja, receita, qtd=10, semanas=4):
    """Pedidos recebidos nas últimas N semanas (mesmo dia-da-semana de
    amanhã) — garante linha na grade da média para amanhã."""
    amanha = hoje() + timedelta(days=1)
    for k in range(1, semanas + 1):
        _pedido(loja, amanha - timedelta(days=7 * k), receita, qtd)


def test_pedido_loja_ia_sanitiza(app, monkeypatch):
    """Receita fora da grade é descartada; por_dia é ajustado ao tamanho
    do horizonte com inteiros >= 0; proposta >3x o motor ganha aviso."""
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'sk-teste')
    with app.app_context():
        loja = _loja()
        r = _receita()
        _historico_semanal(loja, r, qtd=10)
        db.session.add(EstoqueLoja(loja_id=loja.id, receita_id=r.id,
                                   quantidade=5))
        db.session.commit()
        payload = {'itens': [
            {'receita_id': r.id,
             'por_dia': [99, -3, 'x', 99, 99, 99, 99],
             'motivo': 'feriado na quinta'},
            {'receita_id': 99999, 'por_dia': [1] * 7, 'motivo': 'fantasma'},
        ], 'parecer': 'semana de feriado'}
        with patch('anthropic.Anthropic',
                   return_value=_fake_client(payload)):
            out = svc.sugerir_pedido_loja_ia(loja.id, horizonte_dias=7)
    assert 'erro' not in out
    assert len(out['itens']) == 1                     # fantasma caiu
    it = out['itens'][0]
    assert it['receita_id'] == r.id
    assert len(it['por_dia']) == 7
    assert all(isinstance(v, int) and v >= 0 for v in it['por_dia'])
    assert it['por_dia'][1] == 0                      # -3 clampado? nao:
    # -3 vira max(0, -3) = 0; 'x' invalido cai no valor do motor
    assert it['aviso'] and '3x' in it['aviso']
    assert out['parecer'] == 'semana de feriado'


def test_pedido_loja_ia_nao_mexe_em_dia_travado(app, monkeypatch):
    """Dia que JÁ TEM pedido devolve o valor já pedido — a IA não pode
    propor mudança em pedido existente por esta via."""
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'sk-teste')
    with app.app_context():
        loja = _loja()
        r = _receita()
        _historico_semanal(loja, r, qtd=10)
        amanha = hoje() + timedelta(days=1)
        _pedido(loja, amanha, r, 12, status='confirmado')   # trava amanhã
        db.session.commit()
        payload = {'itens': [
            {'receita_id': r.id, 'por_dia': [50] * 7, 'motivo': 'x'},
        ], 'parecer': ''}
        with patch('anthropic.Anthropic',
                   return_value=_fake_client(payload)):
            out = svc.sugerir_pedido_loja_ia(loja.id, horizonte_dias=7,
                                             inicio_offset_dias=1)
    it = out['itens'][0]
    # dia 0 do horizonte = amanhã (travado): devolve o já pedido (12)
    assert it['por_dia'][0] == 12


def test_pedido_loja_ia_modo_venda_usa_item_key(app, monkeypatch):
    """Modo 'venda' (tela /pedidos-semana/estoque, 11/07/2026): a proposta
    é casada por item_key contra a grade de VENDA+ESTOQUE — item fantasma
    cai, por_dia é saneado igual ao modo média."""
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'sk-teste')
    with app.app_context():
        loja = _loja()
        r = _receita()
        _historico_semanal(loja, r, qtd=10)
        db.session.add(EstoqueLoja(loja_id=loja.id, receita_id=r.id,
                                   quantidade=5))
        db.session.commit()
        payload = {'itens': [
            {'item_key': str(r.id), 'por_dia': [7, -1, 'x', 7, 7, 7, 7],
             'motivo': 'feriado na quinta'},
            {'item_key': 'mp:99999', 'por_dia': [1] * 7,
             'motivo': 'fantasma'},
        ], 'parecer': 'semana de feriado'}
        with patch('anthropic.Anthropic',
                   return_value=_fake_client(payload)):
            out = svc.sugerir_pedido_loja_ia(loja.id, horizonte_dias=7,
                                             modo='venda')
    assert 'erro' not in out
    assert len(out['itens']) == 1                     # fantasma caiu
    it = out['itens'][0]
    assert it['item_key'] == str(r.id)
    assert len(it['por_dia']) == 7
    assert all(isinstance(v, int) and v >= 0 for v in it['por_dia'])
    assert out['parecer'] == 'semana de feriado'


def test_pedido_loja_ia_modo_venda_aceita_mp(app, monkeypatch):
    """A grade de venda+estoque inclui MPs pedíveis (item_key 'mp:<id>') —
    a proposta da IA para uma MP passa na sanitização."""
    from app.models import MateriaPrima
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'sk-teste')
    with app.app_context():
        loja = _loja()
        mp = MateriaPrima(nome='Pao de queijo congelado', unidade='un',
                          custo_por_kg=10.0, sugerir_pedido_loja=True)
        db.session.add(mp)
        db.session.flush()
        db.session.add(EstoqueLoja(loja_id=loja.id,
                                   materia_prima_id=mp.id, quantidade=3))
        db.session.commit()
        chave = f'mp:{mp.id}'
        payload = {'itens': [
            {'item_key': chave, 'por_dia': [4] * 7, 'motivo': 'reforço'},
        ], 'parecer': ''}
        with patch('anthropic.Anthropic',
                   return_value=_fake_client(payload)):
            out = svc.sugerir_pedido_loja_ia(loja.id, horizonte_dias=7,
                                             modo='venda')
    assert 'erro' not in out
    assert any(it['item_key'] == chave for it in out['itens'])


def test_pedido_loja_ia_loja_sem_grade(app, monkeypatch):
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'sk-teste')
    with app.app_context():
        loja = _loja('Loja Nova')
        out = svc.sugerir_pedido_loja_ia(loja.id)
    assert 'erro' in out


def test_producao_ia_sanitiza_e_descarta_igual(app, monkeypatch):
    """Ajuste com receita/data inexistente cai fora; ajuste igual ao valor
    atual é descartado; qtd negativa vira 0."""
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'sk-teste')
    with app.app_context():
        loja = _loja()
        r = _receita('Baguete')
        alvo = hoje() + timedelta(days=3)
        _pedido(loja, alvo, r, 40, status='pendente')   # firme futuro
        from app.services.previsao_producao import cronograma_producao
        crono = cronograma_producao(horizonte_dias=7, inicio_offset_dias=0)
        linha = next(x for x in crono['receitas']
                     if x['receita_id'] == r.id)
        cel = next(c for c in linha['por_dia'] if c['qtd'])
        payload = {'ajustes': [
            {'receita_id': r.id, 'data': cel['data'],
             'qtd': cel['qtd'] + 15, 'motivo': 'vespera de feriado'},
            {'receita_id': r.id, 'data': cel['data'],
             'qtd': cel['qtd'], 'motivo': 'igual — deve cair'},
            {'receita_id': r.id, 'data': '2099-01-01', 'qtd': 5,
             'motivo': 'fora do horizonte'},
            {'receita_id': 98765, 'data': cel['data'], 'qtd': 5,
             'motivo': 'fantasma'},
        ], 'parecer': 'reforço pré-feriado'}
        with patch('anthropic.Anthropic',
                   return_value=_fake_client(payload)):
            out = svc.analisar_producao_ia(horizonte_dias=7,
                                           inicio_offset_dias=0)
    assert 'erro' not in out
    assert len(out['ajustes']) == 1
    aj = out['ajustes'][0]
    assert aj['receita_id'] == r.id and aj['data'] == cel['data']
    assert aj['atual'] == cel['qtd'] and aj['qtd'] == cel['qtd'] + 15
    assert out['parecer'] == 'reforço pré-feriado'


def test_producao_ia_avisa_linha_zerada(app, monkeypatch):
    """Produto que o motor de média não sugere (linha zerada) mas a IA
    propõe quantidade ganha aviso — não passa despercebido."""
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'sk-teste')
    with app.app_context():
        loja = _loja()
        r = _receita()
        # histórico raso: cria linha na grade porém com média baixa/zerada
        # em vários dias — garante ao menos um item na grade da loja.
        _historico_semanal(loja, r, qtd=0)
        _pedido(loja, hoje() - timedelta(days=7), r, 0)
        payload = {'itens': [
            {'receita_id': r.id, 'por_dia': [30, 30, 30, 30, 30, 30, 30],
             'motivo': 'evento na loja'},
        ], 'parecer': ''}
        with patch('anthropic.Anthropic',
                   return_value=_fake_client(payload)):
            out = svc.sugerir_pedido_loja_ia(loja.id, horizonte_dias=7)
    if out.get('itens'):
        it = out['itens'][0]
        assert it['aviso'] is not None


def test_pedido_loja_ia_erro_generico(app, monkeypatch):
    """Falha da API vira mensagem amigável (sem vazar detalhe do SDK)."""
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'sk-teste')
    with app.app_context():
        loja = _loja()
        r = _receita()
        _historico_semanal(loja, r, qtd=10)
        client = MagicMock()
        client.messages.create.side_effect = RuntimeError(
            'segredo-interno-do-sdk')
        with patch('anthropic.Anthropic', return_value=client):
            out = svc.sugerir_pedido_loja_ia(loja.id)
    assert 'erro' in out
    assert 'segredo-interno' not in out['erro']


def test_rota_ia_aplicar_data_malformada_nao_da_500(app, admin_user):
    """Ajuste com data inválida entra em falhas — nunca 500 no meio do
    loop deixando ajustes anteriores commitados sem relatório."""
    from app.models import CronogramaOverride
    with app.app_context():
        loja = _loja()
        r = _receita('Baguete')
        alvo = hoje() + timedelta(days=3)
        _pedido(loja, alvo, r, 40, status='pendente')
        from app.services.previsao_producao import cronograma_producao
        crono = cronograma_producao(horizonte_dias=7, inicio_offset_dias=0)
        linha = next(x for x in crono['receitas'] if x['receita_id'] == r.id)
        cel = next(c for c in linha['por_dia'] if c['qtd'])
        rid, data_ok, qtd_nova = r.id, cel['data'], cel['qtd'] + 8
    c = app.test_client()
    c.post('/auth/login', data={'login': 'admin', 'senha': '123'})
    resp = c.post('/telaindustriateste/ia-aplicar', json={
        'horizonte': 7, 'janela': 6, 'inicio': 0,
        'ajustes': [
            {'receita_id': rid, 'data': '2026-99-99', 'qtd': 5},   # ruim
            {'receita_id': rid, 'data': data_ok, 'qtd': qtd_nova},  # ok
        ],
    })
    assert resp.status_code == 200
    d = resp.get_json()
    assert d['ok'] is True
    assert len(d['aplicados']) == 1 and len(d['falhas']) == 1
    assert d['falhas'][0]['erro'] == 'parametros'
    with app.app_context():
        assert any(o.qtd == qtd_nova
                   for o in CronogramaOverride.query.filter_by(
                       receita_id=rid).all())


def test_rota_ia_aplicar_vira_override_rascunho(app, admin_user):
    """Aplicar os ajustes grava CronogramaOverride (rascunho) via
    editar_celula — e NUNCA aprova/envia plano (gesto humano)."""
    from app.models import CronogramaOverride, PlanejamentoProducao
    with app.app_context():
        loja = _loja()
        r = _receita('Baguete')
        alvo = hoje() + timedelta(days=3)
        _pedido(loja, alvo, r, 40, status='pendente')
        from app.services.previsao_producao import cronograma_producao
        crono = cronograma_producao(horizonte_dias=7, inicio_offset_dias=0)
        linha = next(x for x in crono['receitas']
                     if x['receita_id'] == r.id)
        cel = next(c for c in linha['por_dia'] if c['qtd'])
        rid, data_cel, qtd_nova = r.id, cel['data'], cel['qtd'] + 10
    c = app.test_client()
    c.post('/auth/login', data={'login': 'admin', 'senha': '123'})
    resp = c.post('/telaindustriateste/ia-aplicar', json={
        'horizonte': 7, 'janela': 6, 'inicio': 0,
        'ajustes': [
            {'receita_id': rid, 'data': data_cel, 'qtd': qtd_nova},
            {'receita_id': 43210, 'data': data_cel, 'qtd': 5},   # falha
        ],
    })
    d = resp.get_json()
    assert d['ok'] is True
    assert len(d['aplicados']) == 1 and len(d['falhas']) == 1
    with app.app_context():
        ov = CronogramaOverride.query.filter_by(receita_id=rid).all()
        assert any(o.qtd == qtd_nova for o in ov)
        assert PlanejamentoProducao.query.count() == 0   # nada aprovado


def test_rota_pedidos_semana_ia(app, admin_user, monkeypatch):
    """Rota da grade: devolve a proposta em JSON pro JS preencher."""
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'sk-teste')
    with app.app_context():
        loja = _loja()
        r = _receita()
        _historico_semanal(loja, r, qtd=10)
        loja_id, rid = loja.id, r.id
    c = app.test_client()
    c.post('/auth/login', data={'login': 'admin', 'senha': '123'})
    payload = {'itens': [{'receita_id': rid, 'por_dia': [11] * 7,
                          'motivo': 'ok'}], 'parecer': 'tudo certo'}
    with patch('anthropic.Anthropic', return_value=_fake_client(payload)):
        resp = c.post('/producao/pedidos-semana/ia',
                      json={'loja_id': loja_id, 'horizonte': 7,
                            'janela': 6, 'inicio': 1})
    d = resp.get_json()
    assert d['ok'] is True
    assert d['itens'][0]['receita_id'] == rid
    assert d['parecer'] == 'tudo certo'
    assert len(d['dias']) == 7


def test_rota_pedidos_semana_ia_modo_venda(app, admin_user, monkeypatch):
    """Rota com modo='venda': proposta casada por item_key + a tela de
    venda+estoque tem o botão Sugerir por IA."""
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'sk-teste')
    with app.app_context():
        loja = _loja()
        r = _receita()
        _historico_semanal(loja, r, qtd=10)
        loja_id, rid = loja.id, r.id
    c = app.test_client()
    c.post('/auth/login', data={'login': 'admin', 'senha': '123'})
    payload = {'itens': [{'item_key': str(rid), 'por_dia': [9] * 7,
                          'motivo': 'ok'}], 'parecer': 'ajustado'}
    with patch('anthropic.Anthropic', return_value=_fake_client(payload)):
        resp = c.post('/producao/pedidos-semana/ia',
                      json={'loja_id': loja_id, 'horizonte': 7,
                            'janela': 6, 'inicio': 1, 'modo': 'venda',
                            'seguranca': 20})
    d = resp.get_json()
    assert d['ok'] is True
    assert d['itens'][0]['item_key'] == str(rid)
    corpo = c.get('/producao/pedidos-semana/estoque').get_data(as_text=True)
    assert 'btn-ia-loja' in corpo
    assert "'venda'" in corpo or '"venda"' in corpo


def test_rotas_ia_exigem_admin(app):
    from app.models import Usuario
    with app.app_context():
        u = Usuario(nome='Func', login='func', papel='funcionario')
        u.set_senha('12345678')
        db.session.add(u)
        db.session.commit()
    c = app.test_client()
    c.post('/auth/login', data={'login': 'func', 'senha': '12345678'})
    assert c.post('/producao/pedidos-semana/ia',
                  json={'loja_id': 1}).status_code == 403
    assert c.post('/telaindustriateste/ia-proposta',
                  json={}).status_code == 403
    assert c.post('/telaindustriateste/ia-aplicar',
                  json={'ajustes': [{}]}).status_code == 403


def test_sem_api_key(app, monkeypatch):
    monkeypatch.delenv('ANTHROPIC_API_KEY', raising=False)
    with app.app_context():
        loja = _loja()
        r = _receita()
        _historico_semanal(loja, r)
        out = svc.sugerir_pedido_loja_ia(loja.id)
    assert 'ANTHROPIC_API_KEY' in out['erro']
