"""Equipe por loja: adicionar, importar e remover pessoa só mexem no vínculo."""
import json

import pytest
from flask import g
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models import AuditLog, Cargo, Funcionario, Loja, Usuario
from app.models.rh import funcionario_loja
from app.services import rh_equipe_lojas_acoes as acoes
from app.services.acesso_gestao_rh import ENDPOINTS_RH
from app.services.rh_equipe_lojas import carregar_lojas
from app.services.treino_lideranca import unidades_principais


def _cliente(app, usuario=None):
    g.pop('_login_user', None)
    cliente = app.test_client()
    if usuario:
        with cliente.session_transaction() as sessao:
            sessao['_user_id'] = str(usuario.id)
            sessao['_fresh'] = True
    return cliente


def _loja(nome, *, ativa=True):
    loja = Loja(nome=nome, ativa=ativa)
    db.session.add(loja)
    db.session.flush()
    return loja


def _pessoa(nome, *, lojas=(), principal=None, periodo='Manhã', lider=None,
            ativo=True, usuario=None, **campos):
    numero = Funcionario.query.count() + 1
    pessoa = Funcionario(nome=nome, cpf=f'840{numero:08d}', ativo=ativo,
                         lojas=list(lojas), periodo=periodo,
                         lider_id=lider.id if lider else None,
                         usuario=usuario, **campos)
    db.session.add(pessoa)
    db.session.flush()
    if principal is not None:
        db.session.execute(funcionario_loja.update().where(
            funcionario_loja.c.funcionario_id == pessoa.id,
            funcionario_loja.c.loja_id == principal.id,
        ).values(loja_principal=True))
    return pessoa


def _vinculos(pessoa):
    linhas = db.session.execute(
        funcionario_loja.select().where(
            funcionario_loja.c.funcionario_id == pessoa.id)).all()
    return {linha.loja_id: bool(linha.loja_principal) for linha in linhas}


def _auditoria(pessoa):
    return (AuditLog.query.filter_by(tabela='funcionario_loja', registro_id=pessoa.id)
            .order_by(AuditLog.id).all())


@pytest.fixture
def chefe(app):
    usuario = Usuario(nome='Chefia RH lojas', login='chefia-rh-lojas', papel='gestao_rh')
    usuario.set_senha('Senha-teste-123')
    db.session.add(usuario)
    db.session.commit()
    return usuario


# ── Serviço: adicionar ────────────────────────────────────────────────────

def test_adicionar_sem_unidade_vira_principal_e_audita(app, owner_user):
    centro = _loja('Centro')
    ana = _pessoa('Ana', periodo=None)
    db.session.commit()

    resultado = acoes.adicionar_pessoa(centro, ana.id, periodo='Tarde',
                                       actor_id=owner_user.id)
    db.session.commit()

    assert resultado['principal'] is True
    assert resultado['principal_anterior'] is None
    assert resultado['periodo_mudou'] is True
    assert _vinculos(ana) == {centro.id: True}
    assert unidades_principais([ana]) == {ana.id: centro.id}
    assert ana.periodo == 'Tarde'
    (log,) = _auditoria(ana)
    assert (log.acao, log.usuario_id, log.antes) == ('insert', owner_user.id, None)
    depois = json.loads(log.depois)
    assert depois['loja'] == 'Centro' and depois['loja_principal'] is True
    assert depois['funcionario'] == 'Ana' and depois['periodo'] == 'Tarde'


def test_adicionar_segunda_loja_fica_adicional_e_fixa_a_principal_antiga(app):
    centro, jardim = _loja('Centro'), _loja('Jardim')
    bia = _pessoa('Bia', lojas=[centro])  # loja única sem marca explícita
    db.session.commit()
    assert _vinculos(bia) == {centro.id: False}

    resultado = acoes.adicionar_pessoa(jardim, bia.id)
    db.session.commit()

    assert resultado['principal'] is False
    assert resultado['principal_anterior'] == 'Centro'
    # Sem a marca explícita, duas lojas virariam "sem principal definida".
    assert _vinculos(bia) == {centro.id: True, jardim.id: False}
    assert unidades_principais([bia]) == {bia.id: centro.id}
    card = next(c for c in carregar_lojas()['lojas'] if c['id'] == jardim.id)
    assert [p['id'] for p in card['outros_vinculos']] == [bia.id]
    assert card['total'] == 0


