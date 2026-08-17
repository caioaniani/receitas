"""Pedidos automáticos loja→indústria + envio automático da ordem
(10/08/2026, decisão do dono: "faça os pedidos automaticamente de 3 dias na
frente" + automatizar o envio ao padeiro — revoga "enviar é gesto humano"
de 04/07/2026).

Duas camadas de teste, de propósito:
- MOTOR MOCKADO: contrato da materialização (rascunho sem autor, corte,
  respeito a humano) sem depender da matemática do motor.
- MOTOR REAL: a re-sincronização de verdade (a revisão de 13/08/2026 provou
  que só o mock "re-sincronizava" — o motor real devolvia 0 pra dia já
  pedido e a quantidade congelava na primeira criação).
"""
from datetime import datetime, timedelta

import pytest

from app.extensions import db
from app.models import EstoqueLoja, PedidoItem, PedidoLoja, Receita
from app.services import auto_pedidos, pedido_corte
from app.utils import hoje

MARCADOR = 'Gerado do histórico (rascunho) — revisar e confirmar.'


@pytest.fixture(autouse=True)
def _hoje_e_segunda_fixa(congela_hoje):
    """A janela dos pedidos automáticos virou 'amanhã..próximo domingo'
    (dono 17/08/2026) — dependente do dia da semana. Congela hoje() numa
    SEGUNDA (janela ter..dom, 6 dias) pra suíte não variar com o dia em que
    roda. Os testes da ordem semanal que precisam de outro dia monkeypatcham
    auto_pedidos.hoje por cima (vence o congelamento)."""
    congela_hoje()


def _login(client, user):
    with client.session_transaction() as sess:
        sess['_user_id'] = str(user.id)
        sess['_fresh'] = True


def _receita(nome='Pao Auto'):
    r = Receita(nome=nome, categoria='Paes', rendimento_qtd=1,
                rendimento_unidade='un', peso_base=100,
                sugerir_pedido_loja=True)
    db.session.add(r)
    db.session.commit()
    return r


def _as_10h(monkeypatch):
    """Congela o relógio do corte às 10:00 de hoje: os testes de
    materialização não são sobre o corte, e rodar a suíte no corte BRT
    real (>= HORA_CORTE) pularia o D+1 e quebraria o CI (deploy travado — mesma classe do
    caso do card do padeiro em 09/08)."""
    fake = datetime.combine(hoje(), datetime.min.time()).replace(hour=10)
    monkeypatch.setattr(pedido_corte, 'agora', lambda: fake)


def _sugestao(loja, receita, por_dia):
    """Mock do retorno de sugerir_pedidos_por_venda (forma real, enxuta)."""
    dias = [(hoje() + timedelta(days=1 + i)) for i in range(len(por_dia))]
    return {
        'dias': [{'data': d.isoformat()} for d in dias],
        'lojas': [{
            'loja_id': loja.id, 'loja_nome': loja.nome,
            'produtos': [{
                'receita_id': receita.id, 'materia_prima_id': None,
                'por_dia': por_dia,
            }],
        }],
    }


def _rascunho_auto(loja, itens, dias=1):
    """Cria na mão um rascunho como o cron cria (aplicar_grade user_id=None).

    itens: [(receita, qtd), ...]."""
    p = PedidoLoja(loja_id=loja.id, data_entrega=hoje() + timedelta(days=dias),
                   status='pendente', criado_por=None, observacao=MARCADOR)
    db.session.add(p)
    db.session.flush()
    for r, q in itens:
        db.session.add(PedidoItem(pedido_id=p.id, receita_id=r.id,
                                  quantidade=q))
    db.session.commit()
    return p


def test_cria_rascunhos_3_dias_sem_autor_humano(app, loja, monkeypatch):
    with app.app_context():
        _as_10h(monkeypatch)
        r = _receita()
        from app.services import previsao_producao
        monkeypatch.setattr(previsao_producao, 'sugerir_pedidos_por_venda',
                            lambda **kw: _sugestao(loja, r, [10, 20, 30]))
        out = auto_pedidos.gerar_pedidos_automaticos()
        assert out['criados'] == 3
        peds = (PedidoLoja.query.filter_by(loja_id=loja.id)
                .order_by(PedidoLoja.data_entrega).all())
        assert [p.data_entrega for p in peds] == [
            hoje() + timedelta(days=1), hoje() + timedelta(days=2),
            hoje() + timedelta(days=3)]
        for p, qtd in zip(peds, (10, 20, 30)):
            assert p.status == 'pendente'
            assert p.criado_por is None                 # sem autor humano
            assert p.observacao.startswith('Gerado do histórico')
            assert p.itens[0].quantidade == qtd


