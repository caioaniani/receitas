"""Plano de estoque do site por dia (22/06/2026 — decisao do dono).

Permite "hoje 0 foccacia, sexta 20" sem mexer no estoque fisico. Reserva
acontece no webhook pagar.me (pedido pago). Devolucao no cancelamento.
"""
from datetime import date


def test_saldo_sem_plano_retorna_none(app):
    """Sem linha cadastrada = 'None' (sinaliza "sem controle pra esse dia")."""
    from app.services import loja_plano_dia
    assert loja_plano_dia.saldo('receita', 1, date(2026, 6, 26)) is None


def test_definir_e_saldo(app):
    from app.extensions import db
    from app.services import loja_plano_dia
    loja_plano_dia.definir('receita', 1, date(2026, 6, 26), 20)
    assert loja_plano_dia.saldo('receita', 1, date(2026, 6, 26)) == 20
    # Atualiza (upsert)
    loja_plano_dia.definir('receita', 1, date(2026, 6, 26), 5)
    assert loja_plano_dia.saldo('receita', 1, date(2026, 6, 26)) == 5
    # Data diferente eh independente
    assert loja_plano_dia.saldo('receita', 1, date(2026, 6, 27)) is None
    db.session.expire_all()


def test_reservar_e_devolver(app):
    from app.services import loja_plano_dia
    d = date(2026, 6, 26)
    loja_plano_dia.definir('produto', 10, d, 3)
    # Reserva 1: passa, sobra 2
    assert loja_plano_dia.reservar('produto', 10, d, 1) is True
    assert loja_plano_dia.saldo('produto', 10, d) == 2
    # Reserva 2: passa, sobra 0
    assert loja_plano_dia.reservar('produto', 10, d, 2) is True
    assert loja_plano_dia.saldo('produto', 10, d) == 0
    # Reserva 1 a mais: NAO passa (sem saldo)
    assert loja_plano_dia.reservar('produto', 10, d, 1) is False
    # Devolve 1: volta a sobrar 1
    loja_plano_dia.devolver('produto', 10, d, 1)
    assert loja_plano_dia.saldo('produto', 10, d) == 1


def test_reservar_sem_plano_cria_linha_negativa(app):
    """Cliente compra item sem plano — vende mesmo assim mas deixa rastro de
    saldo negativo virtual (qtd_planejada=0, qtd_reservada=qtd) pra auditoria."""
    from app.extensions import db
    from app.models import EstoqueSitePlano
    from app.services import loja_plano_dia
    d = date(2026, 6, 26)
    assert loja_plano_dia.reservar('receita', 99, d, 2) is True
    row = (db.session.query(EstoqueSitePlano)
           .filter_by(kind='receita', item_id=99, data=d).one())
    assert row.qtd_planejada == 0
    assert row.qtd_reservada == 2
    # Saldo continua 0 (max(0, 0-2)=0) — exibicao ok; auditoria pega no row.


def test_devolver_sem_linha_no_op(app):
    """Devolver sem ter reservado nada antes: ignora silenciosamente."""
    from app.services import loja_plano_dia
    loja_plano_dia.devolver('produto', 555, date(2026, 6, 26), 5)  # nao da erro


def test_tem_plano_e_saldos_para_dia(app):
    from app.services import loja_plano_dia
    d = date(2026, 6, 26)
    assert loja_plano_dia.tem_plano(d) is False
    loja_plano_dia.definir('receita', 1, d, 10)
    loja_plano_dia.definir('produto', 2, d, 5)
    assert loja_plano_dia.tem_plano(d) is True
    # Outra data nao tem plano
    assert loja_plano_dia.tem_plano(date(2026, 6, 27)) is False
    # Saldos por dia
    saldos = loja_plano_dia.saldos_para_dia(d)
    assert saldos == {('receita', 1): 10, ('produto', 2): 5}


def test_definir_qtd_negativa_rejeita(app):
    """Plano negativo nao faz sentido — caller passou errado."""
    import pytest

    from app.services import loja_plano_dia
    with pytest.raises(ValueError):
        loja_plano_dia.definir('receita', 1, date(2026, 6, 26), -1)


# ── Integração com loja_pagamento (reserva ao pagar + devolve ao cancelar) ──