def test_adicionar_com_principal_marcado_troca_a_principal_e_mantem_a_antiga(app):
    centro, jardim = _loja('Centro'), _loja('Jardim')
    bia = _pessoa('Bia', lojas=[centro], principal=centro)
    db.session.commit()

    resultado = acoes.adicionar_pessoa(jardim, bia.id, principal=True)
    db.session.commit()

    assert resultado['principal'] is True
    assert _vinculos(bia) == {centro.id: False, jardim.id: True}
    assert unidades_principais([bia]) == {bia.id: jardim.id}


def test_adicionar_ja_vinculada_nao_grava_nada(app):
    centro = _loja('Centro')
    bia = _pessoa('Bia', lojas=[centro])
    db.session.commit()
    with pytest.raises(acoes.EquipeLojaError, match='já está vinculad'):
        acoes.adicionar_pessoa(centro, bia.id)
    db.session.rollback()
    assert _vinculos(bia) == {centro.id: False}
    assert _auditoria(bia) == []


@pytest.mark.parametrize('caso', ['inativa', 'direcao', 'loja_inativa', 'periodo',
                                  'inexistente', 'vazio'])
def test_adicionar_recusa_casos_invalidos_sem_gravar(app, owner_user, caso):
    centro = _loja('Centro', ativa=caso != 'loja_inativa')
    usuario = owner_user if caso == 'direcao' else None
    pessoa = _pessoa('Pessoa', ativo=caso != 'inativa', usuario=usuario)
    db.session.commit()
    funcionario_id = {'inexistente': 999999, 'vazio': None}.get(caso, pessoa.id)
    with pytest.raises(acoes.EquipeLojaError):
        acoes.adicionar_pessoa(centro, funcionario_id,
                               periodo='Noite' if caso == 'periodo' else None)
    db.session.rollback()
    assert _vinculos(pessoa) == {}
    assert pessoa.periodo == 'Manhã'
    assert AuditLog.query.filter_by(tabela='funcionario_loja').count() == 0


# ── Serviço: importar ─────────────────────────────────────────────────────

def test_importar_transfere_a_principal_e_sai_da_origem(app, owner_user):
    centro, jardim, anexo = _loja('Centro'), _loja('Jardim'), _loja('Anexo')
    lider = _pessoa('Líder', lojas=[centro], principal=centro)
    cargo = Cargo(nome='Atendente', salario_base=2500)
    db.session.add(cargo)
    bia = _pessoa('Bia', lojas=[centro, anexo], principal=centro, lider=lider,
                  cargo=cargo, salario_base=2500)
    db.session.commit()

    resultado = acoes.importar_pessoa(jardim, bia.id, actor_id=owner_user.id)
    db.session.commit()

    assert resultado['origem'] == 'Centro' and resultado['origem_mantida'] is False
    assert _vinculos(bia) == {jardim.id: True, anexo.id: False}  # secundário preservado
    assert unidades_principais([bia]) == {bia.id: jardim.id}
    assert (bia.lider_id, bia.cargo_id, bia.salario_base, bia.periodo, bia.ativo) == (
        lider.id, cargo.id, 2500, 'Manhã', True)
    logs = _auditoria(bia)
    assert [log.acao for log in logs] == ['delete', 'insert']
    assert json.loads(logs[0].antes)['loja'] == 'Centro'
    assert 'transferência para Jardim' in logs[0].antes
    assert json.loads(logs[1].depois)['origem'] == 'Centro'


def test_importar_mantendo_origem_deixa_vinculo_adicional(app):
    centro, jardim = _loja('Centro'), _loja('Jardim')
    bia = _pessoa('Bia', lojas=[centro])
    db.session.commit()

    resultado = acoes.importar_pessoa(jardim, bia.id, manter_origem=True, periodo='Tarde')
    db.session.commit()

    assert resultado['origem_mantida'] is True and resultado['periodo_mudou'] is True
    assert _vinculos(bia) == {centro.id: False, jardim.id: True}
    assert bia.periodo == 'Tarde'
    assert [log.acao for log in _auditoria(bia)] == ['insert']