def test_rerodada_sincroniza_quantidades(app, loja, monkeypatch):
    from app.services import previsao_producao
    with app.app_context():
        _as_10h(monkeypatch)
        r = _receita()
        monkeypatch.setattr(previsao_producao, 'sugerir_pedidos_por_venda',
                            lambda **kw: _sugestao(loja, r, [10, 20, 30]))
        auto_pedidos.gerar_pedidos_automaticos()
        monkeypatch.setattr(previsao_producao, 'sugerir_pedidos_por_venda',
                            lambda **kw: _sugestao(loja, r, [12, 20, 30]))
        out = auto_pedidos.gerar_pedidos_automaticos()
        assert out['criados'] == 0 and out['atualizados'] >= 1
        p1 = (PedidoLoja.query
              .filter_by(loja_id=loja.id,
                         data_entrega=hoje() + timedelta(days=1)).one())
        assert p1.itens[0].quantidade == 12


def test_motor_real_re_sincroniza_rascunho_do_cron(app, loja, monkeypatch):
    """CRÍTICO da revisão de 13/08/2026: com o motor REAL, dia já pedido
    voltava sugestão 0 (`ja_tem`) e o rascunho congelava na 1ª criação. O
    `ressincronizar_datas` trata o rascunho do próprio cron como
    substituível — a 2ª rodada tem que ATUALIZAR a quantidade."""
    with app.app_context():
        _as_10h(monkeypatch)
        r = _receita('Pao Motor Real')
        el = EstoqueLoja(loja_id=loja.id, receita_id=r.id, quantidade=0,
                         estoque_minimo=50)
        db.session.add(el)
        db.session.commit()
        el_id = el.id

        out1 = auto_pedidos.gerar_pedidos_automaticos()
        assert out1['criados'] == 1                     # D+1 repõe o mínimo
        p1 = (PedidoLoja.query
              .filter_by(loja_id=loja.id,
                         data_entrega=hoje() + timedelta(days=1)).one())
        assert p1.itens[0].quantidade == 50
        assert p1.criado_por is None

        # A "venda do dia" muda o cenário (aqui: o mínimo configurado).
        db.session.get(EstoqueLoja, el_id).estoque_minimo = 80
        db.session.commit()
        out2 = auto_pedidos.gerar_pedidos_automaticos()
        assert out2['criados'] == 0
        assert out2['atualizados'] == 1
        peds = (PedidoLoja.query
                .filter(PedidoLoja.loja_id == loja.id,
                        PedidoLoja.status != 'cancelado',
                        PedidoLoja.data_entrega == hoje() + timedelta(days=1))
                .all())
        assert len(peds) == 1                           # nunca um 2º pedido
        assert peds[0].itens[0].quantidade == 80


def test_motor_real_nao_mexe_em_dia_de_humano(app, loja, admin_user,
                                              monkeypatch):
    """Dia com pedido de HUMANO fica travado mesmo com o motor real — e a
    quantidade dele entra como entrega na simulação dos dias seguintes."""
    with app.app_context():
        _as_10h(monkeypatch)
        r = _receita('Pao Motor Humano')
        db.session.add(EstoqueLoja(loja_id=loja.id, receita_id=r.id,
                                   quantidade=0, estoque_minimo=50))
        p = PedidoLoja(loja_id=loja.id,
                       data_entrega=hoje() + timedelta(days=1),
                       status='confirmado', criado_por=admin_user.id)
        db.session.add(p)
        db.session.flush()
        db.session.add(PedidoItem(pedido_id=p.id, receita_id=r.id,
                                  quantidade=10))
        db.session.commit()
        pid = p.id

        out = auto_pedidos.gerar_pedidos_automaticos()
        assert out['dias_pulados_humano'] == 1
        assert db.session.get(PedidoLoja, pid).itens[0].quantidade == 10
        assert (PedidoLoja.query
                .filter(PedidoLoja.loja_id == loja.id,
                        PedidoLoja.status != 'cancelado',
                        PedidoLoja.data_entrega == hoje() + timedelta(days=1))
                .count()) == 1
        # D+2 desconta a entrega de 10 do pedido humano: repõe só 40.
        p2 = (PedidoLoja.query
              .filter_by(loja_id=loja.id,
                         data_entrega=hoje() + timedelta(days=2)).one())
        assert p2.itens[0].quantidade == 40