def _pedido_basico(db, codigo, status='aguardando_pagamento',
                   data_entrega=None, qtds=None):
    """Cria PedidoOnline com itens. qtds = [(kind, item_id, qtd), ...]."""
    from datetime import date as _date
    from decimal import Decimal

    from app.models import PedidoOnline, PedidoOnlineItem
    p = PedidoOnline(codigo=codigo, nome_cliente='C',
                     email_cliente='c@x.com', modo_entrega='agendada',
                     status=status, subtotal=Decimal('100'),
                     valor_total=Decimal('100'),
                     data_entrega=data_entrega or _date(2026, 6, 26))
    db.session.add(p)
    db.session.flush()
    for kind, item_id, qtd in (qtds or []):
        kwargs = dict(pedido_id=p.id, nome=f'{kind}-{item_id}',
                      quantidade=qtd, preco_unitario=Decimal('1'),
                      subtotal=Decimal(str(qtd)))
        if kind == 'receita':
            kwargs['kind'] = 'receita'
            kwargs['receita_id'] = item_id
        else:
            kwargs['kind'] = 'produto'
            kwargs['produto_id'] = item_id
        db.session.add(PedidoOnlineItem(**kwargs))
    db.session.commit()
    return p


def test_pagar_reserva_no_plano(app):
    """Quando o webhook marca o pedido como pago, reserva no plano da
    data_entrega — usa o caminho de producao (_marcar_pago)."""
    from datetime import date

    from app.extensions import db
    from app.services import loja_pagamento, loja_plano_dia

    dia = date(2026, 6, 26)
    loja_plano_dia.definir('receita', 7, dia, 10)
    p = _pedido_basico(db, 'P1', data_entrega=dia,
                       qtds=[('receita', 7, 3)])

    loja_pagamento._marcar_pago(p, None)
    assert p.status == 'pago'
    assert loja_plano_dia.saldo('receita', 7, dia) == 7  # 10 - 3


def test_cancelar_pago_devolve_ao_plano(app):
    """Pedido pago → cancelado: devolve a reserva pro plano."""
    from datetime import date

    from app.extensions import db
    from app.models import PagamentoOnline
    from app.services import loja_pagamento, loja_plano_dia

    dia = date(2026, 6, 26)
    loja_plano_dia.definir('produto', 9, dia, 5)
    p = _pedido_basico(db, 'P2', data_entrega=dia,
                       qtds=[('produto', 9, 2)])
    loja_pagamento._marcar_pago(p, None)
    assert loja_plano_dia.saldo('produto', 9, dia) == 3

    # Simula cancelamento direto (sem ir pelo reembolso_pedido pra evitar
    # depender da api do Pagar.me no teste).
    pg = PagamentoOnline.query.filter_by(pedido_id=p.id).first()
    loja_pagamento._marcar_estornado(p, pg)
    assert p.status == 'cancelado'
    assert loja_plano_dia.saldo('produto', 9, dia) == 5  # voltou


def test_cancelar_aguardando_nao_devolve(app):
    """aguardando_pagamento nunca chegou a reservar — cancelar nao mexe no
    plano. (Sem isso, devolver no plano viraria saldo POSITIVO falso.)"""
    from datetime import date

    from app.extensions import db
    from app.services import loja_pagamento, loja_plano_dia

    dia = date(2026, 6, 26)
    loja_plano_dia.definir('receita', 7, dia, 10)
    p = _pedido_basico(db, 'P3', status='aguardando_pagamento',
                       data_entrega=dia, qtds=[('receita', 7, 4)])

    loja_pagamento._marcar_estornado(p, None)
    # Continua 10 — nada foi reservado, nada devolvido.
    assert loja_plano_dia.saldo('receita', 7, dia) == 10


def test_pagar_sem_data_entrega_nao_quebra(app):
    """Pedido sem data_entrega (raro): pula reserva no plano sem erro."""
    from app.extensions import db
    from app.services import loja_pagamento

    p = _pedido_basico(db, 'P4', data_entrega=None,
                       qtds=[('receita', 7, 1)])
    p.data_entrega = None
    db.session.commit()
    loja_pagamento._marcar_pago(p, None)  # nao pode levantar
    assert p.status == 'pago'


# ── Etapa 4: vitrine + pagina produto + checkout usam o plano por dia ──────

def test_vitrine_marca_esgotado_hoje_mas_disponivel_outro_dia(app):
    """Cliente vai na home. Item com plano hoje=0 e plano sexta=5: vitrine
    marca `esgotado_hoje=True` E `tem_em_outros_dias=True` E `esgotado=False`
    (etiqueta amarela 'Esgotado HOJE - compre pra outro dia')."""
    from datetime import date, timedelta

    from app.extensions import db
    from app.models import Receita
    from app.services import loja_catalogo, loja_plano_dia
    from app.utils import hoje

    r = Receita(nome='Foccacia', categoria='Paes', preco_site=18.0,
                imagem_dropbox_url='https://x/f.jpg',
                rendimento_qtd=1, rendimento_unidade='un', peso_base=300.0)
    db.session.add(r)
    db.session.commit()

    dia_hoje = hoje()
    loja_plano_dia.definir('receita', r.id, dia_hoje, 0)              # 0 hoje
    loja_plano_dia.definir('receita', r.id, dia_hoje + timedelta(days=3), 5)
    itens = [{'kind': 'receita', 'id': r.id, 'nome': 'Foccacia'}]
    loja_catalogo.anotar_esgotado(itens)
    assert itens[0]['esgotado_hoje'] is True
    assert itens[0]['tem_em_outros_dias'] is True
    assert itens[0]['esgotado'] is False  # nao mostra "esgotado duro"


