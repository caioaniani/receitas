"""Vigia de custo de IA (11/07/2026) — teto diário + alerta WhatsApp.

O /admin/uso-ia é passivo (só informa quando o dono abre); um loop de bot
dispararia custo em silêncio. O vigia compara o gasto de HOJE (UsoIA) com
o teto USO_IA_TETO_DIA_USD e alerta na transição, com anti-spam de 6h e
aviso de normalização — mesmo padrão do vigia do site.
"""
from decimal import Decimal
from unittest.mock import patch

from app.extensions import db
from app.models import AppConfig, UsoIA


def _uso(funcao='vigia', custo='0.50', modelo='claude-sonnet-4-6'):
    u = UsoIA(funcao=funcao, modelo=modelo, input_tokens=1000,
              output_tokens=100,
              custo_usd=None if custo is None else Decimal(custo))
    db.session.add(u)
    db.session.commit()
    return u


def test_saudavel_abaixo_do_teto(app, monkeypatch):
    from app.services import uso_ia_vigia
    monkeypatch.delenv('USO_IA_TETO_DIA_USD', raising=False)
    with app.app_context():
        _uso(custo='0.10')
        out = uso_ia_vigia.rodar_checks()
        assert out['saudavel'] is True
        assert out['teto_usd'] == 25.0          # default sem env
        assert out['gasto_usd'] == 0.10


def test_estouro_acusado_com_top_funcoes(app, monkeypatch):
    from app.services import uso_ia_vigia
    monkeypatch.setenv('USO_IA_TETO_DIA_USD', '1')
    with app.app_context():
        _uso('chatbot_vigia', '0.80')
        _uso('bot_atendimento', '0.50')
        out = uso_ia_vigia.rodar_checks()
        assert out['saudavel'] is False
        assert 'passou do teto' in out['problemas'][0]
        assert out['gasto_usd'] == 1.30
        assert out['top'][0] == ('chatbot_vigia', 0.80)


def test_chamada_sem_preco_nao_soma_mas_aparece(app):
    """Modelo desconhecido fica custo_usd=NULL — não soma no gasto, mas o
    resultado expõe a contagem (gasto real pode ser maior)."""
    from app.services import uso_ia_vigia
    with app.app_context():
        _uso('copilot', None, modelo='modelo-misterioso')
        _uso('vigia', '0.20')
        out = uso_ia_vigia.rodar_checks()
        assert out['gasto_usd'] == 0.20
        assert out['sem_preco'] == 1


def test_teto_invalido_cai_no_default(app, monkeypatch):
    from app.services import uso_ia_vigia
    monkeypatch.setenv('USO_IA_TETO_DIA_USD', 'banana')
    assert uso_ia_vigia.teto_dia_usd() == Decimal('25')
    monkeypatch.setenv('USO_IA_TETO_DIA_USD', '-3')
    assert uso_ia_vigia.teto_dia_usd() == Decimal('25')


def test_vigiar_alerta_suprime_e_recupera(app, monkeypatch):
    """Estourou → 1 alerta WhatsApp (2ª rodada < 6h não re-spamma; o gasto
    crescente NÃO muda a assinatura); abaixo do teto de novo → aviso de
    normalização e estado limpo."""
    from app.services import uso_ia_vigia
    monkeypatch.setenv('USO_IA_TETO_DIA_USD', '1')
    with app.app_context():
        app.config['ZAPI_BOT_DONO_NUMERO'] = '5511999999999'
        _uso('chatbot_vigia', '1.50')

        with patch('app.services.zapi.enviar_texto') as tx:
            r1 = uso_ia_vigia.vigiar()
            _uso('chatbot_vigia', '0.30')       # gasto seguiu subindo
            r2 = uso_ia_vigia.vigiar()
        assert r1['tipo'] == 'alerta' and r1['enviado'] is True
        assert r2['tipo'] == 'alerta_suprimido'  # mesmo teto, < 6h
        assert tx.call_count == 1
        assert 'Custo de IA' in tx.call_args[0][1]
        assert 'chatbot_vigia' in tx.call_args[0][1]

        UsoIA.query.delete()
        db.session.commit()
        with patch('app.services.zapi.enviar_texto') as tx2:
            r3 = uso_ia_vigia.vigiar()
        assert r3['tipo'] == 'recuperacao'
        assert 'normalizou' in tx2.call_args[0][1]
        assert AppConfig.get('uso_ia_vigia_estourado_desde') is None


def test_rota_owner_roda_checks(app, owner_user):
    with app.app_context():
        c = app.test_client()
        with c.session_transaction() as sess:
            sess['_user_id'] = str(owner_user.id)
            sess['_fresh'] = True
        resp = c.get('/admin/vigia-uso-ia')
        assert resp.status_code == 200
        assert resp.get_json()['saudavel'] is True