def test_motor_real_sugestao_zerada_cancela_rascunho(app, loja, monkeypatch):
    """Rodada 2 da revisão: sugestão que CAI a 0 (estoque subiu e cobre)
    também tem que chegar no rascunho — deixar os 50 velhos congelarem às
    no corte viraria produção/entrega desnecessária. Dia todo-zerado CANCELA."""
    with app.app_context():
        _as_10h(monkeypatch)
        r = _receita('Pao Zera Dia')
        el = EstoqueLoja(loja_id=loja.id, receita_id=r.id, quantidade=0,
                         estoque_minimo=50)
        db.session.add(el)
        db.session.commit()
        el_id = el.id
        out1 = auto_pedidos.gerar_pedidos_automaticos()
        assert out1['criados'] == 1

        db.session.get(EstoqueLoja, el_id).quantidade = 200   # cobre tudo
        db.session.commit()
        out2 = auto_pedidos.gerar_pedidos_automaticos()
        assert out2['rascunhos_cancelados_zero'] == 1
        assert (PedidoLoja.query
                .filter(PedidoLoja.loja_id == loja.id,
                        PedidoLoja.status != 'cancelado')
                .count()) == 0


def test_motor_real_item_que_saiu_da_sugestao_e_removido(app, loja,
                                                         monkeypatch):
    """Item cuja sugestão caiu a 0 sai do rascunho (qtd 0 explícita na
    grade); o resto do pedido segue sincronizando."""
    with app.app_context():
        _as_10h(monkeypatch)
        ra = _receita('Pao Fica')
        rb = _receita('Croissant Sai')
        ela = EstoqueLoja(loja_id=loja.id, receita_id=ra.id, quantidade=0,
                          estoque_minimo=50)
        elb = EstoqueLoja(loja_id=loja.id, receita_id=rb.id, quantidade=0,
                          estoque_minimo=30)
        db.session.add_all([ela, elb])
        db.session.commit()
        elb_id = elb.id
        auto_pedidos.gerar_pedidos_automaticos()
        p1 = (PedidoLoja.query
              .filter_by(loja_id=loja.id,
                         data_entrega=hoje() + timedelta(days=1)).one())
        assert {it.receita_id: it.quantidade for it in p1.itens} == {
            ra.id: 50, rb.id: 30}

        db.session.get(EstoqueLoja, elb_id).quantidade = 200
        db.session.commit()
        auto_pedidos.gerar_pedidos_automaticos()
        p1 = db.session.get(PedidoLoja, p1.id)
        assert {it.receita_id: it.quantidade for it in p1.itens} == {
            ra.id: 50}                              # rb removido, ra fica


def test_dia_misto_absorve_rascunho_e_carry_fica_certo(app, loja, admin_user,
                                                       monkeypatch):
    """Rodada 2: dia com rascunho do cron E pedido humano (colisão — edição
    de data, legado): o cron ABSORVE o rascunho antes do motor, o carry da
    simulação fica só com o pedido que vale e D+2 não infla."""
    with app.app_context():
        _as_10h(monkeypatch)
        r = _receita('Pao Dia Misto')
        db.session.add(EstoqueLoja(loja_id=loja.id, receita_id=r.id,
                                   quantidade=0, estoque_minimo=50))
        humano = PedidoLoja(loja_id=loja.id,
                            data_entrega=hoje() + timedelta(days=1),
                            status='confirmado', criado_por=admin_user.id)
        db.session.add(humano)
        db.session.flush()
        db.session.add(PedidoItem(pedido_id=humano.id, receita_id=r.id,
                                  quantidade=10))
        db.session.commit()
        rascunho = _rascunho_auto(loja, [(r, 40)])
        hid, raid = humano.id, rascunho.id

        out = auto_pedidos.gerar_pedidos_automaticos()
        assert out['rascunhos_absorvidos'] == 1
        assert db.session.get(PedidoLoja, raid).status == 'cancelado'
        assert db.session.get(PedidoLoja, hid).itens[0].quantidade == 10
        # Carry de D+1 = só os 10 do humano → D+2 repõe 40 (não 50).
        p2 = (PedidoLoja.query
              .filter_by(loja_id=loja.id,
                         data_entrega=hoje() + timedelta(days=2)).one())
        assert p2.itens[0].quantidade == 40


def test_cancelamento_humano_nao_ressuscita(app, loja, admin_user,
                                            monkeypatch):
    """Rodada 2: a loja cancela o pedido automático de D+2 ("não quero
    pedido") — o cron NÃO recria na rodada seguinte (o cancelar carimba e o
    dia fica protegido)."""
    from app.services import previsao_producao
    with app.app_context():
        _as_10h(monkeypatch)
        r = _receita()
        p = _rascunho_auto(loja, [(r, 20)], dias=2)
        pid = p.id
        c = app.test_client()
        _login(c, admin_user)
        c.post(f'/pedidos/{pid}/cancelar')
        p = db.session.get(PedidoLoja, pid)
        assert p.status == 'cancelado'
        assert p.modificado_por_id == admin_user.id

        monkeypatch.setattr(previsao_producao, 'sugerir_pedidos_por_venda',
                            lambda **kw: _sugestao(loja, r, [11, 22, 33]))
        auto_pedidos.gerar_pedidos_automaticos()
        assert (PedidoLoja.query
                .filter(PedidoLoja.loja_id == loja.id,
                        PedidoLoja.status != 'cancelado',
                        PedidoLoja.data_entrega == hoje() + timedelta(days=2))
                .count()) == 0                      # D+2 segue sem pedido


