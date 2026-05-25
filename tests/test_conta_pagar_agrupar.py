"""Agrupamento automatico de NF + boleto do mesmo recebimento."""
from datetime import date
from decimal import Decimal


def _conta(db, **kw):
    from app.models import ContaPagar
    base = dict(origem_canal='C_RIB', valor_total=Decimal('141.31'),
                vencimento=date(2026, 5, 7), status='aberto',
                tipo_documento='nota_fiscal')
    base.update(kw)
    c = ContaPagar(**base)
    db.session.add(c)
    db.session.commit()
    return c


def test_agrupa_par_mesma_loja_valor_vencimento(app):
    from app.extensions import db
    from app.services import conta_pagar as cp
    with app.app_context():
        nf = _conta(db, tipo_documento='nota_fiscal')
        boleto = _conta(db, tipo_documento='boleto')
        n = cp.agrupar_automatico()
        assert n == 1
        # o de maior id vira secundario, aponta pro principal (menor id)
        db.session.refresh(nf)
        db.session.refresh(boleto)
        principais = [c for c in (nf, boleto) if c.relacionado_id is None]
        secundarios = [c for c in (nf, boleto) if c.relacionado_id is not None]
        assert len(principais) == 1 and len(secundarios) == 1
        assert secundarios[0].relacionado_id == principais[0].id
        # bidirecional: principal "ligados" acha o secundario
        assert secundarios[0] in principais[0].ligados


def test_nao_agrupa_valores_diferentes(app):
    from app.extensions import db
    from app.services import conta_pagar as cp
    with app.app_context():
        _conta(db, valor_total=Decimal('141.31'))
        _conta(db, valor_total=Decimal('200.00'))
        assert cp.agrupar_automatico() == 0


def test_nao_agrupa_lojas_diferentes(app):
    from app.extensions import db
    from app.services import conta_pagar as cp
    with app.app_context():
        _conta(db, origem_canal='C_RIB')
        _conta(db, origem_canal='C_NEB')
        assert cp.agrupar_automatico() == 0


def test_nao_agrupa_sem_vencimento(app):
    from app.extensions import db
    from app.services import conta_pagar as cp
    with app.app_context():
        _conta(db, vencimento=None)
        _conta(db, vencimento=None)
        assert cp.agrupar_automatico() == 0


def test_idempotente(app):
    from app.extensions import db
    from app.services import conta_pagar as cp
    with app.app_context():
        _conta(db, tipo_documento='nota_fiscal')
        _conta(db, tipo_documento='boleto')
        assert cp.agrupar_automatico() == 1
        assert cp.agrupar_automatico() == 0  # nada novo


def test_tentar_agrupar_nova_conta(app):
    from app.extensions import db
    from app.services import conta_pagar as cp
    with app.app_context():
        nf = _conta(db, tipo_documento='nota_fiscal')
        boleto = _conta(db, tipo_documento='boleto')
        assert cp.tentar_agrupar(boleto) is True
        db.session.refresh(boleto)
        assert boleto.relacionado_id == nf.id


def test_trio_mesma_chave(app):
    """3 documentos do mesmo recebimento → 1 principal, 2 secundarios."""
    from app.extensions import db
    from app.services import conta_pagar as cp
    with app.app_context():
        _conta(db, tipo_documento='nota_fiscal')
        _conta(db, tipo_documento='boleto')
        _conta(db, tipo_documento='boleto')
        assert cp.agrupar_automatico() == 2


def test_agrupa_por_numero_documento_vencimentos_diferentes(app):
    """NF e boleto juntam pelo numero do documento mesmo com vencimentos
    diferentes (boleto traz o 'No documento' = numero da NF, com zeros)."""
    from app.extensions import db
    from app.services import conta_pagar as cp
    with app.app_context():
        nf = _conta(db, tipo_documento='nota_fiscal', nf_numero='000053498',
                    vencimento=date(2026, 5, 8))
        boleto = _conta(db, tipo_documento='boleto', nf_numero='53498',
                        vencimento=date(2026, 6, 2))
        assert cp.agrupar_automatico() == 1
        db.session.refresh(nf)
        db.session.refresh(boleto)
        principais = [c for c in (nf, boleto) if c.relacionado_id is None]
        secundarios = [c for c in (nf, boleto) if c.relacionado_id is not None]
        assert len(principais) == 1 and len(secundarios) == 1
        assert secundarios[0].relacionado_id == principais[0].id


def test_nao_agrupa_numeros_diferentes(app):
    """Numeros de documento diferentes nao juntam, mesmo com valor igual."""
    from app.extensions import db
    from app.services import conta_pagar as cp
    with app.app_context():
        _conta(db, tipo_documento='nota_fiscal', nf_numero='111',
               vencimento=date(2026, 5, 8))
        _conta(db, tipo_documento='boleto', nf_numero='222',
               vencimento=date(2026, 6, 2))
        assert cp.agrupar_automatico() == 0
