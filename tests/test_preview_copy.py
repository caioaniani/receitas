from datetime import date, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.extensions import db
from app.models import (
    EstoqueSitePlano,
    Loja,
    PedidoItem,
    PedidoLoja,
    Receita,
)
from app.preview_copy import _sanitizar, copy_preview_data


def test_sanitizador_remove_dados_privados_e_vinculos_pessoais():
    row = {
        'id': 1,
        'nome': 'Loja real',
        'telefone': '11999999999',
        'cnpj': '00111222000100',
        'endereco_logradouro': 'Rua privada',
        'criado_por_id': 42,
    }

    sanitized = _sanitizar('loja', row)

    assert sanitized['nome'] == 'Loja real'
    assert sanitized['telefone'] is None
    assert sanitized['cnpj'] is None
    assert sanitized['endereco_logradouro'] is None
    assert sanitized['criado_por_id'] is None


def test_copia_operacional_inclui_estoque_site_e_e_idempotente(app, tmp_path):
    source_path = tmp_path / 'producao-falsa.db'
    source = create_engine(f'sqlite:///{source_path}')
    db.metadata.create_all(source)

    with Session(source) as session:
        loja = Loja(
            id=81,
            nome='Loja Operacional',
            telefone='11999999999',
            cnpj='00111222000100',
            endereco_logradouro='Rua privada',
            ativa=True,
        )
        receita = Receita(
            id=91,
            nome='Croissant real',
            categoria='Viennoiserie',
            rendimento_qtd=50,
            rendimento_unidade='un',
            peso_base=4500,
            dias_producao=1,
            capacidade_amassadeira_g=50000,
        )
        pedido = PedidoLoja(
            id=101,
            loja=loja,
            data_pedido=date.today(),
            data_entrega=date.today() + timedelta(days=1),
            status='confirmado',
            observacao='Nome de pessoa que nao deve sair',
        )
        session.add_all([
            loja,
            receita,
            pedido,
            PedidoItem(
                id=111,
                pedido=pedido,
                receita=receita,
                quantidade=24,
                observacao='Telefone 11999999999',
            ),
            EstoqueSitePlano(
                id=121,
                kind='receita',
                item_id=91,
                data=date.today() + timedelta(days=1),
                qtd_planejada=30,
                qtd_reservada=6,
            ),
        ])
        session.commit()

    with app.app_context():
        resumo = copy_preview_data(
            f'sqlite:///{source_path}', 'snapshot-1', dias_historico=180)

        assert resumo['loja'] == 1
        assert resumo['estoque_site_plano'] == 1
        imported = db.session.get(Loja, 81)
        assert imported.nome == 'Loja Operacional'
        assert imported.telefone is None
        assert imported.cnpj is None
        assert imported.endereco_logradouro is None
        assert db.session.get(PedidoLoja, 101).observacao == (
            'Dados sanitizados para homologacao')
        assert db.session.get(PedidoItem, 111).observacao == (
            'Dados sanitizados para homologacao')
        site = db.session.get(EstoqueSitePlano, 121)
        assert site.qtd_planejada == 30
        assert site.qtd_reservada == 6

        assert copy_preview_data(
            f'sqlite:///{source_path}', 'snapshot-1', dias_historico=180
        ) is None

    source.dispose()