def test_importar_quem_ja_era_vinculo_adicional_promove_e_audita_update(app):
    centro, jardim = _loja('Centro'), _loja('Jardim')
    bia = _pessoa('Bia', lojas=[centro, jardim], principal=centro)
    db.session.commit()

    acoes.importar_pessoa(jardim, bia.id)
    db.session.commit()

    assert _vinculos(bia) == {jardim.id: True}
    assert [log.acao for log in _auditoria(bia)] == ['delete', 'update']


def test_importar_recusa_sem_principal_ou_ja_principal_aqui(app):
    centro, jardim = _loja('Centro'), _loja('Jardim')
    sem = _pessoa('Sem unidade')
    aqui = _pessoa('Já aqui', lojas=[jardim])
    db.session.commit()
    with pytest.raises(acoes.EquipeLojaError, match='Adicionar pessoa'):
        acoes.importar_pessoa(jardim, sem.id)
    db.session.rollback()
    with pytest.raises(acoes.EquipeLojaError, match='já é a unidade principal'):
        acoes.importar_pessoa(jardim, aqui.id)
    db.session.rollback()
    assert _vinculos(sem) == {} and _vinculos(aqui) == {jardim.id: False}
    assert centro.ativa


# ── Serviço: remover ──────────────────────────────────────────────────────

def test_remover_principal_com_uma_loja_restante_passa_a_principal(app, owner_user):
    centro, jardim = _loja('Centro'), _loja('Jardim')
    lider = _pessoa('Líder', lojas=[centro], principal=centro)
    bia = _pessoa('Bia', lojas=[centro, jardim], principal=centro, lider=lider)
    db.session.commit()

    resultado = acoes.remover_pessoa(centro, bia.id, actor_id=owner_user.id)
    db.session.commit()

    assert resultado['era_principal'] is True
    assert resultado['restantes'] == 1 and resultado['nova_principal'] == 'Jardim'
    assert _vinculos(bia) == {jardim.id: False}
    assert unidades_principais([bia]) == {bia.id: jardim.id}
    assert bia.ativo is True and bia.lider_id == lider.id and bia.periodo == 'Manhã'
    (log,) = _auditoria(bia)
    assert log.acao == 'delete' and json.loads(log.antes)['loja_principal'] is True
    assert log.depois is None


def test_remover_ultima_loja_deixa_sem_unidade_sem_desligar(app):
    centro = _loja('Centro')
    bia = _pessoa('Bia', lojas=[centro])
    db.session.commit()

    resultado = acoes.remover_pessoa(centro, bia.id)
    db.session.commit()

    assert resultado == {'pessoa': bia, 'era_principal': True, 'restantes': 0,
                         'nova_principal': None}
    assert _vinculos(bia) == {}
    assert bia.ativo is True and Funcionario.query.get(bia.id) is bia
    visao = carregar_lojas()
    assert [p['id'] for p in visao['sem_unidade']] == [bia.id]


def test_remover_vinculo_adicional_preserva_a_principal(app):
    centro, jardim = _loja('Centro'), _loja('Jardim')
    bia = _pessoa('Bia', lojas=[centro, jardim], principal=centro)
    db.session.commit()

    resultado = acoes.remover_pessoa(jardim, bia.id)
    db.session.commit()

    assert resultado['era_principal'] is False and resultado['nova_principal'] is None
    assert _vinculos(bia) == {centro.id: True}


def test_remover_quem_nao_esta_na_loja_recusa(app):
    centro, jardim = _loja('Centro'), _loja('Jardim')
    bia = _pessoa('Bia', lojas=[centro])
    db.session.commit()
    with pytest.raises(acoes.EquipeLojaError, match='não está vinculad'):
        acoes.remover_pessoa(jardim, bia.id)
    db.session.rollback()
    assert _vinculos(bia) == {centro.id: False}


# ── Candidatos dos formulários ────────────────────────────────────────────

