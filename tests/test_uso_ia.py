"""Instrumentacao de custo de IA (app/services/uso_ia.py + modelo UsoIA) e as
decisoes de modelo por funcao (25/06/2026).

Trava DUAS coisas:
1. O calculo de custo e o registro/agregacao por funcao.
2. Quais modelos cada funcao usa (decisao do dono) — pra nao regredir sem querer.
"""
from decimal import Decimal


def _login(client, user):
    with client.session_transaction() as sess:
        sess['_user_id'] = str(user.id)
        sess['_fresh'] = True


# ── Calculo de custo ───────────────────────────────────────


def test_custo_opus():
    from app.services.uso_ia import calcular_custo
    # 1M input + 1M output em Opus 4.8 = $5 + $25 = $30
    c = calcular_custo('claude-opus-4-8', 1_000_000, 1_000_000)
    assert c == Decimal('30')


def test_custo_sonnet():
    from app.services.uso_ia import calcular_custo
    # 1M in + 1M out Sonnet 4.6 = $3 + $15 = $18
    assert calcular_custo('claude-sonnet-4-6', 1_000_000, 1_000_000) == Decimal('18')


def test_custo_haiku_com_sufixo():
    from app.services.uso_ia import calcular_custo
    # casa por prefixo, mesmo com sufixo de data
    c = calcular_custo('claude-haiku-4-5-20251001', 1_000_000, 0)
    assert c == Decimal('1')


def test_custo_cache_read_eh_um_decimo():
    from app.services.uso_ia import calcular_custo
    # 1M cache_read em Opus = 0.1 * $5 = $0.5
    c = calcular_custo('claude-opus-4-8', 0, 0, cache_read=1_000_000)
    assert c == Decimal('0.5')


def test_custo_modelo_desconhecido_none():
    from app.services.uso_ia import calcular_custo
    assert calcular_custo('gpt-9', 1000, 1000) is None


# ── registrar + resumo ─────────────────────────────────────


class _FakeUsage:
    def __init__(self, i, o, cr=0, cc=0):
        self.input_tokens = i
        self.output_tokens = o
        self.cache_read_input_tokens = cr
        self.cache_creation_input_tokens = cc


def test_registrar_persiste(app):
    from app.models import UsoIA
    from app.services import uso_ia
    with app.app_context():
        uso_ia.registrar('vigia', 'claude-sonnet-4-6', _FakeUsage(1000, 200))
        row = UsoIA.query.filter_by(funcao='vigia').first()
        assert row is not None
        assert row.input_tokens == 1000
        assert row.output_tokens == 200
        # 1000*3/1e6 + 200*15/1e6 = 0.003 + 0.003 = 0.006
        assert row.custo_usd == Decimal('0.006000')


def test_registrar_best_effort_nao_quebra(app):
    """usage zoado / modelo None nao pode levantar excecao (best-effort)."""
    from app.services import uso_ia
    with app.app_context():
        uso_ia.registrar('x', None, None)  # nao deve levantar
        uso_ia.registrar('y', 'modelo-invalido', _FakeUsage(10, 10))


def test_resumo_agrega_por_funcao_ordenado(app):
    from app.services import uso_ia
    with app.app_context():
        uso_ia.registrar('vigia', 'claude-sonnet-4-6', _FakeUsage(1000, 100))
        uso_ia.registrar('vigia', 'claude-sonnet-4-6', _FakeUsage(1000, 100))
        uso_ia.registrar('seo', 'claude-sonnet-4-6', _FakeUsage(100, 10))
        r = uso_ia.resumo(dias=7)
        funcoes = [d['funcao'] for d in r]
        assert 'vigia' in funcoes and 'seo' in funcoes
        vigia = next(d for d in r if d['funcao'] == 'vigia')
        assert vigia['chamadas'] == 2
        # ordenado por custo desc — vigia (mais tokens) antes de seo
        assert funcoes.index('vigia') < funcoes.index('seo')


# ── Rota /admin/uso-ia ─────────────────────────────────────


def test_rota_uso_ia_owner_ok(app, owner_user):
    from app.services import uso_ia
    with app.app_context():
        uso_ia.registrar('vigia', 'claude-sonnet-4-6', _FakeUsage(1000, 100))
    client = app.test_client()
    _login(client, owner_user)
    resp = client.get('/admin/uso-ia')
    assert resp.status_code == 200
    assert 'Vigia do bot'.encode() in resp.data  # label amigavel


def test_rota_uso_ia_exige_owner(app, admin_user):
    client = app.test_client()
    _login(client, admin_user)
    assert client.get('/admin/uso-ia').status_code == 403


# ── Regressao das decisoes de modelo (dono, 25/06/2026) ────


def test_modelos_por_funcao():
    """Sonnet 4.6 em tudo, EXCETO bot Chatwoot / WhatsApp dono / OCRs = Opus 4.8."""
    from app.services import (
        chatbot,
        chatbot_auditor,
        chatbot_vigia,
        conta_pagar_ia,
        copilot,
        seo_descricoes,
        zapi_bot,
    )
    # Subiram pra Sonnet
    assert chatbot_vigia.MODELO == 'claude-sonnet-4-6'
    assert chatbot.FOLLOWUP_MODELO == 'claude-sonnet-4-6'
    assert seo_descricoes.MODELO == 'claude-sonnet-4-6'
    # Ja eram Sonnet
    assert chatbot_auditor.MODELO == 'claude-sonnet-4-6'
    assert copilot.MODELO_DEFAULT == 'claude-sonnet-4-6'
    # Exceções em Opus 4.8
    assert chatbot.MODELO == 'claude-opus-4-8'                 # bot Chatwoot
    assert zapi_bot.MODELO_WHATSAPP_DEFAULT == 'claude-opus-4-8'  # WhatsApp dono
    assert conta_pagar_ia.MODELO.startswith('claude-opus-4-8')   # OCR contas


def test_ocr_cupom_usa_opus():
    """ocr_nota nao tem constante (modelo inline) — confere via codigo-fonte."""
    import inspect

    from app.services import ocr_nota
    src = inspect.getsource(ocr_nota)
    assert "modelo = 'claude-opus-4-8'" in src
    assert 'claude-sonnet-4-6' not in src  # nao sobrou o antigo
