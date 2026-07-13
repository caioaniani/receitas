"""Vigia do PDV (07/07/2026): avisa quando a baixa de venda para de funcionar
— o incidente da Ribeiro (renome no Seru) ficou 2 semanas invisível."""
from datetime import timedelta
from unittest.mock import patch

from app.extensions import db
from app.models import EstoqueLoja, Loja, MovEstoqueLoja, Receita, SeruLojaMap
from app.services.pdv_vigia import rodar_checks, vigiar
from app.utils import agora, hoje


def _loja_confirmada(nome, company):
    lj = Loja(nome=nome, ativa=True)
    db.session.add(lj)
    db.session.commit()
    db.session.add(SeruLojaMap(seru_company_name=company, loja_id=lj.id,
                               confirmado_em=agora()))
    db.session.commit()
    return lj


def _baixa(loja, horas_atras, qtd=10):
    # Ancorado em agora() (nao em meio-dia de hoje): teste rodando de
    # madrugada punha a baixa "recente" no FUTURO e flakava.
    r = Receita.query.first()
    if r is None:
        r = Receita(nome='Pao Vigia', rendimento_qtd=1,
                    rendimento_unidade='un', peso_base=100.0)
        db.session.add(r)
        db.session.commit()
    el = EstoqueLoja.query.filter_by(loja_id=loja.id, receita_id=r.id).first()
    if el is None:
        el = EstoqueLoja(loja_id=loja.id, receita_id=r.id, quantidade=0)
        db.session.add(el)
        db.session.flush()
    db.session.add(MovEstoqueLoja(
        estoque_loja_id=el.id, tipo='venda_seru', quantidade=qtd,
        data=agora() - timedelta(hours=horas_atras),
        referencia='vigia-teste'))
    db.session.commit()


def _sync_ok():
    return patch('app.services.pdv_saude.resumo',
                 return_value={'seru_atrasado': False})


def _vazao_ok(total=300):
    """Mocka a API do Seru pro check 4 (vazao) — sem isso, o teste bateria
    na rede de verdade (e o resultado dependeria da hora local)."""
    return patch('app.services.seru.listar_pedidos',
                 return_value={'data': [{'id': 'x'}], 'totalPages': total})


def test_loja_que_vendia_e_ficou_muda_detecta(app):
    lj = _loja_confirmada('Loja Ribeiro do Vale', 'O PAO PADARIA')
    for h in (100, 150, 220):
        _baixa(lj, h)                       # vendia no histórico
    with _sync_ok(), _vazao_ok():
        out = rodar_checks()                # nada nas últimas 36h
    assert not out['saudavel']
    assert any('Ribeiro do Vale' in p and 'sem' in p.lower()
               for p in out['problemas'])


def test_loja_vendendo_normal_e_saudavel(app):
    lj = _loja_confirmada('Loja Ativa', 'CIA ATIVA')
    for h in (1, 60, 120):
        _baixa(lj, h)                       # inclui baixa recente
    with _sync_ok(), _vazao_ok():
        out = rodar_checks()
    assert out['saudavel'] is True


def test_company_vendendo_sem_vinculo_confirmado_detecta(app):
    from app.models import VendaSeruDiaria
    db.session.add(SeruLojaMap(seru_company_name='CIA NOVA SEM CONFIRMA'))
    db.session.add(VendaSeruDiaria(data=hoje(), loja_seru='CIA NOVA SEM CONFIRMA',
                                   seru_nome='CROISSANT', qtd=30,
                                   faturamento=100))
    db.session.commit()
    with _sync_ok(), _vazao_ok():
        out = rodar_checks()
    assert any('CIA NOVA SEM CONFIRMA' in p and 'NAO baixam' in p
               for p in out['problemas'])


def test_vigiar_alerta_na_transicao_e_avisa_normalizacao(app):
    app.config['ZAPI_BOT_DONO_NUMERO'] = '5511999998888'
    lj = _loja_confirmada('Loja Muda', 'CIA MUDA')
    for h in (100, 150):
        _baixa(lj, h)
    with _sync_ok(), _vazao_ok(), patch('app.services.zapi.enviar_texto') as tx:
        r1 = vigiar()                       # doente → alerta
        r2 = vigiar()                       # mesmo problema → suprimido (6h)
    assert r1['tipo'] == 'alerta' and r2['tipo'] == 'alerta_suprimido'
    assert tx.call_count == 1
    assert 'Vigia do PDV' in tx.call_args[0][1]
    _baixa(lj, 1)                           # voltou a vender
    with _sync_ok(), _vazao_ok(), patch('app.services.zapi.enviar_texto') as tx2:
        r3 = vigiar()
    assert r3['tipo'] == 'recuperacao'
    assert 'normalizou' in tx2.call_args[0][1]


# ── Check 4: vazao na FONTE (13/07/2026, incidente das companies) ─────────
def _hora(h, m=0):
    from datetime import datetime
    d = hoje()
    return datetime(d.year, d.month, d.day, h, m)


def test_vazao_abaixo_do_piso_detecta(app):
    with _sync_ok(), _vazao_ok(total=1), \
         patch('app.utils.agora', return_value=_hora(11, 30)):
        out = rodar_checks()
    assert not out['saudavel']
    assert any('nao estao chegando na API do Seru' in p
               for p in out['problemas'])


def test_vazao_normal_e_saudavel(app):
    with _sync_ok(), _vazao_ok(total=240), \
         patch('app.utils.agora', return_value=_hora(14, 10)):
        out = rodar_checks()
    assert out['saudavel'] is True


def test_vazao_fora_de_horario_fica_quieta(app):
    """As 6h, 0 pedidos e normal — o piso so vale em horario de loja."""
    with _sync_ok(), _vazao_ok(total=0), \
         patch('app.utils.agora', return_value=_hora(6, 0)):
        out = rodar_checks()
    assert out['saudavel'] is True


def test_api_fora_no_check_de_vazao_e_achado(app):
    with _sync_ok(), \
         patch('app.services.seru.listar_pedidos',
               side_effect=RuntimeError('Seru auth 500')), \
         patch('app.utils.agora', return_value=_hora(11, 0)):
        out = rodar_checks()
    assert not out['saudavel']
    assert any('API do Seru fora' in p for p in out['problemas'])
