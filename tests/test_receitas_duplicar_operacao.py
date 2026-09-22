"""Copiar uma ficha mantém sua operação, sem herdar publicação ou histórico."""
from app.extensions import db
from app.models import Receita, ReceitaEtapa, ReceitaIngrediente
from app.utils import agora


def _login(client, user):
    with client.session_transaction() as sess:
        sess['_user_id'] = str(user.id)
        sess['_fresh'] = True


def test_copia_preserva_prazo_lotes_calendario_e_etapas(app, admin_user):
    retorno = Receita(nome='Retorno fresco', rendimento_qtd=1,
                      rendimento_unidade='un', peso_base=100)
    sub = Receita(nome='Massa base', rendimento_qtd=1,
                  rendimento_unidade='un', peso_base=100)
    db.session.add_all([retorno, sub])
    db.session.flush()
    original = Receita(
        nome='Especial de sábado', categoria='Especiais', familia='viennoiserie',
        rendimento_qtd=8, rendimento_unidade='un', peso_base=1200,
        peso_unitario=150, estado_padrao='assado', dias_producao=2,
        capacidade_amassadeira_g=28000, sugerir_pedido_loja=False,
        lote_pedido=6, minimo_pedido=12, estoque_minimo_industria=24,
        lote_producao=8, producao_max_dia=40, fornada_especial=True,
        reaproveitavel=True, sub_na_amassadeira=True, estoque_nao_abate=True,
        sob_encomenda=True, antecedencia_max_dias=0, cobra_sobra_diaria=True,
        retorno_receita_id=retorno.id, preco_interno=4.5,
        observacao='Assar antes da entrega',
    )
    db.session.add(original)
    db.session.flush()
    ingrediente = ReceitaIngrediente(
        receita_id=original.id, tipo='receita', ingrediente_nome=sub.nome,
        sub_receita_id=sub.id, porcentagem=0.5, eh_base=False, nota='gelada',
    )
    etapa = ReceitaEtapa(
        receita_id=original.id, ordem=2, nome='Fermentar', duracao_min=1440,
        equipamento='camara_fria', ativa=False, descricao='Cobrir a massa',
    )
    db.session.add_all([ingrediente, etapa])
    db.session.commit()

    client = app.test_client()
    _login(client, admin_user)
    response = client.post(f'/receitas/{original.id}/duplicar')
    assert response.status_code == 302
    copia = Receita.query.filter_by(nome='Cópia de Especial de sábado').one()
    assert (copia.dias_producao, copia.antecedencia_max_dias) == (2, 0)
    assert (copia.lote_pedido, copia.minimo_pedido, copia.lote_producao) == (6, 12, 8)
    assert (copia.estoque_minimo_industria, copia.producao_max_dia) == (24, 40)
    assert copia.fornada_especial is True
    assert copia.sugerir_pedido_loja is False
    assert copia.estoque_nao_abate is True
    assert copia.sob_encomenda is True
    assert copia.reaproveitavel is True
    assert copia.sub_na_amassadeira is True
    assert copia.cobra_sobra_diaria is True
    assert copia.familia == 'viennoiserie'
    assert copia.estado_padrao == 'assado'
    assert copia.capacidade_amassadeira_g == 28000
    assert copia.retorno_receita_id == retorno.id
    assert copia.preco_interno == 4.5
    assert copia.observacao == 'Assar antes da entrega'
    assert len(copia.ingredientes) == len(copia.etapas) == 1
    ing_copia = copia.ingredientes[0]
    assert ing_copia.id != ingrediente.id
    assert ing_copia.sub_receita_id == sub.id
    assert ing_copia.porcentagem == 0.5
    assert ing_copia.nota == 'gelada'
    etapa_copia = copia.etapas[0]
    assert etapa_copia.id != etapa.id
    assert (etapa_copia.ordem, etapa_copia.duracao_min) == (2, 1440)
    assert etapa_copia.equipamento == 'camara_fria'
    assert etapa_copia.ativa is False
    assert etapa_copia.descricao == 'Cobrir a massa'
    etapa_copia.duracao_min = 720
    db.session.commit()
    assert etapa.duracao_min == 1440


def test_copia_nao_publica_nem_compartilha_arquivo_ou_arquivamento(app, admin_user):
    from app.services.loja_catalogo import por_id_publicado

    original = Receita(
        nome='Produto publicado', rendimento_qtd=1, rendimento_unidade='un',
        peso_base=100, preco_site=18, site_ativo=True, ordem_site=3,
        imagem_storage_path='/original.jpg', imagem_dropbox_url='https://example.test/original',
        imagem_url='https://example.test/fallback', descricao_seo='SEO do original',
        arquivada_em=agora(), arquivada_por_id=admin_user.id,
    )
    db.session.add(original)
    db.session.commit()
    client = app.test_client()
    _login(client, admin_user)
    assert client.post(f'/receitas/{original.id}/duplicar').status_code == 302
    copia = Receita.query.filter_by(nome='Cópia de Produto publicado').one()
    assert copia.id != original.id
    assert copia.preco_site == 18
    assert copia.site_ativo is False
    assert por_id_publicado('receita', copia.id) is None
    assert copia.ordem_site is None
    assert copia.descricao_seo is None
    assert copia.imagem_storage_path is None
    assert copia.imagem_dropbox_url is None
    assert copia.imagem_url is None
    assert copia.arquivada_em is None
    assert copia.arquivada_por_id is None