def test_candidatos_separam_sem_unidade_outras_lojas_e_excluem_direcao(app, owner_user):
    centro, jardim = _loja('Centro'), _loja('Jardim')
    fechada = _loja('Fechada', ativa=False)
    _pessoa('Dono', lojas=[centro], usuario=owner_user)
    sem = _pessoa('Sem unidade')
    da_fechada = _pessoa('Da fechada', lojas=[fechada])
    aqui = _pessoa('Aqui', lojas=[centro])
    de_la = _pessoa('De lá', lojas=[jardim], periodo='Tarde')
    inativa = _pessoa('Inativa', lojas=[jardim], ativo=False)
    db.session.commit()

    cand = acoes.candidatos_por_loja([centro.id])[centro.id]

    def ids(grupos):
        return [(g['titulo'], [p['id'] for p in g['pessoas']]) for g in grupos]
    assert ids(cand['adicionar']) == [
        ('Sem unidade principal', [da_fechada.id, sem.id]), ('Jardim', [de_la.id])]
    assert ids(cand['importar']) == [('Jardim', [de_la.id])]
    todos = [p['id'] for g in cand['adicionar'] + cand['importar'] for p in g['pessoas']]
    assert aqui.id not in todos and inativa.id not in todos
    pessoa = cand['importar'][0]['pessoas'][0]
    assert pessoa == {'id': de_la.id, 'nome': 'De lá', 'cargo': 'Sem cargo',
                      'periodo': 'Tarde'}


def test_card_expoe_vinculados_sem_lider_de_outra_unidade(app):
    centro, jardim = _loja('Centro'), _loja('Jardim')
    lider = _pessoa('Líder de fora', lojas=[jardim])
    ana = _pessoa('Ana', lojas=[centro], lider=lider)
    extra = _pessoa('Extra', lojas=[jardim, centro], principal=jardim)
    db.session.commit()
    card = next(c for c in carregar_lojas()['lojas'] if c['id'] == centro.id)
    assert card['vinculados'] == sorted([ana.id, extra.id])
    assert lider.id not in card['vinculados']


# ── Rotas ─────────────────────────────────────────────────────────────────

def _equipe():
    centro, jardim = _loja('Centro'), _loja('Jardim')
    lider = _pessoa('Líder visual', lojas=[centro], principal=centro)
    ana = _pessoa('Ana visual', lojas=[centro], lider=lider)
    bia = _pessoa('Bia visual', lojas=[jardim], periodo='Tarde')
    livre = _pessoa('Livre visual')
    db.session.commit()
    return centro, jardim, lider, ana, bia, livre


def test_tela_mostra_gestos_so_no_contexto_da_loja(app, owner_user):
    centro, jardim, lider, ana, bia, livre = _equipe()
    de_fora = _pessoa('Chefe de fora', lojas=[jardim], principal=jardim)
    ana.lider = de_fora
    db.session.commit()
    html = _cliente(app, owner_user).get(
        '/rh/equipe/lojas', query_string={'loja': centro.id}).get_data(as_text=True)

    assert html.count('Adicionar pessoa') == 1 and html.count('Importar de outra loja') == 1
    assert f'action="/rh/equipe/lojas/{centro.id}/adicionar"' in html
    assert f'action="/rh/equipe/lojas/{centro.id}/importar"' in html
    assert f'href="/rh/funcionarios/novo?loja={centro.id}"' in html
    # Remover: só Ana e o líder local; o chefe de outra unidade é referência.
    assert f'<input type="hidden" name="funcionario_id" value="{ana.id}">' in html
    assert f'<input type="hidden" name="funcionario_id" value="{lider.id}">' in html
    assert f'<input type="hidden" name="funcionario_id" value="{de_fora.id}">' not in html
    assert html.count('Remover da loja') == 2
    assert 'data-confirmar="Remover Ana visual da equipe de Centro?' in html
    assert '<optgroup label="Sem unidade principal"><option value="%d">Livre visual' % livre.id in html
    assert '<optgroup label="Jardim">' in html and 'Bia visual · Sem cargo · Tarde' in html
    assert '<input type="hidden" name="voltar" value="loja">' in html