def test_sincronizar_do_cron_nao_apaga_carimbo_humano(app, loja, admin_user):
    """Rodada 2 (corrida cron×adoção): o sync com user_id=None nunca zera um
    carimbo humano existente — na corrida, a rodada seguinte protege."""
    from app.services.pedidos_semana import _sincronizar_itens
    with app.app_context():
        r = _receita()
        p = _rascunho_auto(loja, [(r, 40)])
        p.modificado_por_id = admin_user.id         # humano tocou no meio
        db.session.commit()
        _sincronizar_itens(p, [{'receita_id': r.id, 'qtd': 30}], None)
        db.session.commit()
        assert p.itens[0].quantidade == 30
        assert p.modificado_por_id == admin_user.id  # carimbo sobrevive


def test_adocao_com_estado_divergente_nao_duplica_linha(app, loja,
                                                        admin_user,
                                                        monkeypatch):
    """Rodada 2: humano cita "45 assado" e o rascunho tem a linha sem
    estado — substitui a MESMA linha (estado junto), nunca cria uma 2ª
    (seria a dobra parcial que a adoção quis evitar)."""
    from app.services.copilot import executar_criar_pedido
    with app.app_context():
        _as_10h(monkeypatch)
        r = _receita('Pao Estado Divergente')
        p = _rascunho_auto(loja, [(r, 40)])
        pid = p.id
        res = executar_criar_pedido({
            'loja_id': loja.id,
            'data_entrega': (hoje() + timedelta(days=1)).isoformat(),
            'itens': [{'resolvido': {'tipo': 'receita', 'id': r.id,
                                     'nome': r.nome}, 'quantidade': 45,
                       'estado': 'assado'}],
        }, admin_user)
        assert res['ok'] is True
        p = db.session.get(PedidoLoja, pid)
        assert len(p.itens) == 1
        assert p.itens[0].quantidade == 45
        assert p.itens[0].estado == 'assado'


def test_pedido_tocado_por_humano_e_preservado(app, loja, admin_user,
                                               monkeypatch):
    """A palavra da loja vale mais que a do motor: pedido criado ou
    modificado por gente NUNCA é sobrescrito pelo cron."""
    from app.services import previsao_producao
    with app.app_context():
        _as_10h(monkeypatch)
        r = _receita()
        monkeypatch.setattr(previsao_producao, 'sugerir_pedidos_por_venda',
                            lambda **kw: _sugestao(loja, r, [10, 20, 30]))
        auto_pedidos.gerar_pedidos_automaticos()
        p2 = (PedidoLoja.query
              .filter_by(loja_id=loja.id,
                         data_entrega=hoje() + timedelta(days=2)).one())
        p2.itens[0].quantidade = 99                 # gerente ajustou na mão
        p2.modificado_por_id = admin_user.id
        db.session.commit()
        pid2 = p2.id

        monkeypatch.setattr(previsao_producao, 'sugerir_pedidos_por_venda',
                            lambda **kw: _sugestao(loja, r, [11, 22, 33]))
        out = auto_pedidos.gerar_pedidos_automaticos()
        assert out['dias_pulados_humano'] == 1
        assert db.session.get(PedidoLoja, pid2).itens[0].quantidade == 99
        p1 = (PedidoLoja.query
              .filter_by(loja_id=loja.id,
                         data_entrega=hoje() + timedelta(days=1)).one())
        assert p1.itens[0].quantidade == 11         # os outros seguem o motor


def test_pedido_criado_por_humano_e_preservado(app, loja, admin_user,
                                               monkeypatch):
    from app.services import previsao_producao
    with app.app_context():
        _as_10h(monkeypatch)
        r = _receita()
        p = PedidoLoja(loja_id=loja.id,
                       data_entrega=hoje() + timedelta(days=2),
                       status='confirmado', criado_por=admin_user.id)
        db.session.add(p)
        db.session.flush()
        db.session.add(PedidoItem(pedido_id=p.id, receita_id=r.id,
                                  quantidade=77))
        db.session.commit()
        pid = p.id
        monkeypatch.setattr(previsao_producao, 'sugerir_pedidos_por_venda',
                            lambda **kw: _sugestao(loja, r, [10, 20, 30]))
        out = auto_pedidos.gerar_pedidos_automaticos()
        assert out['dias_pulados_humano'] == 1
        assert db.session.get(PedidoLoja, pid).itens[0].quantidade == 77


