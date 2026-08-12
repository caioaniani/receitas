"""Import de contatos (e-mail + celular) por planilha — RH (05/08/2026).

Nasceu na rodada de assinatura do Regulamento Interno: o canal que sustenta
a prova é o da FICHA, então a planilha do gerente entra pelas fichas.
"""
import io

from openpyxl import Workbook

from app.extensions import db
from app.models import Funcionario, PreCadastroFuncionario


def _xlsx(linhas, cabecalho_na_linha_3=True):
    """Monta um xlsx no formato da planilha real (legenda + cabeçalho na 3)."""
    wb = Workbook()
    ws = wb.active
    if cabecalho_na_linha_3:
        # Legenda REAL da planilha do gerente: uma célula só, cujo texto
        # contém "e-mail" E "FUNCIONÁRIO" — foi ela que enganou a detecção
        # de cabeçalho na 1ª versão (bug real). O fixture trava isso.
        ws.append(['CONTATOS PARA ASSINATURA DO REGULAMENTO INTERNO — '
                   'preencher as células AMARELAS (e-mail e celular). O '
                   'celular precisa ser o WhatsApp DO FUNCIONÁRIO.'])
        ws.append([])
    ws.append(['Nº', 'Funcionário (ficha do RH)', 'Função', 'E-mail',
               'Celular (WhatsApp)', 'Observação'])
    ws.append(['—', 'EXEMPLO (não apagar)', 'Atendente',
               'maria@gmail.com', '(11) 99999-8888', ''])
    for ln in linhas:
        ws.append(ln)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _func(nome, cpf, email=None, telefone=None, ativo=True):
    f = Funcionario(nome=nome, cpf=cpf, email=email, telefone=telefone,
                    ativo=ativo)
    db.session.add(f)
    db.session.commit()
    return f


# ── ler_planilha ─────────────────────────────────────────────────────

def test_ler_acha_cabecalho_fora_da_linha_1_e_pula_exemplo(app):
    from app.services import contatos_import
    raw = _xlsx([[1, 'ANA SILVA', 'Atendente', 'ANA@X.com',
                  '(11) 98888-7777', '']])
    linhas, avisos = contatos_import.ler_planilha(raw)
    assert len(linhas) == 1 and not avisos
    assert linhas[0]['nome'] == 'ANA SILVA'
    assert linhas[0]['email'] == 'ana@x.com'            # normaliza
    assert linhas[0]['telefone'] == '11988887777'       # só dígitos


def test_ler_telefone_fixo_vira_aviso_e_campo_vazio(app):
    """Celular sem o 9 (o caso real da Isabela Fontes: 11376802857) não
    pode virar destino de token de assinatura."""
    from app.services import contatos_import
    raw = _xlsx([[1, 'ANA SILVA', '', 'ana@x.com', '11376802857', '']])
    linhas, avisos = contatos_import.ler_planilha(raw)
    assert linhas[0]['telefone'] == ''
    assert linhas[0]['email'] == 'ana@x.com'            # o e-mail sobrevive
    assert any('celular ilegível' in a for a in avisos)


def test_ler_email_torto_vira_aviso_e_campo_vazio(app):
    from app.services import contatos_import
    raw = _xlsx([[1, 'ANA SILVA', '', 'nao-e-email', '11988887777', '']])
    linhas, avisos = contatos_import.ler_planilha(raw)
    assert linhas[0]['email'] == ''
    assert linhas[0]['telefone'] == '11988887777'
    assert any('e-mail ilegível' in a for a in avisos)


def test_ler_marca_desligado_pela_observacao(app):
    from app.services import contatos_import
    raw = _xlsx([[1, 'ANA SILVA', '', '', '', 'funcionaria desligada da empresa']])
    linhas, _ = contatos_import.ler_planilha(raw)
    assert linhas[0]['desligado'] is True


def test_ler_telefone_com_55_na_frente(app):
    from app.services import contatos_import
    raw = _xlsx([[1, 'ANA SILVA', '', 'ana@x.com', '5511988887777', '']])
    linhas, _ = contatos_import.ler_planilha(raw)
    assert linhas[0]['telefone'] == '11988887777'


# ── comparar ─────────────────────────────────────────────────────────

def test_comparar_casa_por_nome_sem_acento_e_caixa(app):
    from app.services import contatos_import
    f = _func('JOÃO PEDRO DA SILVA', '111.111.111-11')
    prev = contatos_import.comparar([
        {'nome': 'Joao Pedro da Silva', 'email': 'jp@x.com',
         'telefone': '11988887777', 'desligado': False}])
    assert len(prev['atualizar']) == 1
    assert prev['atualizar'][0]['id'] == f.id
    assert prev['atualizar'][0]['difs']['email'][1] == 'jp@x.com'


def test_comparar_homonimo_vira_aviso_e_fica_fora(app):
    """Duas fichas com o mesmo nome: nunca chutar em quem grava contato."""
    from app.services import contatos_import
    _func('ANA SILVA', '111.111.111-11')
    _func('ANA SILVA', '222.222.222-22')
    prev = contatos_import.comparar([
        {'nome': 'Ana Silva', 'email': 'a@x.com', 'telefone': '',
         'desligado': False}])
    assert not prev['atualizar'] and not prev['novos']
    assert any('2 fichas' in a for a in prev['avisos'])