def test_listas_fora_da_loja_nao_tem_remover(app, owner_user):
    centro, jardim, lider, ana, bia, livre = _equipe()
    _pessoa('Dono', lojas=[centro], usuario=owner_user)
    html = _cliente(app, owner_user).get('/rh/equipe/lojas').get_data(as_text=True)
    assert 'Unidade principal a definir' in html and 'Livre visual' in html
    assert f'name="funcionario_id" value="{livre.id}"' not in html
    assert f'name="funcionario_id" value="{owner_user.funcionario.id}"' not in html
    assert '<input type="hidden" name="voltar" value="todas">' in html


def test_post_adicionar_pelo_dono_volta_para_a_loja_com_aviso(app, owner_user):
    centro, jardim, lider, ana, bia, livre = _equipe()
    cliente = _cliente(app, owner_user)
    resposta = cliente.post(f'/rh/equipe/lojas/{centro.id}/adicionar', data={
        'funcionario_id': livre.id, 'periodo': 'Manhã', 'voltar': 'loja'})
    assert resposta.status_code == 302
    assert resposta.location.endswith(f'/rh/equipe/lojas?loja={centro.id}')
    html = cliente.get(resposta.location).get_data(as_text=True)
    assert 'Livre visual agora tem Centro como unidade principal.' in html
    assert _vinculos(livre) == {centro.id: True}
    assert _auditoria(livre)[0].usuario_id == owner_user.id


def test_post_importar_e_remover_pela_chefia_de_rh(app, chefe):
    centro, jardim, lider, ana, bia, livre = _equipe()
    cliente = _cliente(app, chefe)
    resposta = cliente.post(f'/rh/equipe/lojas/{centro.id}/importar', data={
        'funcionario_id': bia.id, 'voltar': 'todas'})
    assert resposta.status_code == 302 and resposta.location.endswith('/rh/equipe/lojas')
    assert _vinculos(bia) == {centro.id: True}
    html = cliente.get('/rh/equipe/lojas').get_data(as_text=True)
    assert 'Bia visual foi transferido(a) de Jardim para Centro.' in html

    resposta = cliente.post(f'/rh/equipe/lojas/{centro.id}/remover', data={
        'funcionario_id': ana.id, 'voltar': 'loja'})
    assert resposta.location.endswith(f'/rh/equipe/lojas?loja={centro.id}')
    html = cliente.get(resposta.location).get_data(as_text=True)
    assert 'Ana visual saiu da equipe de Centro; a ficha continua ativa no RH.' in html
    assert 'A pessoa ficou sem loja' in html
    assert _vinculos(ana) == {} and ana.ativo is True
    assert {log.usuario_id for log in _auditoria(ana)} == {chefe.id}


@pytest.mark.parametrize('acao', ['adicionar', 'importar', 'remover'])
def test_posts_exigem_dono_ou_chefia_de_rh(app, admin_user, acao):
    centro, jardim, lider, ana, bia, livre = _equipe()
    alvo = {'adicionar': livre, 'importar': bia, 'remover': ana}[acao]
    dados = {'funcionario_id': alvo.id}
    antes = _vinculos(alvo)
    assert _cliente(app).post(f'/rh/equipe/lojas/{centro.id}/{acao}', data=dados).status_code == 302
    assert _cliente(app, admin_user).post(
        f'/rh/equipe/lojas/{centro.id}/{acao}', data=dados).status_code == 403
    gerente = Usuario(nome='Gerente', login='gerente-lojas', papel='gerente')
    gerente.set_senha('x')
    db.session.add(gerente)
    db.session.commit()
    assert _cliente(app, gerente).post(
        f'/rh/equipe/lojas/{centro.id}/{acao}', data=dados).status_code == 403
    assert _vinculos(alvo) == antes
    assert AuditLog.query.filter_by(tabela='funcionario_loja').count() == 0
    assert f'rh.equipe_lojas_{acao}' in ENDPOINTS_RH


