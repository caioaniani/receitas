"""Dados artificiais para a homologacao visual.

Nunca le nem copia dados de producao. O seed so roda com PREVIEW_MODE=1 e e
idempotente, inclusive quando os workers do Gunicorn sobem em paralelo.
"""
from datetime import timedelta

from sqlalchemy import text

from app.extensions import db
from app.models import (
    EstoqueProducao,
    Loja,
    PedidoItem,
    PedidoLoja,
    PlanejamentoItem,
    PlanejamentoProducao,
    Receita,
)
from app.utils import hoje

_LOJA_MARCADOR = 'Loja Centro — DEMONSTRAÇÃO'
_LOCK_ID = 734_210_991


def _receita(nome, categoria, lote, lead=1, peso=90, especial=False):
    receita = Receita(
        nome=f'{nome} — DEMO',
        categoria=categoria,
        familia='viennoiserie' if categoria == 'Viennoiserie' else None,
        rendimento_qtd=lote,
        rendimento_unidade='un',
        peso_base=float(lote * peso),
        peso_unitario=float(peso),
        dias_producao=lead,
        lote_producao=lote,
        capacidade_amassadeira_g=50_000,
        sugerir_pedido_loja=True,
        fornada_especial=especial,
    )
    db.session.add(receita)
    return receita


def seed_preview_data():
    """Cria uma semana representativa, sem qualquer dado pessoal real."""
    if db.engine.dialect.name == 'postgresql':
        db.session.execute(
            text('SELECT pg_advisory_xact_lock(:lock_id)'),
            {'lock_id': _LOCK_ID},
        )

    if Loja.query.filter_by(nome=_LOJA_MARCADOR).first():
        db.session.rollback()
        return False

    centro = Loja(nome=_LOJA_MARCADOR, ativa=True)
    bairro = Loja(nome='Loja Bairro — DEMONSTRAÇÃO', ativa=True)
    db.session.add_all([centro, bairro])

    receitas = [
        _receita('Croissant tradicional', 'Viennoiserie', 50, lead=1),
        _receita('Pain au chocolat', 'Viennoiserie', 40, lead=1),
        _receita('Croissant almond', 'Viennoiserie', 30, lead=1),
        _receita('Cinnamon roll', 'Viennoiserie', 24, lead=1, peso=110),
        _receita('Brioche', 'Viennoiserie', 20, lead=1, peso=120),
        _receita('Sourdough tradicional', 'Pães', 12, lead=2, peso=650),
        _receita('Sourdough integral', 'Pães', 12, lead=2, peso=650),
        _receita('Focaccia especial', 'Fornada especial', 8, lead=0,
                 peso=180, especial=True),
    ]
    db.session.flush()

    estoques = [18, 12, 0, 8, 6, 4, 2, 0]
    for receita, quantidade in zip(receitas, estoques):
        db.session.add(EstoqueProducao(
            receita_id=receita.id, quantidade=quantidade))

    hoje_d = hoje()
    # Historico artificial: da contexto ao motor de previsao e ao detalhamento
    # por loja. Quantidades variam por semana, receita e unidade.
    for semana in range(1, 5):
        for dia_offset in (1, 3, 5):
            entrega = hoje_d - timedelta(days=7 * semana - dia_offset)
            for loja_idx, loja in enumerate((centro, bairro)):
                pedido = PedidoLoja(
                    loja=loja, data_pedido=entrega - timedelta(days=1),
                    data_entrega=entrega, status='entregue',
                    observacao='Dados artificiais de demonstração')
                db.session.add(pedido)
                db.session.flush()
                for rec_idx, receita in enumerate(receitas[:6]):
                    quantidade = 8 + rec_idx * 3 + loja_idx * 4 + semana
                    db.session.add(PedidoItem(
                        pedido_id=pedido.id, receita_id=receita.id,
                        quantidade=quantidade))

    # Pedidos firmes dos proximos dias. O sourdough de amanha, com lead de
    # dois dias e estoque curto, cria uma excecao util para avaliar a UX.
    for dia_offset in range(1, 7):
        entrega = hoje_d + timedelta(days=dia_offset)
        for loja_idx, loja in enumerate((centro, bairro)):
            pedido = PedidoLoja(
                loja=loja, data_pedido=hoje_d, data_entrega=entrega,
                status='confirmado',
                observacao='Pedido artificial do ambiente de demonstração')
            db.session.add(pedido)
            db.session.flush()
            limite = 8 if dia_offset in (1, 5, 6) else 7
            for rec_idx, receita in enumerate(receitas[:limite]):
                quantidade = 10 + rec_idx * 4 + loja_idx * 5 + dia_offset * 2
                db.session.add(PedidoItem(
                    pedido_id=pedido.id, receita_id=receita.id,
                    quantidade=quantidade))

    # Uma ordem vencida e uma ordem de hoje simulam o fluxo de confirmacao.
    plano_vencido = PlanejamentoProducao(
        data=hoje_d - timedelta(days=1), nome='Produção demo — ontem',
        status='aprovado', origem='cronograma', enviado_ao_padeiro=True)
    plano_hoje = PlanejamentoProducao(
        data=hoje_d, nome='Produção demo — hoje', status='aprovado',
        origem='cronograma', enviado_ao_padeiro=True)
    db.session.add_all([plano_vencido, plano_hoje])
    db.session.flush()
    db.session.add_all([
        PlanejamentoItem(
            planejamento_id=plano_vencido.id, receita_id=receitas[0].id,
            qtd_alvo=50, produzido_qtd=32),
        PlanejamentoItem(
            planejamento_id=plano_hoje.id, receita_id=receitas[1].id,
            qtd_alvo=40, produzido_qtd=0),
    ])

    db.session.commit()
    return True