def test_comparar_sem_match_vira_novo_e_desligado_sem_match_vira_aviso(app):
    from app.services import contatos_import
    prev = contatos_import.comparar([
        {'nome': 'RAYANA DOS ANJOS', 'email': 'r@x.com',
         'telefone': '11988887777', 'desligado': False},
        {'nome': 'FULANO SUMIDO', 'email': '', 'telefone': '',
         'desligado': True}])
    assert [n['nome'] for n in prev['novos']] == ['RAYANA DOS ANJOS']
    assert any('FULANO SUMIDO' in a for a in prev['avisos'])


def test_comparar_desligado_na_planilha_vira_candidato(app):
    from app.services import contatos_import
    f = _func('LUAN COSTA', '111.111.111-11')
    ja = _func('LIDIANE PILOTO', '222.222.222-22', ativo=False)
    prev = contatos_import.comparar([
        {'nome': 'Luan Costa', 'email': '', 'telefone': '', 'desligado': True},
        {'nome': 'Lidiane Piloto', 'email': '', 'telefone': '',
         'desligado': True}])
    assert [d['id'] for d in prev['desligar']] == [f.id]
    assert ja.ativo is False                    # já estava — vai pra iguais
    assert len(prev['iguais']) == 1


def test_comparar_contatos_identicos_nao_viram_mudanca(app):
    from app.services import contatos_import
    _func('ANA SILVA', '111.111.111-11', email='a@x.com',
          telefone='11988887777')
    prev = contatos_import.comparar([
        {'nome': 'Ana Silva', 'email': 'a@x.com', 'telefone': '11988887777',
         'desligado': False}])
    assert not prev['atualizar'] and len(prev['iguais']) == 1


# ── aplicar ──────────────────────────────────────────────────────────

def test_aplicar_grava_email_e_telefone_na_ficha(app):
    from app.services import contatos_import
    f = _func('ANA SILVA', '111.111.111-11')
    st = contatos_import.aplicar({'atualizar': [
        {'id': f.id, 'email': 'Ana@X.com', 'telefone': '(11) 98888-7777'}]})
    assert st['atualizados'] == 1 and not st['erros']
    db.session.refresh(f)
    assert f.email == 'ana@x.com' and f.telefone == '11988887777'


def test_aplicar_nao_apaga_valor_existente_com_vazio(app):
    from app.services import contatos_import
    f = _func('ANA SILVA', '111.111.111-11', email='antigo@x.com',
              telefone='11911112222')
    contatos_import.aplicar({'atualizar': [
        {'id': f.id, 'email': '', 'telefone': '11988887777'}]})
    db.session.refresh(f)
    assert f.email == 'antigo@x.com'            # vazio não apaga
    assert f.telefone == '11988887777'


def test_aplicar_novo_vira_precadastro_nao_funcionario(app):
    from app.services import contatos_import
    st = contatos_import.aplicar({'precadastro': [
        {'nome': 'RAYANA DOS ANJOS DE OLIVEIRA', 'email': 'r@x.com',
         'telefone': '11979520652'}]})
    assert st['precadastros'] == 1
    assert Funcionario.query.count() == 0       # ficha só com CPF, no promover
    pre = PreCadastroFuncionario.query.one()
    assert pre.nome == 'RAYANA' and 'ANJOS' in pre.sobrenome
    assert pre.email == 'r@x.com'


def test_aplicar_desligar_marca_data_e_ignora_id_invalido(app):
    from app.services import contatos_import
    f = _func('LUAN COSTA', '111.111.111-11')
    st = contatos_import.aplicar({'desligar': [str(f.id), '99999', 'lixo']})
    assert st['desligados'] == 1
    db.session.refresh(f)
    assert f.ativo is False and f.data_demissao is not None


def test_aplicar_forjado_com_email_invalido_nao_grava(app):
    """A prévia é tela — POST forjado com lixo não entra na ficha."""
    from app.services import contatos_import
    f = _func('ANA SILVA', '111.111.111-11', email='antigo@x.com')
    st = contatos_import.aplicar({'atualizar': [
        {'id': f.id, 'email': 'nao-e-email', 'telefone': '123'}]})
    assert st['atualizados'] == 0 and st['erros']
    db.session.refresh(f)
    assert f.email == 'antigo@x.com'


# ── rotas ────────────────────────────────────────────────────────────

def _login(c, user_id):
    with c.session_transaction() as s:
        s['_user_id'] = str(user_id)
        s['_fresh'] = True


def test_rota_exige_login(app):
    assert app.test_client().get('/rh/contatos/importar').status_code \
        in (302, 401, 403)


def test_fluxo_upload_preview_pelo_owner(app, owner_user):
    uid = owner_user.id
    _func('ANA SILVA', '111.111.111-11')
    c = app.test_client()
    _login(c, uid)
    raw = _xlsx([[1, 'ANA SILVA', 'Atendente', 'ana@x.com',
                  '11988887777', '']])
    r = c.post('/rh/contatos/importar',
               data={'arquivo': (io.BytesIO(raw), 'contatos.xlsx')},
               content_type='multipart/form-data')
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert 'Contato novo ou diferente' in html
    assert 'ana@x.com' in html