def test_post_invalido_avisa_sem_erro_500_e_loja_inexistente_da_404(app, owner_user):
    centro, jardim, lider, ana, bia, livre = _equipe()
    cliente = _cliente(app, owner_user)
    resposta = cliente.post(f'/rh/equipe/lojas/{centro.id}/adicionar', data={
        'funcionario_id': ana.id, 'voltar': 'loja'})
    assert resposta.status_code == 302
    html = cliente.get(resposta.location).get_data(as_text=True)
    assert 'Ana visual já está vinculado(a) a Centro.' in html
    resposta = cliente.post(f'/rh/equipe/lojas/{centro.id}/remover', data={
        'funcionario_id': 'abc', 'voltar': 'loja'})
    assert resposta.status_code == 302
    assert 'Escolha uma pessoa da lista.' in cliente.get(resposta.location).get_data(as_text=True)
    assert cliente.post('/rh/equipe/lojas/999999/adicionar',
                        data={'funcionario_id': livre.id}).status_code == 404
    assert _vinculos(livre) == {} and _vinculos(ana) == {centro.id: False}


def test_corrida_no_mesmo_vinculo_vira_aviso(app, owner_user, monkeypatch):
    centro, jardim, lider, ana, bia, livre = _equipe()

    def estoura(*args, **kwargs):
        raise IntegrityError('insert', {}, Exception('duplicado'))
    monkeypatch.setattr(acoes, '_inserir_vinculo', estoura)
    cliente = _cliente(app, owner_user)
    resposta = cliente.post(f'/rh/equipe/lojas/{centro.id}/adicionar', data={
        'funcionario_id': livre.id, 'voltar': 'loja'})
    assert resposta.status_code == 302
    assert 'acabou de ser alterado por outra pessoa' in cliente.get(
        resposta.location).get_data(as_text=True)
    assert _vinculos(livre) == {}
    assert AuditLog.query.filter_by(tabela='funcionario_loja').count() == 0


def test_loja_inativa_recusa_e_volta_para_todas(app, owner_user):
    fechada = _loja('Fechada', ativa=False)
    livre = _pessoa('Livre')
    db.session.commit()
    cliente = _cliente(app, owner_user)
    resposta = cliente.post(f'/rh/equipe/lojas/{fechada.id}/adicionar', data={
        'funcionario_id': livre.id, 'voltar': 'loja'})
    assert resposta.status_code == 302 and resposta.location.endswith('/rh/equipe/lojas')
    assert 'não está ativa' in cliente.get(resposta.location).get_data(as_text=True)
    assert _vinculos(livre) == {}


def test_nomes_sao_escapados_nos_formularios(app, owner_user):
    centro = _loja('Loja "aspas" <b>')
    ana = _pessoa('Ana "x" <script>', lojas=[centro])
    livre = _pessoa('<img src=x onerror=alert(1)>')
    db.session.commit()
    html = _cliente(app, owner_user).get(
        '/rh/equipe/lojas', query_string={'loja': centro.id}).get_data(as_text=True)
    assert 'Ana "x" <script>' not in html and '<img src=x onerror=alert(1)>' not in html
    assert 'Loja "aspas" <b>' not in html
    assert 'data-confirmar="Remover Ana &#34;x&#34; &lt;script&gt; da equipe de Loja &#34;aspas&#34; &lt;b&gt;?' in html
    assert '&lt;img src=x onerror=alert(1)&gt; · Sem cargo' in html
    assert livre.id and ana.id


def test_cadastro_novo_pre_marca_a_loja_vinda_da_equipe(app, owner_user):
    centro, jardim = _loja('Centro'), _loja('Jardim')
    db.session.commit()
    cliente = _cliente(app, owner_user)
    html = cliente.get('/rh/funcionarios/novo', query_string={'loja': centro.id}).get_data(as_text=True)
    assert f'value="{centro.id}" id="loja-{centro.id}" checked' in html
    assert f'value="{jardim.id}" id="loja-{jardim.id}">' in html
    html = cliente.get('/rh/funcionarios/novo').get_data(as_text=True)
    assert ' checked' not in html.split('name="lojas[]"')[1].split('>')[0]
    assert cliente.get('/rh/funcionarios/novo', query_string={'loja': 'abc'}).status_code == 200