def test_confirmar_na_web_carimba_e_protege_do_cron(app, loja, admin_user,
                                                    monkeypatch):
    """O clique "Confirmar" sem mexer em item é revisão humana: carimba
    modificado_por_id e o cron para de re-sincronizar aquele dia (achado 5
    da revisão de 13/08 — sem o carimbo, o fix da re-sincronização
    reintroduziria "sobrescrever gente")."""
    from app.services import previsao_producao
    with app.app_context():
        _as_10h(monkeypatch)
        r = _receita()
        p = _rascunho_auto(loja, [(r, 20)], dias=2)
        pid = p.id
        c = app.test_client()
        _login(c, admin_user)
        c.post(f'/pedidos/{pid}/confirmar')
        p = db.session.get(PedidoLoja, pid)
        assert p.status == 'confirmado'
        assert p.modificado_por_id == admin_user.id

        monkeypatch.setattr(previsao_producao, 'sugerir_pedidos_por_venda',
                            lambda **kw: _sugestao(loja, r, [11, 22, 33]))
        out = auto_pedidos.gerar_pedidos_automaticos()
        assert out['dias_pulados_humano'] == 1
        assert db.session.get(PedidoLoja, pid).itens[0].quantidade == 20


def test_d1_sob_corte_nunca_e_tocado(app, loja, monkeypatch):
    """Rodada depois do corte (ex.: disparo manual): o D+1 está travado — o
    cron respeita o MESMO corte que trava as lojas."""
    from app.services import previsao_producao
    with app.app_context():
        r = _receita()
        fake = datetime.combine(hoje(), datetime.min.time()).replace(hour=20)
        monkeypatch.setattr(pedido_corte, 'agora', lambda: fake)
        monkeypatch.setattr(previsao_producao, 'sugerir_pedidos_por_venda',
                            lambda **kw: _sugestao(loja, r, [10, 20, 30]))
        out = auto_pedidos.gerar_pedidos_automaticos()
        assert out['criados'] == 2                   # D+2 e D+3
        assert out['dias_pulados_corte'] == [
            (hoje() + timedelta(days=1)).isoformat()]
        assert (PedidoLoja.query
                .filter_by(loja_id=loja.id,
                           data_entrega=hoje() + timedelta(days=1))
                .count()) == 0


def test_sugestao_zerada_nao_cria_pedido_vazio(app, loja, monkeypatch):
    from app.services import previsao_producao
    with app.app_context():
        _as_10h(monkeypatch)
        r = _receita()
        monkeypatch.setattr(previsao_producao, 'sugerir_pedidos_por_venda',
                            lambda **kw: _sugestao(loja, r, [0, 0, 0]))
        out = auto_pedidos.gerar_pedidos_automaticos()
        assert out['criados'] == 0
        assert PedidoLoja.query.filter_by(loja_id=loja.id).count() == 0


def test_seguranca_pct_ilegivel_nao_mata_o_job(app, loja, monkeypatch):
    """Env torta não pode matar o cron em silêncio a cada rodada — vira 0
    com WARNING (padrão _cfg_int da casa)."""
    from app.services import previsao_producao
    with app.app_context():
        _as_10h(monkeypatch)
        r = _receita()
        monkeypatch.setenv('AUTO_PEDIDOS_SEGURANCA_PCT', 'abc')
        recebidos = {}

        def _motor(**kw):
            recebidos.update(kw)
            return _sugestao(loja, r, [5, 0, 0])
        monkeypatch.setattr(previsao_producao, 'sugerir_pedidos_por_venda',
                            _motor)
        out = auto_pedidos.gerar_pedidos_automaticos()
        assert out['criados'] == 1
        assert recebidos['seguranca_pct'] == 0


# ── colisão humano × rascunho do cron (crítico 2 da revisão 13/08) ──

def test_web_novo_adota_rascunho_automatico(app, loja, admin_user,
                                            monkeypatch):
    """Gerente "lança o pedido de amanhã" via /pedidos/novo num dia que o
    cron já cobriu: ADOTA o rascunho (nunca um 2º pedido = demanda em
    dobro). Item citado substitui; item do motor não citado FICA."""
    with app.app_context():
        _as_10h(monkeypatch)
        r = _receita('Pao Adocao')
        r2 = _receita('Croissant Adocao')
        p = _rascunho_auto(loja, [(r, 40), (r2, 30)])
        pid, rid, lid = p.id, r.id, loja.id
        c = app.test_client()
        _login(c, admin_user)
        resp = c.post('/pedidos/novo', data={
            'loja_id': str(lid),
            'data_entrega': (hoje() + timedelta(days=1)).isoformat(),
            'item_id[]': f'r_{rid}',
            'item_qtd[]': '45',
            'item_estado[]': '',
            'item_obs[]': '',
        }, follow_redirects=True)
        texto = resp.get_data(as_text=True)
        assert 'sugestão automática' in texto
        vivos = (PedidoLoja.query
                 .filter(PedidoLoja.loja_id == lid,
                         PedidoLoja.status != 'cancelado',
                         PedidoLoja.data_entrega == hoje() + timedelta(days=1))
                 .all())
        assert [v.id for v in vivos] == [pid]           # UM pedido só
        p = vivos[0]
        assert p.status == 'confirmado'
        assert p.modificado_por_id == admin_user.id     # protegido do cron
        qtds = {it.receita_id: it.quantidade for it in p.itens}
        assert qtds[rid] == 45                          # substituiu (não 85)
        assert qtds[r2.id] == 30                        # mantido + avisado
        assert 'MANTIDOS' in texto
        assert not (p.observacao or '').startswith('Gerado do histórico')


