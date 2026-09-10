"""A visão por loja respeita a alocação e a liderança reais, sem dados privados."""
import json

import pytest
from sqlalchemy import event

from app.extensions import db
from app.models import Cargo, EquipeLiderCompartilhado, Funcionario, Loja, Usuario
from app.models.rh import funcionario_loja
from app.services.rh_equipe_lojas import carregar_lojas


def _loja(nome, *, ativa=True):
    loja = Loja(nome=nome, ativa=ativa)
    db.session.add(loja)
    db.session.flush()
    return loja


def _pessoa(nome, *, lojas=(), principal=None, periodo='Manhã', lider=None,
            ativo=True, acesso=False, **campos):
    numero = Funcionario.query.count() + 1
    usuario = None
    if acesso:
        usuario = Usuario(nome=nome, login=f'lojas-{numero}',
                          senha_hash='hash-apenas-de-teste', papel='funcionario')
        db.session.add(usuario)
        db.session.flush()
    pessoa = Funcionario(nome=nome, cpf=f'830{numero:08d}', ativo=ativo,
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


def _compartilhar(lider, parceiro, loja, autor_id, *, ativo=True):
    vinculo = EquipeLiderCompartilhado(
        lider=lider, parceiro=parceiro, loja=loja, periodo='Manhã',
        criado_por_id=autor_id, ativo=ativo)
    db.session.add(vinculo)
    db.session.flush()
    return vinculo


def _card(visao, loja):
    return next(card for card in visao['lojas'] if card['id'] == loja.id)


def _turno(card, nome):
    return next(turno for turno in card['turnos'] if turno['nome'] == nome)


def _grupo(card, periodo, lider):
    return next(grupo for grupo in _turno(card, periodo)['grupos']
                if grupo['lider']['id'] == lider.id)


def _ids(pessoas):
    return {pessoa['id'] for pessoa in pessoas}


def test_lojas_ativas_incluem_unidade_vazia(app):
    vazia = _loja('Unidade sem equipe')
    _loja('Unidade encerrada', ativa=False)
    db.session.commit()

    visao = carregar_lojas()

    assert [card['id'] for card in visao['lojas']] == [vazia.id]
    card = _card(visao, vazia)
    assert card['nome'] == vazia.nome
    assert card['total'] == card['total_lideres'] == 0
    assert all(turno['total'] == 0 and not turno['grupos']
               and not turno['sem_lider'] for turno in card['turnos'])
    assert card['outros_vinculos'] == []
    assert visao['sem_unidade'] == visao['direcao'] == []


def test_mesmo_lider_em_dois_turnos_e_lojas_nao_duplica_totais(app):
    norte, sul = _loja('Norte'), _loja('Sul')
    lider = _pessoa('Líder manhã', lojas=[norte, sul], principal=norte)
    tarde = _pessoa('Equipe tarde', lojas=[norte], periodo='Tarde', lider=lider)
    remota = _pessoa('Equipe sul', lojas=[sul], lider=lider)
    _pessoa('Pessoa desligada', lojas=[norte], lider=lider, ativo=False)
    db.session.commit()

    visao = carregar_lojas()
    card_norte, card_sul = _card(visao, norte), _card(visao, sul)

    assert (card_norte['total'], card_sul['total']) == (2, 1)
    assert card_norte['total_lideres'] == card_sul['total_lideres'] == 1
    assert _turno(card_norte, 'Manhã')['total'] == 1
    assert _turno(card_norte, 'Tarde')['total'] == 1
    assert _grupo(card_norte, 'Manhã', lider)['pessoas'] == []
    assert _ids(_grupo(card_norte, 'Tarde', lider)['pessoas']) == {tarde.id}
    grupo_sul = _grupo(card_sul, 'Manhã', lider)
    assert _ids(grupo_sul['pessoas']) == {remota.id}
    assert grupo_sul['lider']['unidade'] == norte.nome
    assert grupo_sul['lider']['periodo'] == 'Manhã'
    assert _ids(card_sul['outros_vinculos']) == {lider.id}
    assert not visao['sem_unidade']
    assert lider.usuario_id is tarde.usuario_id is remota.usuario_id is None


def test_lideres_locais_com_e_sem_superior_nao_viram_pendencia(app):
    loja = _loja('Equipe local')
    superior = _pessoa('Superior externo', periodo='Tarde')
    lider = _pessoa('Líder com superior', lojas=[loja], lider=superior)
    independente = _pessoa('Líder sem superior', lojas=[loja], periodo='Tarde')
    equipe_a = _pessoa('Equipe A', lojas=[loja], lider=lider)
    equipe_b = _pessoa('Equipe B', lojas=[loja], periodo='Tarde', lider=independente)
    db.session.commit()

    card = _card(carregar_lojas(), loja)

    assert card['total'] == 4 and card['total_lideres'] == 2
    assert _turno(card, 'Manhã')['total'] == _turno(card, 'Tarde')['total'] == 2
    grupo_a, grupo_b = _grupo(card, 'Manhã', lider), _grupo(card, 'Tarde', independente)
    assert grupo_a['lider']['lider_nome'] == superior.nome
    assert grupo_b['lider']['lider_nome'] is None
    assert not grupo_a['lider'].get('alerta')
    assert not grupo_b['lider'].get('alerta')
    assert _ids(grupo_a['pessoas']) == {equipe_a.id}
    assert _ids(grupo_b['pessoas']) == {equipe_b.id}
    assert all(not turno['sem_lider'] for turno in card['turnos'])
    assert {grupo['lider']['id'] for turno in card['turnos']
            for grupo in turno['grupos']} == {lider.id, independente.id}


def test_lider_inativo_inexistente_proprio_ou_ausente_preserva_pendencias(app):
    loja = _loja('Pendências')
    inativo = _pessoa('Líder desligado', ativo=False)
    subordinada = _pessoa('Líder inativo', lojas=[loja], lider=inativo)
    inexistente = _pessoa('Líder ausente do cadastro', lojas=[loja])
    inexistente.lider_id = 99999
    propria = _pessoa('Líder de si', lojas=[loja], periodo='Noite')
    propria.lider_id = propria.id
    sem_lider = _pessoa('Gerente sem equipe', lojas=[loja], periodo=None,
                        funcao='GERENTE')
    db.session.commit()

    card = _card(carregar_lojas(), loja)

    assert card['total'] == 4 and card['total_lideres'] == 0
    assert all(not turno['grupos'] for turno in card['turnos'])
    pendentes = {pessoa['id']: pessoa for turno in card['turnos']
                 for pessoa in turno['sem_lider']}
    assert set(pendentes) == {subordinada.id, inexistente.id, propria.id, sem_lider.id}
    assert all(pessoa.get('alerta') for pessoa in pendentes.values())
    assert pendentes[subordinada.id]['lider_nome'] == inativo.nome
    assert pendentes[propria.id]['periodo'] == 'Noite'
    assert pendentes[sem_lider.id]['periodo'] is None
    assert _ids(_turno(card, 'Período não definido')['sem_lider']) == {
        propria.id, sem_lider.id}


def test_unidade_principal_e_vinculos_secundarios_sem_duplicar_pessoas(app):
    norte, sul = _loja('Principal norte'), _loja('Principal sul')
    explicita = _pessoa('Principal informada', lojas=[norte, sul], principal=sul)
    unica = _pessoa('Vínculo único legado', lojas=[norte])
    indefinida = _pessoa('Duas lojas sem principal', lojas=[norte, sul])
    sem_loja = _pessoa('Nenhuma associação', lojas=[])
    db.session.commit()

    visao = carregar_lojas()
    card_norte, card_sul = _card(visao, norte), _card(visao, sul)

    assert (card_norte['total'], card_sul['total']) == (1, 1)
    assert _ids(_turno(card_norte, 'Manhã')['sem_lider']) == {unica.id}
    assert _ids(_turno(card_sul, 'Manhã')['sem_lider']) == {explicita.id}
    assert _ids(card_norte['outros_vinculos']) == {explicita.id, indefinida.id}
    assert _ids(card_sul['outros_vinculos']) == {indefinida.id}
    assert _ids(visao['sem_unidade']) == {indefinida.id, sem_loja.id}
    assert all(pessoa['unidade'] is None for pessoa in visao['sem_unidade'])
    assert sum(card['total'] for card in visao['lojas']) + len(visao['sem_unidade']) == 4


def test_compartilhamento_direcional_so_anota_origem_loja_turno_e_nao_propaga(app, owner_user):
    loja, outra = _loja('Compartilhada'), _loja('Outra unidade')
    a, b, c = [_pessoa(nome, lojas=[loja], acesso=True) for nome in ('Ana', 'Bia', 'Caio')]
    equipe_a = _pessoa('Equipe Ana', lojas=[loja], lider=a)
    equipe_b = _pessoa('Equipe Bia', lojas=[loja], lider=b)
    equipe_c = _pessoa('Equipe Caio', lojas=[loja], lider=c)
    tarde = _pessoa('Ana tarde', lojas=[loja], periodo='Tarde', lider=a)
    remota = _pessoa('Ana outra loja', lojas=[outra], lider=a)
    sem_conta = _pessoa('Parceiro sem acesso', lojas=[loja])
    revogado = _pessoa('Parceiro revogado', lojas=[loja], acesso=True)
    _compartilhar(a, b, loja, owner_user.id)
    _compartilhar(b, c, loja, owner_user.id)
    _compartilhar(a, sem_conta, loja, owner_user.id)
    _compartilhar(a, revogado, loja, owner_user.id, ativo=False)
    db.session.commit()

    visao = carregar_lojas()
    card = _card(visao, loja)

    assert card['total'] == 9 and card['total_lideres'] == 3
    assert _ids(_grupo(card, 'Manhã', a)['compartilhados']) == {b.id}
    assert _ids(_grupo(card, 'Manhã', b)['compartilhados']) == {c.id}
    assert _grupo(card, 'Manhã', c)['compartilhados'] == []
    assert _ids(_grupo(card, 'Manhã', a)['pessoas']) == {equipe_a.id}
    assert _ids(_grupo(card, 'Manhã', b)['pessoas']) == {equipe_b.id}
    assert _ids(_grupo(card, 'Manhã', c)['pessoas']) == {equipe_c.id}
    assert _ids(_grupo(card, 'Tarde', a)['pessoas']) == {tarde.id}
    assert _grupo(card, 'Tarde', a)['compartilhados'] == []
    grupo_remoto = _grupo(_card(visao, outra), 'Manhã', a)
    assert _ids(grupo_remoto['pessoas']) == {remota.id}
    assert grupo_remoto['compartilhados'] == []
    assert equipe_a.lider_id == a.id and equipe_b.lider_id == b.id
    assert EquipeLiderCompartilhado.query.count() == 4


def test_parceiro_compartilhado_sem_equipe_propria_e_superior_vira_lider_local(app, owner_user):
    loja = _loja('Parceria operacional')
    lider = _pessoa('Liderança de referência', lojas=[loja], acesso=True)
    parceiro = _pessoa('Parceiro sem equipe própria', lojas=[loja], acesso=True)
    pessoa = _pessoa('Equipe compartilhada', lojas=[loja], lider=lider)
    _compartilhar(lider, parceiro, loja, owner_user.id)
    db.session.commit()
    assert parceiro.lider_id is None
    assert Funcionario.query.filter_by(lider_id=parceiro.id).count() == 0

    card = _card(carregar_lojas(), loja)
    turno = _turno(card, 'Manhã')
    origem = _grupo(card, 'Manhã', lider)
    parceria = _grupo(card, 'Manhã', parceiro)

    assert card['total'] == turno['total'] == 3
    assert card['total_lideres'] == 2
    assert len(turno['grupos']) == 2
    assert _ids([grupo['lider'] for grupo in turno['grupos']]) == {lider.id, parceiro.id}
    assert turno['sem_lider'] == []
    assert _ids(origem['pessoas']) == {pessoa.id}
    assert [p['id'] for p in origem['compartilhados']] == [parceiro.id]
    assert parceria['pessoas'] == parceria['compartilhados'] == []
    assert parceria['lider']['lider_nome'] is None
    assert not parceria['lider'].get('alerta')
    assert sum(len(grupo['pessoas']) for grupo in turno['grupos']) == 1
    assert pessoa.lider_id == lider.id and parceiro.lider_id is None


@pytest.mark.parametrize('equipe', ['nenhuma', 'inativa', 'somente_parceiro',
                                  'outro_turno', 'outra_loja'])
def test_compartilhamento_sem_equipe_ativa_no_escopo_nao_cria_lider_fantasma(
        app, owner_user, equipe):
    loja, outra = _loja('Escopo compartilhado'), _loja('Fora do escopo')
    lider = _pessoa('Referência sem equipe no escopo', lojas=[loja], acesso=True)
    parceiro = _pessoa('Parceiro potencial', lojas=[loja], acesso=True)
    pessoa = None
    if equipe == 'inativa':
        _pessoa('Antiga equipe', lojas=[loja], lider=lider, ativo=False)
    elif equipe == 'somente_parceiro':
        parceiro.lider_id = lider.id
    elif equipe == 'outro_turno':
        pessoa = _pessoa('Equipe tarde', lojas=[loja], lider=lider, periodo='Tarde')
    elif equipe == 'outra_loja':
        pessoa = _pessoa('Equipe externa', lojas=[outra], lider=lider)
    _compartilhar(lider, parceiro, loja, owner_user.id)
    db.session.commit()

    visao = carregar_lojas()
    card = _card(visao, loja)

    assert all(grupo['lider']['id'] != parceiro.id
               and not grupo['compartilhados']
               for unidade in visao['lojas'] for turno in unidade['turnos']
               for grupo in turno['grupos'])
    assert card['total'] == (3 if equipe == 'outro_turno' else 2)
    assert card['total_lideres'] == int(equipe in ('somente_parceiro', 'outro_turno'))
    if equipe == 'somente_parceiro':
        assert _ids(_grupo(card, 'Manhã', lider)['pessoas']) == {parceiro.id}
    else:
        assert parceiro.id in _ids(_turno(card, 'Manhã')['sem_lider'])
    if equipe == 'outro_turno':
        assert _ids(_grupo(card, 'Tarde', lider)['pessoas']) == {pessoa.id}
    elif equipe == 'outra_loja':
        assert _ids(_grupo(_card(visao, outra), 'Manhã', lider)['pessoas']) == {pessoa.id}
    assert EquipeLiderCompartilhado.query.one().ativo


def test_compartilhamento_legado_em_periodo_nao_suportado_nao_cria_lider_local(app, owner_user):
    loja = _loja('Período legado')
    lider = _pessoa('Referência noturna', lojas=[loja], periodo='Noite', acesso=True)
    parceiro = _pessoa('Parceiro noturno', lojas=[loja], periodo='Noite', acesso=True)
    pessoa = _pessoa('Equipe noturna', lojas=[loja], periodo='Noite', lider=lider)
    vinculo = _compartilhar(lider, parceiro, loja, owner_user.id)
    vinculo.periodo = 'Noite'
    db.session.commit()

    card = _card(carregar_lojas(), loja)
    turno = _turno(card, 'Período não definido')
    grupo = _grupo(card, 'Período não definido', lider)

    assert card['total'] == turno['total'] == 3
    assert card['total_lideres'] == 1
    assert [grupo['lider']['id'] for grupo in turno['grupos']] == [lider.id]
    assert _ids(grupo['pessoas']) == {pessoa.id}
    assert grupo['compartilhados'] == []
    assert _ids(turno['sem_lider']) == {parceiro.id}
    assert turno['sem_lider'][0]['periodo'] == 'Noite'
    assert grupo['lider']['periodo'] == 'Noite'


@pytest.mark.parametrize('mudanca', ['lider_inativo', 'parceiro_inativo',
                                    'turno', 'unidade', 'sem_conta', 'revogado'])
def test_compartilhamento_deixa_de_aparecer_quando_escopo_muda(app, owner_user, mudanca):
    loja, outra = _loja('Escopo vivo'), _loja('Destino novo')
    lider = _pessoa('Referência', lojas=[loja], acesso=True)
    parceiro = _pessoa('Parceiro', lojas=[loja], acesso=True)
    pessoa = _pessoa('Pessoa acompanhada', lojas=[loja], lider=lider)
    vinculo = _compartilhar(lider, parceiro, loja, owner_user.id)
    db.session.commit()
    assert _ids(_grupo(_card(carregar_lojas(), loja), 'Manhã', lider)['compartilhados']) == {
        parceiro.id}

    if mudanca == 'lider_inativo':
        lider.ativo = False
    elif mudanca == 'parceiro_inativo':
        parceiro.ativo = False
    elif mudanca == 'turno':
        parceiro.periodo = 'Tarde'
    elif mudanca == 'unidade':
        parceiro.lojas = [outra]
    elif mudanca == 'sem_conta':
        parceiro.usuario_id = None
    else:
        vinculo.ativo = False
    db.session.commit()

    visao = carregar_lojas()

    assert all(not grupo['compartilhados'] for card in visao['lojas']
               for turno in card['turnos'] for grupo in turno['grupos'])
    assert pessoa.lider_id == lider.id
    assert EquipeLiderCompartilhado.query.count() == 1


def test_direcao_por_cadastro_e_owner_fica_fora_dos_totais_operacionais(app, owner_user):
    norte, sul = _loja('Direção norte'), _loja('Direção sul')
    owner = _pessoa('Dona com conta owner', lojas=[norte, sul], principal=norte)
    owner.usuario = owner_user
    direcao = _pessoa('Diretor cadastro 45', id=45, periodo=None)
    administrativo = _pessoa('Admin operacional', lojas=[norte], acesso=True)
    administrativo.usuario.papel = 'admin'
    db.session.commit()

    visao = carregar_lojas()

    assert _ids(visao['direcao']) == {owner.id, direcao.id}
    assert not visao['sem_unidade']
    assert _card(visao, norte)['total'] == 1
    assert _card(visao, sul)['total'] == 0
    assert all(not card['outros_vinculos'] for card in visao['lojas'])
    assert not any(pessoa.get('alerta') for pessoa in visao['direcao'])
    assert _ids(_turno(_card(visao, norte), 'Manhã')['sem_lider']) == {administrativo.id}


def test_resultado_so_contem_dados_publicos_e_nomes_sao_texto_comum(app):
    nome_malicioso = '<script>alert("pessoa")</script>'
    loja = _loja('<img src=x onerror=alert("loja")>')
    cargo = Cargo(nome='ATENDENTE', salario_base=9876.54)
    db.session.add(cargo)
    lider = _pessoa('<b>Líder</b>', lojas=[loja])
    pessoa = _pessoa(nome_malicioso, lojas=[loja], lider=lider, cargo=cargo,
                     salario_base=9876.54, email='privado@example.test',
                     telefone='11987654321', observacao='OBSERVAÇÃO PRIVADA')
    legado = _pessoa('Função legada', lojas=[loja], lider=lider,
                     funcao='<svg onload=alert("cargo")>')
    db.session.commit()
    antes = (pessoa.cargo_id, pessoa.salario_base, pessoa.lider_id)

    visao = carregar_lojas()
    card = _card(visao, loja)
    linhas = {linha['id']: linha for linha in _grupo(card, 'Manhã', lider)['pessoas']}

    assert type(card['nome']) is str and card['nome'] == loja.nome
    assert type(linhas[pessoa.id]['nome']) is str
    assert linhas[pessoa.id]['nome'] == nome_malicioso
    assert linhas[pessoa.id]['cargo'] == 'Atendente 1'
    assert linhas[legado.id]['cargo'] == legado.funcao
    assert type(linhas[legado.id]['cargo']) is str
    publicos = {'id', 'nome', 'cargo', 'periodo', 'unidade', 'lider_nome', 'alerta'}
    for linha in [*_grupo(card, 'Manhã', lider)['pessoas'],
                  _grupo(card, 'Manhã', lider)['lider']]:
        assert set(linha) <= publicos
        assert publicos - {'alerta'} <= set(linha)

    privados = {'cpf', 'email', 'telefone', 'observacao', 'salario_base',
                'salario', 'senha_hash', 'usuario', 'usuario_id', 'pessoa',
                'funcionario', 'cargo_id', 'data_nascimento'}

    def verificar(valor):
        if type(valor) is dict:
            assert not privados.intersection(valor)
            assert all(type(chave) is str for chave in valor)
            for item in valor.values():
                verificar(item)
        elif type(valor) is list:
            for item in valor:
                verificar(item)
        else:
            assert type(valor) in (str, int, float, bool, type(None))

    verificar(visao)
    serializado = json.dumps(visao, ensure_ascii=False)
    assert pessoa.cpf not in serializado
    assert pessoa.email not in serializado
    assert pessoa.telefone not in serializado
    assert pessoa.observacao not in serializado
    assert (pessoa.cargo_id, pessoa.salario_base, pessoa.lider_id) == antes
    assert cargo.nome == 'ATENDENTE'


def test_consultas_nao_crescem_por_pessoa_loja_ou_lider_e_visao_nao_grava(app):
    def adicionar_equipe(numero):
        loja = _loja(f'Loja de carga {numero}')
        lider = _pessoa(f'Líder de carga {numero}', lojas=[loja])
        _pessoa(f'Equipe de carga {numero}', lojas=[loja], lider=lider)

    def carregar_sem_cache():
        db.session.expunge_all()
        consultas = []

        def registrar(conn, cursor, statement, parameters, context, executemany):
            consultas.append(statement)

        event.listen(db.engine, 'before_cursor_execute', registrar)
        try:
            visao = carregar_lojas()
        finally:
            event.remove(db.engine, 'before_cursor_execute', registrar)
        assert consultas
        assert all(sql.lstrip().upper().startswith('SELECT') for sql in consultas)
        return visao, len(consultas)

    adicionar_equipe(0)
    db.session.commit()
    pequena, consultas_pequena = carregar_sem_cache()
    assert sum(card['total'] for card in pequena['lojas']) == 2

    for numero in range(1, 13):
        adicionar_equipe(numero)
    db.session.commit()
    grande, consultas_grande = carregar_sem_cache()

    assert len(grande['lojas']) == 13
    assert sum(card['total'] for card in grande['lojas']) == 26
    assert consultas_grande == consultas_pequena