def test_vitrine_esgotado_duro_quando_zerado_em_todos(app):
    """Sem saldo em nenhum dos proximos 14 dias = esgotado duro
    (etiqueta vermelha + bloqueia compra)."""
    from datetime import date, timedelta

    from app.extensions import db
    from app.models import Receita
    from app.services import loja_catalogo, loja_plano_dia
    from app.utils import hoje

    r = Receita(nome='Foccacia', categoria='Paes', preco_site=18.0,
                rendimento_qtd=1, rendimento_unidade='un', peso_base=300.0)
    db.session.add(r)
    db.session.commit()

    dia = hoje()
    for i in range(14):
        loja_plano_dia.definir('receita', r.id, dia + timedelta(days=i), 0)

    itens = [{'kind': 'receita', 'id': r.id, 'nome': 'Foccacia'}]
    loja_catalogo.anotar_esgotado(itens)
    assert itens[0]['esgotado_hoje'] is True
    assert itens[0]['tem_em_outros_dias'] is False
    assert itens[0]['esgotado'] is True


def test_api_disponibilidade_dia(app):
    """Pagina de produto: cliente muda data, JS pergunta /api/disponibilidade-dia
    e ve 'disponivel' ou nao."""
    from datetime import date, timedelta

    from app.extensions import db
    from app.models import Receita
    from app.services import loja_plano_dia
    from app.utils import hoje

    r = Receita(nome='Foccacia', categoria='Paes', preco_site=18.0,
                rendimento_qtd=1, rendimento_unidade='un', peso_base=300.0)
    db.session.add(r)
    db.session.commit()

    dia_hoje = hoje()
    loja_plano_dia.definir('receita', r.id, dia_hoje, 0)
    loja_plano_dia.definir('receita', r.id, dia_hoje + timedelta(days=2), 5)
    c = app.test_client()
    # Hoje: esgotado
    j = c.get(f'/loja/api/disponibilidade-dia?kind=receita&item_id={r.id}'
              f'&data={dia_hoje.isoformat()}').get_json()
    assert j['disponivel'] is False
    # +2 dias: disponivel
    j2 = c.get(f'/loja/api/disponibilidade-dia?kind=receita&item_id={r.id}'
               f'&data={(dia_hoje + timedelta(days=2)).isoformat()}').get_json()
    assert j2['disponivel'] is True


def test_checkout_recusa_com_nome_e_data_quando_sem_saldo(app):
    """Cliente escolhe data Y mas algum item nao tem saldo no plano de Y.
    Erro do checkout DEVE mencionar o NOME do item e a DATA pra ficar claro
    qual produto/qual data quebrou."""
    from datetime import timedelta

    from app.extensions import db
    from app.models import Receita
    from app.services import loja_checkout, loja_plano_dia
    from app.utils import agora, hoje

    r = Receita(nome='Foccacia', categoria='Paes', preco_site=18.0,
                imagem_dropbox_url='https://x/f.jpg',
                rendimento_qtd=1, rendimento_unidade='un', peso_base=300.0)
    db.session.add(r)
    db.session.commit()

    dia_alvo = hoje() + timedelta(days=2)
    loja_plano_dia.definir('receita', r.id, dia_alvo, 0)  # esgotado pra esse dia

    form = {
        'nome': 'Cliente', 'email': 'c@x.com', 'cpf': '11111111111',
        'telefone': '11999999999',
        'modo_entrega': 'agendada',
        'aceite_lgpd': '1',
        'data_entrega': dia_alvo.isoformat(),
        'janela_entrega': '08:00-09:00',
        'cep': '04077000', 'logradouro': 'Rua X', 'numero': '1',
        'cidade': 'São Paulo',
    }
    itens_raw = [{'kind': 'receita', 'id': r.id, 'qtd': 1}]
    _, erros = loja_checkout.criar_pedido(form, itens_raw, base=agora())
    # A msg precisa identificar o produto + a data
    msg = ' '.join(erros)
    assert 'Foccacia' in msg
    assert dia_alvo.strftime('%d/%m/%Y') in msg