def test_web_novo_com_confirmado_absorve_rascunho(app, loja, admin_user,
                                                  monkeypatch):
    """Estado de colisão pré-fix (pedido humano E rascunho do cron no mesmo
    dia): o próximo gesto humano de criação junta no confirmado e CANCELA o
    rascunho — a dobra morre ali."""
    with app.app_context():
        _as_10h(monkeypatch)
        r = _receita('Pao Absorve')
        humano = PedidoLoja(loja_id=loja.id,
                            data_entrega=hoje() + timedelta(days=1),
                            status='confirmado', criado_por=admin_user.id)
        db.session.add(humano)
        db.session.flush()
        db.session.add(PedidoItem(pedido_id=humano.id, receita_id=r.id,
                                  quantidade=45))
        db.session.commit()
        rascunho = _rascunho_auto(loja, [(r, 40)])
        hid, raid, rid, lid = humano.id, rascunho.id, r.id, loja.id
        c = app.test_client()
        _login(c, admin_user)
        c.post('/pedidos/novo', data={
            'loja_id': str(lid),
            'data_entrega': (hoje() + timedelta(days=1)).isoformat(),
            'item_id[]': f'r_{rid}',
            'item_qtd[]': '5',
            'item_estado[]': '',
            'item_obs[]': '',
        }, follow_redirects=True)
        assert db.session.get(PedidoLoja, raid).status == 'cancelado'
        h = db.session.get(PedidoLoja, hid)
        assert h.itens[0].quantidade == 50              # 45 + 5 (merge normal)


def test_copilot_criar_adota_rascunho(app, loja, admin_user, monkeypatch):
    from app.services.copilot import executar_criar_pedido
    with app.app_context():
        _as_10h(monkeypatch)
        r = _receita('Pao Copilot Adota')
        r2 = _receita('Cookie Copilot Adota')
        p = _rascunho_auto(loja, [(r, 40), (r2, 30)])
        pid = p.id
        res = executar_criar_pedido({
            'loja_id': loja.id,
            'data_entrega': (hoje() + timedelta(days=1)).isoformat(),
            'itens': [{'resolvido': {'tipo': 'receita', 'id': r.id,
                                     'nome': r.nome}, 'quantidade': 45}],
        }, admin_user)
        assert res['ok'] is True
        assert res.get('adotou_rascunho') is True
        assert res['pedido_id'] == pid
        assert 'MANTIDOS' in res['aviso']
        p = db.session.get(PedidoLoja, pid)
        assert p.status == 'confirmado'
        assert p.modificado_por_id == admin_user.id
        qtds = {it.receita_id: it.quantidade for it in p.itens}
        assert qtds[r.id] == 45 and qtds[r2.id] == 30
        assert (PedidoLoja.query
                .filter(PedidoLoja.loja_id == loja.id,
                        PedidoLoja.status != 'cancelado')
                .count()) == 1


# ── ordem de produção da SEMANA (dono 17/08/2026) ───────────────────

def test_semana_no_domingo_abre_seg_a_dom(app, monkeypatch):
    """Rodada de DOMINGO: envia as ordens de segunda até o PRÓXIMO domingo
    (7 dias), motor default 'vendas', horizonte que alcança o fim."""
    from datetime import date

    from app.services import producao
    chamadas = []
    with app.app_context():
        dom = date(2026, 8, 16)                        # um domingo real
        monkeypatch.setattr(auto_pedidos, 'hoje', lambda: dom)
        monkeypatch.setattr(producao, 'aprovar_plano_do_dia',
                            lambda data, user_id=None, **kw: None)
        monkeypatch.setattr(
            producao, 'enviar_plano_do_dia',
            lambda data, user_id=None, **kw: chamadas.append(
                (data, kw.get('motor'), kw.get('horizonte_dias'))) or
            type('P', (), {'itens': [1]})())
        out = auto_pedidos.enviar_ordens_da_semana()
        esperados = [dom + timedelta(days=i) for i in range(1, 8)]
        assert [c[0] for c in chamadas] == esperados   # seg..dom seguinte
        assert {c[1] for c in chamadas} == {'vendas'}  # default 17/08/2026
        # o grid precisa CONTER o próximo domingo (dia 7 → horizonte 8)
        assert {c[2] for c in chamadas} == {8}
        assert out['de'] == esperados[0].isoformat()
        assert out['ate'] == esperados[-1].isoformat()
        assert len(out['enviadas']) == 7 and not out['puladas']


