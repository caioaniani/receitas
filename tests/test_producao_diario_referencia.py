"""Referências de planilha preservam procedência sem virar lotes inventados."""

import json
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Barrier

import pytest
from sqlalchemy import event

from app.extensions import db
from app.models import ProducaoDiarioEtapa, ProducaoDiarioLote, ProducaoDiarioReferencia
from app.services import producao_diario_referencia as referencias


@pytest.fixture
def manifesto(tmp_path, monkeypatch):
    dados = dict(
        arquivo='producao_sourdough_padrao.xlsx', sha256='a' * 64, aba='Registros',
        registros=[dict(
            id_fonte=i, linha=i + 1, produto='  Tradicional  ' if i == 1 else f'Produto {i}',
            valores={'farinha_kg': i * 2, 'temperatura_agua': 12.5, 'tempo_frio_min': None},
            classificacao={'farinha_kg': 'transcrito', 'temperatura_agua': 'informado',
                           'tempo_frio_min': 'a_confirmar'},
            fonte_original='Caderno original', observacao_original='Sem data de produção',
        ) for i in range(1, 11)],
    )
    arquivo = tmp_path / 'manifesto.json'
    arquivo.write_text(json.dumps(dados, ensure_ascii=False), encoding='utf-8')
    monkeypatch.setattr(referencias, 'MANIFESTO_PATH', arquivo)
    return dados, arquivo


def test_importa_dez_linhas_com_procedencia_completa_e_produto_literal(admin_user, manifesto):
    dados, _ = manifesto
    assert referencias.importar_referencias(admin_user.id) == 10
    registros = referencias.listar_referencias()
    assert len(registros) == 10
    for esperado, registro in zip(dados['registros'], registros):
        assert registro.produto == esperado['produto']
        assert registro.dados == esperado
        assert registro.fonte_arquivo == dados['arquivo']
        assert registro.fonte_aba == 'Registros'
        assert registro.fonte_linha == esperado['linha']
        assert registro.chave == f"{dados['sha256']}:Registros:{esperado['linha']}"
        assert registro.importado_por_id == admin_user.id
        assert registro.importado_em is not None
        assert not hasattr(registro, 'data_producao')
        assert not hasattr(registro, 'planejamento_id')


def test_reimportacao_nao_sobrescreve_dados_nem_autoria(admin_user, owner_user, manifesto):
    dados, arquivo = manifesto
    referencias.importar_referencias(admin_user.id)
    primeira = referencias.listar_referencias()[0]
    snapshot = deepcopy(primeira.dados)
    quando = primeira.importado_em
    dados['registros'][0]['valores']['farinha_kg'] = 999
    arquivo.write_text(json.dumps(dados), encoding='utf-8')
    assert referencias.importar_referencias(owner_user.id) == 0
    db.session.refresh(primeira)
    assert primeira.dados == snapshot
    assert primeira.importado_por_id == admin_user.id
    assert primeira.importado_em == quando


def test_planilhas_distintas_nao_compartilham_identidade_da_linha(admin_user, manifesto):
    dados, arquivo = manifesto
    referencias.importar_referencias(admin_user.id)
    dados['sha256'] = 'b' * 64
    arquivo.write_text(json.dumps(dados), encoding='utf-8')
    assert referencias.importar_referencias(admin_user.id) == 10
    assert ProducaoDiarioReferencia.query.count() == 20


def test_abas_distintas_e_contagem_pendente_por_identidade(admin_user, manifesto):
    dados, arquivo = manifesto
    assert referencias.contar_pendentes() == 10
    referencias.importar_referencias(admin_user.id)
    assert referencias.contar_pendentes() == 0
    dados['aba'] = 'Outro caderno'
    arquivo.write_text(json.dumps(dados), encoding='utf-8')
    assert referencias.contar_pendentes() == 10
    assert referencias.importar_referencias(admin_user.id) == 10
    assert referencias.contar_pendentes() == 0


def test_manifesto_empacotado_valido_preserva_notas_celulas_e_formulas(admin_user):
    dados = json.loads(referencias.MANIFESTO_PATH.read_text(encoding='utf-8'))
    assert referencias.contar_pendentes() == len(dados['registros']) == 10
    assert referencias.importar_referencias(admin_user.id) == 10
    primeiro = referencias.listar_referencias()[0]
    assert primeiro.dados == dados['registros'][0]
    assert primeiro.dados['notas']
    assert primeiro.dados['celulas']
    assert primeiro.dados['formulas_originais']


@pytest.mark.parametrize('alterar', [
    lambda dados: dados.update(sha256='semhash'),
    lambda dados: dados.update(registros='linhas'),
    lambda dados: dados['registros'][-1].update(linha=0),
    lambda dados: dados['registros'][-1].update(linha=True),
    lambda dados: dados['registros'][-1].update(linha=2),
    lambda dados: dados['registros'][-1].update(produto=''),
    lambda dados: dados['registros'][-1].update(classificacao={'farinha_kg': 'medido'}),
    lambda dados: dados['registros'][-1]['classificacao'].pop('farinha_kg'),
])
def test_manifesto_invalido_e_atomico_sem_importacao_parcial(admin_user, manifesto, alterar):
    dados, arquivo = manifesto
    alterar(dados)
    arquivo.write_text(json.dumps(dados), encoding='utf-8')
    with pytest.raises(referencias.ReferenciaError):
        referencias.importar_referencias(admin_user.id)
    assert ProducaoDiarioReferencia.query.count() == 0


def test_manifesto_nao_finito_ou_ilegivel_rejeitado(admin_user, manifesto):
    dados, arquivo = manifesto
    dados['registros'][-1]['valores']['farinha_kg'] = float('nan')
    arquivo.write_text(json.dumps(dados), encoding='utf-8')
    with pytest.raises(referencias.ReferenciaError):
        referencias.importar_referencias(admin_user.id)
    arquivo.unlink()
    with pytest.raises(referencias.ReferenciaError):
        referencias.importar_referencias(admin_user.id)
    assert ProducaoDiarioReferencia.query.count() == 0


def test_usuario_de_importacao_obrigatorio(admin_user, manifesto):
    for usuario_id in (None, True, 999999):
        with pytest.raises(referencias.ReferenciaError):
            referencias.importar_referencias(usuario_id)
    assert ProducaoDiarioReferencia.query.count() == 0


def test_importar_nao_escreve_lotes_etapas_fichas_ou_estoque(admin_user, manifesto):
    writes = []

    def observar(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().split()[0].upper() in ('INSERT', 'UPDATE', 'DELETE'):
            writes.append(statement)

    event.listen(db.engine, 'before_cursor_execute', observar)
    try:
        referencias.importar_referencias(admin_user.id)
    finally:
        event.remove(db.engine, 'before_cursor_execute', observar)
    assert writes and all('INSERT INTO producao_diario_referencia ' in sql for sql in writes)
    assert ProducaoDiarioLote.query.count() == 0
    assert ProducaoDiarioEtapa.query.count() == 0


def test_dois_workers_importam_dez_registros_uma_so_vez(app, admin_user, manifesto):
    usuario_id = admin_user.id
    barreira = Barrier(2)

    def importar():
        with app.app_context():
            barreira.wait(timeout=10)
            try:
                return referencias.importar_referencias(usuario_id)
            finally:
                db.session.remove()

    with ThreadPoolExecutor(max_workers=2) as pool:
        resultados = list(pool.map(lambda _: importar(), range(2)))
    assert sorted(resultados) == [0, 10]
    assert ProducaoDiarioReferencia.query.count() == 10
