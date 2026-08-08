"""'Pular endereço' do motorista (08/08/2026, dono, véspera do Dia dos
Pais): portaria não recebeu → foto OBRIGATÓRIA provando que esteve lá + o
pedido vai pro FIM da rota, seguindo PENDENTE pra voltar depois. NÃO é
nao_entregue (desfecho final). Entregue pós-pulo exige foto NOVA (tirada
depois do pulo — a foto da portaria não comprova entrega)."""
from datetime import timedelta

from app.extensions import db
from app.models import AtribuicaoEntrega, Driver, EntregaFoto, PedidoOnline
from app.utils import agora, hoje


def _driver(nome='Zé Pula', token='tok-pula-1234'):
    d = Driver(nome=nome, ativo=True, token=token)
    db.session.add(d)
    db.session.commit()
    return d


def _rota(d, n=3):
    codes = []
    for i in range(n):
        p = PedidoOnline(codigo=f'PU{i}', nome_cliente=f'C{i}',
                         email_cliente=f'c{i}@x.com', status='pago',
                         modo_entrega='agendada', data_entrega=hoje(),
                         janela_entrega='06:00–10:00', valor_total=100)
        db.session.add(p)
        db.session.add(AtribuicaoEntrega(pedido_code=f'PU{i}', driver_id=d.id,
                                         data_entrega=hoje(), ordem=i + 1))
        codes.append(f'PU{i}')
    db.session.commit()
    return codes


def _atrib(code):
    return AtribuicaoEntrega.query.filter_by(pedido_code=code).first()


def _foto(a, quando=None):
    f = EntregaFoto(atribuicao_id=a.id, url='https://x.example/f.jpg',
                    tirada_em=quando or agora())
    db.session.add(f)
    db.session.commit()
    return f


def _client(app, d):
    c = app.test_client()
    with c.session_transaction() as s:
        s[f'driver_auth_{d.id}'] = True
    return c


def test_pular_sem_foto_e_recusado(app):
    d = _driver()
    codes = _rota(d, n=1)
    a = _atrib(codes[0])
    c = _client(app, d)
    r = c.post(f'/driver/api/{d.token}/pular', json={'atribuicao_id': a.id})
    assert r.status_code == 422
    assert r.get_json()['precisa_foto'] is True
    db.session.refresh(a)
    assert a.pulado_em is None and a.ordem == 1     # nada mudou


def test_pular_com_foto_vai_pro_fim_da_rota_e_segue_pendente(app):
    d = _driver()
    codes = _rota(d, n=3)
    a = _atrib(codes[0])                            # 1ª parada pula
    _foto(a)
    c = _client(app, d)
    r = c.post(f'/driver/api/{d.token}/pular',
               json={'atribuicao_id': a.id, 'nota': 'portaria não recebeu'})
    assert r.status_code == 200
    assert r.get_json()['ok'] is True
    db.session.refresh(a)
    assert a.ordem == 4                             # max(3) + 1 = fim da rota
    assert a.pulado_em is not None
    assert a.status == 'pendente'                   # NÃO vira nao_entregue
    assert a.nota == 'portaria não recebeu'


def test_pular_reposiciona_o_cliente_no_rastreio(app):
    """O cliente pulado vê a posição cair pro fim; os outros avançam."""
    from app.models import RotaInicio
    from app.services import rastreio_entrega as svc
    d = _driver()
    codes = _rota(d, n=3)
    db.session.add(RotaInicio(driver_id=d.id, data=hoje(),
                              iniciado_em=agora(), emails_em=agora()))
    db.session.commit()
    a = _atrib(codes[0])
    _foto(a)
    c = _client(app, d)
    assert c.post(f'/driver/api/{d.token}/pular',
                  json={'atribuicao_id': a.id}).status_code == 200
    s_pulado = svc.status_do_pedido(codes[0])
    assert s_pulado['fase'] == 'a_caminho'
    assert s_pulado['parada'] == 3                  # foi pro fim da fila
    s_outro = svc.status_do_pedido(codes[1])
    assert s_outro['parada'] == 1                   # avançou (era a 2ª)


def test_so_pendente_pula(app):
    d = _driver()
    codes = _rota(d, n=1)
    a = _atrib(codes[0])
    _foto(a)
    a.status = 'entregue'
    a.entregue_em = agora()
    db.session.commit()
    c = _client(app, d)
    r = c.post(f'/driver/api/{d.token}/pular', json={'atribuicao_id': a.id})
    assert r.status_code == 422


def test_pular_pedido_de_outro_driver_403(app):
    d1 = _driver()
    d2 = _driver(nome='Outro', token='tok-outro-9999')
    codes = _rota(d1, n=1)
    a = _atrib(codes[0])
    _foto(a)
    c = _client(app, d2)
    r = c.post(f'/driver/api/{d2.token}/pular', json={'atribuicao_id': a.id})
    assert r.status_code == 403


def test_entregue_pos_pulo_exige_foto_nova(app):
    """A foto da portaria (anterior ao pulo) NÃO vale como comprovante de
    entrega — o entregue pós-pulo recusa até vir foto tirada DEPOIS."""
    d = _driver()
    codes = _rota(d, n=1)
    a = _atrib(codes[0])
    _foto(a, quando=agora() - timedelta(minutes=10))    # foto da fachada
    c = _client(app, d)
    assert c.post(f'/driver/api/{d.token}/pular',
                  json={'atribuicao_id': a.id}).status_code == 200
    # Volta mais tarde e tenta entregar SÓ com a foto antiga:
    r = c.post(f'/driver/api/{d.token}/status',
               json={'atribuicao_id': a.id, 'status': 'entregue'})
    assert r.status_code == 422
    assert r.get_json()['precisa_foto'] is True
    # Foto NOVA (depois do pulo) libera o entregue:
    db.session.refresh(a)
    _foto(a, quando=a.pulado_em + timedelta(minutes=30))
    r2 = c.post(f'/driver/api/{d.token}/status',
                json={'atribuicao_id': a.id, 'status': 'entregue'})
    assert r2.status_code == 200
    db.session.refresh(a)
    assert a.status == 'entregue'


def test_api_pedidos_expoe_pulado_e_precisa_foto_nova(app):
    d = _driver()
    codes = _rota(d, n=2)
    a = _atrib(codes[0])
    _foto(a, quando=agora() - timedelta(minutes=5))
    c = _client(app, d)
    assert c.post(f'/driver/api/{d.token}/pular',
                  json={'atribuicao_id': a.id}).status_code == 200
    r = c.get(f'/driver/api/{d.token}/pedidos?data={hoje().isoformat()}')
    dados = r.get_json()
    por_code = {p['code']: p for p in dados['pedidos']}
    pulado = por_code[codes[0]]
    assert pulado['pulado_em'] is not None
    assert pulado['precisa_foto_nova'] is True      # só tem a foto da fachada
    assert por_code[codes[1]]['pulado_em'] is None
    # Lista reordenada: o pulado aparece POR ÚLTIMO
    assert dados['pedidos'][-1]['code'] == codes[0]