def test_semana_pula_ordem_humana_e_resincroniza_a_do_cron(app, admin_user,
                                                          monkeypatch):
    """Fora do domingo o job mantém a semana FIEL AO GRID: ordem enviada
    por HUMANO (criado_por preenchido) nunca é tocada; ordem do PRÓPRIO
    CRON (criado_por None) é re-sincronizada com o grid; dia sem ordem é
    enviado (rede)."""
    from datetime import date

    from app.models import PlanejamentoProducao
    from app.services import producao
    chamadas = []
    with app.app_context():
        seg = date(2026, 8, 17)                        # uma segunda real
        monkeypatch.setattr(auto_pedidos, 'hoje', lambda: seg)
        qua = date(2026, 8, 19)                        # ordem HUMANA
        qui = date(2026, 8, 20)                        # ordem do CRON
        db.session.add(PlanejamentoProducao(
            data=qua, origem='cronograma', enviado_ao_padeiro=True,
            criado_por=admin_user.id, nome='Ordem humana'))
        db.session.add(PlanejamentoProducao(
            data=qui, origem='cronograma', enviado_ao_padeiro=True,
            criado_por=None, nome='Ordem do cron'))
        db.session.commit()
        monkeypatch.setattr(producao, 'aprovar_plano_do_dia',
                            lambda data, user_id=None, **kw: None)
        monkeypatch.setattr(
            producao, 'enviar_plano_do_dia',
            lambda data, user_id=None, **kw: chamadas.append(data) or
            type('P', (), {'itens': [1]})())
        out = auto_pedidos.enviar_ordens_da_semana()
        # qua (humana) fora; qui entra como RE-SYNC; o resto como envio.
        esperados = [date(2026, 8, d) for d in (18, 20, 21, 22, 23)]
        assert chamadas == esperados
        assert out['puladas'] == [qua.isoformat()]
        assert out['resincronizadas'] == [qui.isoformat()]
        assert out['enviadas'] == [date(2026, 8, d).isoformat()
                                   for d in (18, 21, 22, 23)]
        assert out['ate'] == '2026-08-23'              # próximo domingo


def test_semana_corrida_humano_enviou_pula_o_dia(app, monkeypatch):
    """Humano enviou um dia ENTRE o snapshot e o aprovar (corrida): o
    aprovar recusa (PlanoJaEnviadoError), o dia é pulado e o resto da
    semana segue normal."""
    from datetime import date

    from app.services import producao
    chamadas = []
    with app.app_context():
        seg = date(2026, 8, 17)
        monkeypatch.setattr(auto_pedidos, 'hoje', lambda: seg)
        alvo_corrida = date(2026, 8, 20)

        def _aprovar(data, user_id=None, **kw):
            if data == alvo_corrida:
                raise producao.PlanoJaEnviadoError(data.isoformat())
        monkeypatch.setattr(producao, 'aprovar_plano_do_dia', _aprovar)
        monkeypatch.setattr(
            producao, 'enviar_plano_do_dia',
            lambda data, user_id=None, **kw: chamadas.append(data) or
            type('P', (), {'itens': [1]})())
        out = auto_pedidos.enviar_ordens_da_semana()
        assert alvo_corrida not in chamadas
        assert out['puladas'] == [alvo_corrida.isoformat()]
        assert len(out['enviadas']) == 5               # ter..dom menos qui


def test_atualiza_re_sincroniza_ordem_do_cron(app, monkeypatch):
    """🔄 automático (17/08/2026): a ordem DE HOJE criada pelo cron é
    re-sincronizada com o grid às 06:45/19:05 — os itens de véspera dela
    são dirigidos pela demanda de amanhã, que muda depois do envio."""
    from app.models import PlanejamentoProducao
    from app.services import producao
    chamadas = []
    with app.app_context():
        db.session.add(PlanejamentoProducao(
            data=hoje(), origem='cronograma', enviado_ao_padeiro=True,
            criado_por=None, nome='Ordem do cron'))
        db.session.commit()
        monkeypatch.setattr(
            producao, 'enviar_plano_do_dia',
            lambda data, user_id=None, **kw: chamadas.append(
                (data, kw.get('motor'))) or
            type('P', (), {'itens': [1, 2, 3]})())
        out = auto_pedidos.atualizar_plano_automatico()
        assert chamadas == [(hoje(), 'vendas')]   # mesmo default do envio
        assert out['atualizada'] is True and out['itens'] == 3


