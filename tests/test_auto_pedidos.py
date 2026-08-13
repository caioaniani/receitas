"""Pedidos automáticos loja→indústria + envio automático da ordem
(10/08/2026, decisão do dono: "faça os pedidos automaticamente de 3 dias na
frente" + automatizar o envio ao padeiro — revoga "enviar é gesto humano"
de 04/07/2026).

Motor venda+estoque mockado nos testes de materialização (o motor em si já
tem suíte própria); aqui trava-se o CONTRATO: rascunho sem autor humano,
re-sincronização, respeito a pedido de humano, corte das 18h e o envio.
"""
from datetime import datetime, timedelta

from app.extensions import db
from app.models import PedidoItem, PedidoLoja, Receita
from app.services import auto_pedidos, pedido_corte
from app.utils import hoje


def _receita(nome='Pao Auto'):
    r = Receita(nome=nome, categoria='Paes', rendimento_qtd=1,
                rendimento_unidade='un', peso_base=100)
    db.session.add(r)
    db.session.commit()
    return r


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


def test_cria_rascunhos_3_dias_sem_autor_humano(app, loja, monkeypatch):
    with app.app_context():
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


def test_pedido_tocado_por_humano_e_preservado(app, loja, admin_user,
                                               monkeypatch):
    """A palavra da loja vale mais que a do motor: pedido criado ou
    modificado por gente NUNCA é sobrescrito pelo cron."""
    from app.services import previsao_producao
    with app.app_context():
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
        r = _receita()
        monkeypatch.setattr(previsao_producao, 'sugerir_pedidos_por_venda',
                            lambda **kw: _sugestao(loja, r, [0, 0, 0]))
        out = auto_pedidos.gerar_pedidos_automaticos()
        assert out['criados'] == 0
        assert PedidoLoja.query.filter_by(loja_id=loja.id).count() == 0


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


def test_envio_tolera_plano_ja_enviado(app, monkeypatch):
    """Ordem já enviada por gesto humano no dia: o aprovar recusa
    (PlanoJaEnviadoError) e o enviar re-sincroniza mesmo assim."""
    from app.services import producao
    with app.app_context():
        def _boom(*a, **kw):
            raise producao.PlanoJaEnviadoError('ja enviado')
        monkeypatch.setattr(producao, 'aprovar_plano_do_dia', _boom)
        monkeypatch.setattr(
            producao, 'enviar_plano_do_dia',
            lambda *a, **kw: type('P', (), {'itens': [1]})())
        out = auto_pedidos.enviar_plano_automatico()
        assert out['itens'] == 1


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
