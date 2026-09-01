"""Regras semanais e excecoes da disponibilidade da loja online."""
from datetime import date


def _receita_publicada(db, nome='Focaccia Gorgonzola'):
    from app.models import Receita

    receita = Receita(
        nome=nome, categoria='Fornadas Especiais', preco_site=32,
        rendimento_qtd=1, rendimento_unidade='un', peso_base=500)
    db.session.add(receita)
    db.session.commit()
    return receita


def _owner(app):
    from app.extensions import db
    from app.models import Usuario

    usuario = Usuario(
        nome='Dono', login='dono-disponibilidade', papel='admin',
        is_owner=True)
    usuario.set_senha('senha-segura')
    db.session.add(usuario)
    db.session.commit()
    client = app.test_client()
    with client.session_transaction() as session:
        session['_user_id'] = str(usuario.id)
        session['_fresh'] = True
    return client


def test_regra_semanal_libera_focaccia_so_no_fim_de_semana(app):
    from app.extensions import db
    from app.services import loja_plano_dia

    receita = _receita_publicada(db)
    loja_plano_dia.salvar_regra_semanal(
        'receita', receita.id, [5, 6], None)

    sexta = date(2026, 9, 4)
    sabado = date(2026, 9, 5)
    domingo = date(2026, 9, 6)
    assert loja_plano_dia.saldo('receita', receita.id, sexta) == 0
    assert loja_plano_dia.saldo('receita', receita.id, sabado) == 99999
    assert loja_plano_dia.saldo('receita', receita.id, domingo) == 99999


def test_limite_semanal_desconta_o_que_ja_foi_reservado(app):
    from app.extensions import db
    from app.services import loja_plano_dia

    receita = _receita_publicada(db)
    sabado = date(2026, 9, 5)
    loja_plano_dia.salvar_regra_semanal(
        'receita', receita.id, [5], 20)

    assert loja_plano_dia.reservar(
        'receita', receita.id, sabado, 3) is True
    assert loja_plano_dia.saldo('receita', receita.id, sabado) == 17
    assert loja_plano_dia.reservar(
        'receita', receita.id, sabado, 18) is False


def test_excecao_pontual_prevalece_e_depois_regra_volta(app):
    from app.extensions import db
    from app.services import loja_plano_dia

    receita = _receita_publicada(db)
    sabado = date(2026, 9, 5)
    domingo = date(2026, 9, 6)
    loja_plano_dia.salvar_regra_semanal(
        'receita', receita.id, [5, 6], None)
    loja_plano_dia.salvar_excecao(
        'receita', receita.id, sabado, 0)

    assert loja_plano_dia.saldo('receita', receita.id, sabado) == 0
    assert loja_plano_dia.saldo('receita', receita.id, domingo) == 99999

    loja_plano_dia.remover_excecao('receita', receita.id, sabado)
    assert loja_plano_dia.saldo('receita', receita.id, sabado) == 99999


def test_item_sem_regra_preserva_plano_diario_existente(app):
    from app.extensions import db
    from app.services import loja_plano_dia

    receita = _receita_publicada(db)
    dia = date(2026, 9, 2)
    loja_plano_dia.definir('receita', receita.id, dia, 7)

    assert loja_plano_dia.saldo('receita', receita.id, dia) == 7


def test_herdar_remove_limite_diario_antigo_sem_apagar_reserva(app):
    from app.extensions import db
    from app.models import EstoqueSitePlano
    from app.services import loja_plano_dia

    receita = _receita_publicada(db)
    dia = date(2026, 9, 2)
    db.session.add(EstoqueSitePlano(
        kind='receita', item_id=receita.id, data=dia,
        qtd_planejada=4, qtd_reservada=2))
    db.session.commit()

    loja_plano_dia.remover_excecao('receita', receita.id, dia)
    row = EstoqueSitePlano.query.filter_by(
        kind='receita', item_id=receita.id, data=dia).one()
    assert row.qtd_reservada == 2
    assert loja_plano_dia.saldo('receita', receita.id, dia) == 99999


def test_tela_nova_salva_regra_e_excecao_sem_expor_99999(app):
    from app.extensions import db
    from app.models import EstoqueSiteExcecao, EstoqueSiteRegraSemanal

    receita = _receita_publicada(db)
    client = _owner(app)

    resposta = client.get('/admin/loja-online/plano-do-dia')
    assert resposta.status_code == 200
    html = resposta.get_data(as_text=True)
    assert 'Quando cada produto aparece' in html
    assert 'Regra semanal' in html
    assert 'Exceções por data' in html
    assert 'Buscar produto' in html
    assert 'Aplicar 99999' not in html

    resposta = client.post(
        '/admin/loja-online/plano-do-dia/regra-semanal',
        data={
            'kind': 'receita', 'item_id': receita.id,
            'regra': 'personalizada', 'dias': ['5', '6'],
            'tipo_limite': 'sem_limite',
        })
    assert resposta.status_code == 302
    regra = EstoqueSiteRegraSemanal.query.filter_by(
        kind='receita', item_id=receita.id).one()
    assert regra.dias_mask == (1 << 5) | (1 << 6)
    assert regra.qtd_limite is None

    resposta = client.post(
        '/admin/loja-online/plano-do-dia/excecao',
        data={
            'kind': 'receita', 'item_id': receita.id,
            'data': '2026-09-05', 'tipo_excecao': 'limite',
            'qtd_limite': '12',
        })
    assert resposta.status_code == 302
    excecao = EstoqueSiteExcecao.query.filter_by(
        kind='receita', item_id=receita.id,
        data=date(2026, 9, 5)).one()
    assert excecao.qtd_limite == 12