def test_atualiza_nao_toca_ordem_de_humano(app, admin_user, monkeypatch):
    """Ordem enviada por HUMANO nunca muda por caminho implícito — o 🔄
    automático só re-sincroniza ordem do próprio cron."""
    from app.models import PlanejamentoProducao
    from app.services import producao
    chamadas = []
    with app.app_context():
        db.session.add(PlanejamentoProducao(
            data=hoje(), origem='cronograma', enviado_ao_padeiro=True,
            criado_por=admin_user.id, nome='Ordem do dono'))
        db.session.commit()
        monkeypatch.setattr(producao, 'enviar_plano_do_dia',
                            lambda *a, **kw: chamadas.append('enviar'))
        out = auto_pedidos.atualizar_plano_automatico()
        assert out.get('ordem_humana') is True
        assert chamadas == []


def test_atualiza_sem_ordem_e_noop(app, monkeypatch):
    from app.services import producao
    chamadas = []
    with app.app_context():
        monkeypatch.setattr(producao, 'enviar_plano_do_dia',
                            lambda *a, **kw: chamadas.append('enviar'))
        out = auto_pedidos.atualizar_plano_automatico()
        assert out.get('sem_ordem') is True
        assert chamadas == []


def test_semana_dia_sem_nada_conta_como_vazio(app, monkeypatch):
    """Dia cujo grid não tem nada a produzir (enviar devolve None) entra em
    `vazias` — não explode nem conta como enviado."""
    from app.services import producao
    with app.app_context():
        monkeypatch.setattr(producao, 'aprovar_plano_do_dia',
                            lambda *a, **kw: None)
        monkeypatch.setattr(producao, 'enviar_plano_do_dia',
                            lambda *a, **kw: None)
        out = auto_pedidos.enviar_ordens_da_semana()
        assert not out['enviadas']
        assert len(out['vazias']) >= 1


def test_motor_da_semana_por_env(app, monkeypatch):
    """A env AUTO_ENVIO_MOTOR segue mandando sobre o default 'vendas' —
    setada com outro motor, é ela que vale (Railway sobrepõe o código)."""
    from app.services import producao
    motores = set()
    with app.app_context():
        monkeypatch.setenv('AUTO_ENVIO_MOTOR', 'pedidos')
        monkeypatch.setattr(producao, 'aprovar_plano_do_dia',
                            lambda data, user_id=None, **kw:
                            motores.add(kw.get('motor')))
        monkeypatch.setattr(
            producao, 'enviar_plano_do_dia',
            lambda data, user_id=None, **kw:
            motores.add(kw.get('motor')) or
            type('P', (), {'itens': []})())
        auto_pedidos.enviar_ordens_da_semana()
        assert motores == {'pedidos'}


def test_retro_one_shot_roda_uma_vez(app, monkeypatch):
    """O one-shot de retroação (boot pós-deploy de 17/08/2026) roda UMA
    vez — PEDIDOS da semana antes das ORDENS (o firme alimenta o grid) —
    grava o marker em AppConfig e o boot seguinte não re-executa."""
    from app.models import AppConfig
    from app.services import seru_cron
    chamadas = []
    with app.app_context():
        monkeypatch.setattr(auto_pedidos, 'gerar_pedidos_automaticos',
                            lambda: chamadas.append('pedidos'))
        monkeypatch.setattr(auto_pedidos, 'enviar_ordens_da_semana',
                            lambda: chamadas.append('ordens'))
        seru_cron._run_ordens_semana_retro(app)
        assert chamadas == ['pedidos', 'ordens']       # nesta ordem
        assert AppConfig.get(seru_cron.ORDENS_SEMANA_RETRO_MARKER)
        seru_cron._run_ordens_semana_retro(app)
        assert chamadas == ['pedidos', 'ordens']       # não repetiu


def test_janela_dos_pedidos_vai_ate_o_proximo_domingo(app, loja, monkeypatch):
    """Dono 17/08/2026: os pedidos automáticos cobrem amanhã..PRÓXIMO
    DOMINGO (numa segunda: ter..dom, 6 dias) — não mais D+1..D+3."""
    from datetime import date

    from app.services import previsao_producao
    capt = {}
    with app.app_context():
        _as_10h(monkeypatch)
        r = _receita()

        def _motor(**kw):
            capt.update(kw)
            return _sugestao(loja, r, [10])
        monkeypatch.setattr(previsao_producao, 'sugerir_pedidos_por_venda',
                            _motor)
        auto_pedidos.gerar_pedidos_automaticos()
        assert capt['horizonte_dias'] == 6         # segunda congelada
        assert capt['inicio_offset_dias'] == 1
        datas = capt['ressincronizar_datas']
        assert datas[0] == date(2026, 8, 18)       # amanhã (terça)
        assert datas[-1] == date(2026, 8, 23)      # próximo domingo
