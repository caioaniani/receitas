"""Acompanhamento de entrega por progresso + foto obrigatória (01/08/2026).

Operação do Dia dos Pais: motoristas contratados, cliente acompanha por
"parada N de M + previsão", e ENTREGUE só com foto de comprovação (decisão
do dono 01/08/2026 — sem comprovação, "entregue" é só uma palavra)."""
from unittest.mock import patch

from app.extensions import db
from app.utils import hoje


def _driver():
    from app.models import Driver
    d = Driver(nome='João Rota', ativo=True, token='tok123', pin='1234')
    db.session.add(d)
    db.session.commit()
    return d


def _rota(d, n=3):
    from app.models import AtribuicaoEntrega, PedidoOnline
    codes = []
    for i in range(n):
        p = PedidoOnline(codigo=f'RT{i}', nome_cliente=f'C{i}',
                         email_cliente=f'c{i}@x.com', status='pago',
                         modo_entrega='agendada', data_entrega=hoje(),
                         janela_entrega='06:00–10:00', valor_total=100)
        db.session.add(p)
        db.session.commit()
        db.session.add(AtribuicaoEntrega(pedido_code=p.codigo,
                                         driver_id=d.id,
                                         data_entrega=hoje(), ordem=i))
        codes.append(p.codigo)
    db.session.commit()
    return codes


def test_antes_da_rota_sair_fica_em_preparo(app):
    from app.services import rastreio_entrega as svc
    d = _driver()
    codes = _rota(d)
    assert svc.status_do_pedido(codes[0]) == {'fase': 'em_preparo'}


def test_iniciar_rota_dispara_email_uma_vez(app):
    """Idempotente: o 2º clique (ou o duplo clique) não reenvia nada."""
    from app.services import rastreio_entrega as svc
    d = _driver()
    _rota(d, n=2)
    with patch('app.services.email.enviar_pedido_a_caminho',
               return_value={'ok': True}) as env:
        ri, n1 = svc.iniciar_rota(d)
        ri2, n2 = svc.iniciar_rota(d)
    assert n1 == 2 and n2 == 0
    assert ri.id == ri2.id
    assert env.call_count == 2


def test_progresso_avanca_quando_o_motorista_entrega(app):
    """Cada "entregue" move a posição e a ETA de TODOS os da rota."""
    from app.models import AtribuicaoEntrega
    from app.services import rastreio_entrega as svc
    from app.utils import agora
    d = _driver()
    codes = _rota(d, n=3)
    with patch('app.services.email.enviar_pedido_a_caminho',
               return_value={'ok': True}):
        svc.iniciar_rota(d)
    s = svc.status_do_pedido(codes[2])
    assert s['fase'] == 'a_caminho'
    assert s['parada'] == 3 and s['faltam'] == 2
    assert s['driver'] == 'João Rota'
    assert s['eta']                       # tem previsão
    a = AtribuicaoEntrega.query.filter_by(pedido_code=codes[0]).first()
    a.status = 'entregue'
    a.entregue_em = agora()
    db.session.commit()
    s = svc.status_do_pedido(codes[2])
    assert s['parada'] == 2 and s['faltam'] == 1


def test_entregue_mostra_hora(app):
    from app.models import AtribuicaoEntrega
    from app.services import rastreio_entrega as svc
    from app.utils import agora
    d = _driver()
    codes = _rota(d, n=1)
    a = AtribuicaoEntrega.query.filter_by(pedido_code=codes[0]).first()
    a.status = 'entregue'
    a.entregue_em = agora()
    db.session.commit()
    s = svc.status_do_pedido(codes[0])
    assert s['fase'] == 'entregue' and s['entregue_em']


def test_json_publico_do_pedido_leva_o_rastreio(app):
    from app.services import rastreio_entrega as svc  # noqa: F401
    d = _driver()
    codes = _rota(d, n=1)
    with patch('app.services.email.enviar_pedido_a_caminho',
               return_value={'ok': True}):
        from app.services.rastreio_entrega import iniciar_rota
        iniciar_rota(d)
    c = app.test_client()
    j = c.get(f'/loja/pedido/{codes[0]}/status').get_json()
    assert j['rastreio']['fase'] == 'a_caminho'


# ── Foto obrigatória no entregue (driver) ────────────────────────────────

def _client_driver(app, d):
    c = app.test_client()
    with c.session_transaction() as s:
        s[f'driver_auth_{d.id}'] = True
    return c


def test_entregue_sem_foto_e_recusado(app):
    from app.models import AtribuicaoEntrega
    d = _driver()
    codes = _rota(d, n=1)
    a = AtribuicaoEntrega.query.filter_by(pedido_code=codes[0]).first()
    c = _client_driver(app, d)
    r = c.post(f'/driver/api/{d.token}/status',
               json={'atribuicao_id': a.id, 'status': 'entregue'})
    assert r.status_code == 422
    assert r.get_json()['precisa_foto'] is True
    db.session.expire_all()
    assert a.status != 'entregue'


def test_entregue_com_foto_passa(app):
    from app.models import AtribuicaoEntrega, EntregaFoto
    d = _driver()
    codes = _rota(d, n=1)
    a = AtribuicaoEntrega.query.filter_by(pedido_code=codes[0]).first()
    db.session.add(EntregaFoto(atribuicao_id=a.id, url='https://x/f.jpg',
                               storage_path='/f.jpg', tamanho_bytes=10))
    db.session.commit()
    c = _client_driver(app, d)
    r = c.post(f'/driver/api/{d.token}/status',
               json={'atribuicao_id': a.id, 'status': 'entregue'})
    assert r.status_code == 200 and r.get_json()['ok'] is True


def test_nao_entregue_nao_exige_foto(app):
    """Problema na entrega não pode ficar preso atrás de foto."""
    from app.models import AtribuicaoEntrega
    d = _driver()
    codes = _rota(d, n=1)
    a = AtribuicaoEntrega.query.filter_by(pedido_code=codes[0]).first()
    c = _client_driver(app, d)
    r = c.post(f'/driver/api/{d.token}/status',
               json={'atribuicao_id': a.id, 'status': 'nao_entregue',
                     'motivo_falha': 'ausente'})
    assert r.status_code == 200
