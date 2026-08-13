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

from app.extensions import db
from app.models import EstoqueLoja, PedidoItem, PedidoLoja, Receita
from app.services import auto_pedidos, pedido_corte
from app.utils import hoje

MARCADOR = 'Gerado do histórico (rascunho) — revisar e confirmar.'


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
    materialização não são sobre o corte, e rodar a suíte após as 18h BRT
    reais pularia o D+1 e quebraria o CI (deploy travado — mesma classe do
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
    """Rodada depois das 18h (ex.: disparo manual): o D+1 está travado pelo
    corte — o cron respeita o MESMO corte que trava as lojas."""
    from app.services import previsao_producao
    with app.app_context():
        r = _receita()
        fake = datetime.combine(hoje(), datetime.min.time()).replace(hour=19)
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


# ── envio automático da ordem ───────────────────────────────────────

def test_envio_automatico_aprova_e_envia_amanha(app, monkeypatch):
    from app.services import producao
    chamadas = []
    with app.app_context():
        monkeypatch.setattr(
            producao, 'aprovar_plano_do_dia',
            lambda data, user_id=None, **kw: chamadas.append(
                ('aprovar', data, kw.get('motor'))))
        monkeypatch.setattr(
            producao, 'enviar_plano_do_dia',
            lambda data, user_id=None, **kw: chamadas.append(
                ('enviar', data, kw.get('motor'))) or
            type('P', (), {'itens': [1, 2]})())
        out = auto_pedidos.enviar_plano_automatico()
        amanha = hoje() + timedelta(days=1)
        assert ('aprovar', amanha, 'pedidos') in chamadas
        assert ('enviar', amanha, 'pedidos') in chamadas
        assert out['data'] == amanha.isoformat() and out['itens'] == 2


def test_envio_nao_reenvia_ordem_ja_enviada(app, monkeypatch):
    """Ordem de amanhã JÁ ENVIADA (gesto humano na tela, com o motor/
    equilibrar DELE): o cron NÃO reenvia — reenviar com os defaults trocaria
    os números do padeiro em silêncio (achado 3 da revisão de 13/08; regra
    "ordem enviada nunca muda por caminho implícito" preservada)."""
    from app.models import PlanejamentoProducao
    from app.services import producao
    chamadas = []
    with app.app_context():
        db.session.add(PlanejamentoProducao(
            data=hoje() + timedelta(days=1), origem='cronograma',
            enviado_ao_padeiro=True, nome='Ordem humana'))
        db.session.commit()
        monkeypatch.setattr(producao, 'aprovar_plano_do_dia',
                            lambda *a, **kw: chamadas.append('aprovar'))
        monkeypatch.setattr(producao, 'enviar_plano_do_dia',
                            lambda *a, **kw: chamadas.append('enviar'))
        out = auto_pedidos.enviar_plano_automatico()
        assert out.get('ja_enviado') is True
        assert chamadas == []


def test_envio_corrida_plano_ja_enviado_nao_reenvia(app, monkeypatch):
    """Humano enviou ENTRE a checagem e o aprovar (corrida): o aprovar
    recusa (PlanoJaEnviadoError) e o cron desiste — a ordem do humano
    vale."""
    from app.services import producao
    chamadas = []
    with app.app_context():
        def _boom(*a, **kw):
            raise producao.PlanoJaEnviadoError('ja enviado')
        monkeypatch.setattr(producao, 'aprovar_plano_do_dia', _boom)
        monkeypatch.setattr(producao, 'enviar_plano_do_dia',
                            lambda *a, **kw: chamadas.append('enviar'))
        out = auto_pedidos.enviar_plano_automatico()
        assert out.get('ja_enviado') is True
        assert chamadas == []


def test_envio_dia_vazio_nao_explode(app, monkeypatch):
    from app.services import producao
    with app.app_context():
        monkeypatch.setattr(producao, 'aprovar_plano_do_dia',
                            lambda *a, **kw: None)
        monkeypatch.setattr(producao, 'enviar_plano_do_dia',
                            lambda *a, **kw: None)
        out = auto_pedidos.enviar_plano_automatico()
        assert out.get('vazio') is True and out['itens'] == 0


def test_motor_do_envio_por_env(app, monkeypatch):
    from app.services import producao
    chamadas = []
    with app.app_context():
        monkeypatch.setenv('AUTO_ENVIO_MOTOR', 'vendas')
        monkeypatch.setattr(producao, 'aprovar_plano_do_dia',
                            lambda data, user_id=None, **kw:
                            chamadas.append(kw.get('motor')))
        monkeypatch.setattr(
            producao, 'enviar_plano_do_dia',
            lambda data, user_id=None, **kw:
            chamadas.append(kw.get('motor')) or
            type('P', (), {'itens': []})())
        auto_pedidos.enviar_plano_automatico()
        assert chamadas == ['vendas', 'vendas']
