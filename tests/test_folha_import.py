"""Importação da folha de pagamento (xlsx) pro RH — 03/08/2026.

Salário é dinheiro: nada é gravado sem prévia + checkbox; funcionário fora
da folha NUNCA é desligado sozinho; linha ilegível vira aviso, não sumiço."""
import io

from app.extensions import db


def _xlsx(linhas, cabecalho=None):
    """Monta um xlsx de folha em memória, no formato da contabilidade."""
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.title = 'Funcionários'
    ws.append(cabecalho or ['Matrícula', 'Nome', 'CPF', 'Admissão',
                            'Cargo', 'Salário Base'])
    for ln in linhas:
        ws.append(ln)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _func(nome='AMANDA DE SOUZA', cpf='42326669827', salario=2000.0,
          funcao='ATENDENTE', ativo=True):
    from app.models import Funcionario
    f = Funcionario(nome=nome, cpf=cpf, funcao=funcao,
                    salario_base=salario, ativo=ativo)
    db.session.add(f)
    db.session.commit()
    return f


L1 = ['17', 'AMANDA DE SOUZA', '423.266.698-27', '12/01/2026',
      'ATENDENTE', 2130.4]


def test_le_a_planilha_da_contabilidade(app):
    from app.services import folha_import
    linhas, avisos = folha_import.ler_folha(_xlsx([L1]))
    assert avisos == []
    assert linhas[0]['cpf'] == '42326669827'      # só dígitos
    assert linhas[0]['salario'] == 2130.4
    assert linhas[0]['admissao'].isoformat() == '2026-01-12'


def test_colunas_por_nome_nao_por_posicao(app):
    """A contabilidade muda a ordem das colunas entre meses."""
    from app.services import folha_import
    raw = _xlsx([['AMANDA', 2130.4, '423.266.698-27']],
                cabecalho=['Nome', 'Salário Base', 'CPF'])
    linhas, _ = folha_import.ler_folha(raw)
    assert linhas[0]['salario'] == 2130.4


def test_cpf_ilegivel_vira_aviso_nao_sumico(app):
    from app.services import folha_import
    raw = _xlsx([L1, ['9', 'FULANO SEM CPF', '123', '', 'PADEIRO', 3000]])
    linhas, avisos = folha_import.ler_folha(raw)
    assert len(linhas) == 1
    assert any('FULANO SEM CPF' in a for a in avisos)


def test_comparar_classifica_novo_alterado_igual_fora(app):
    from app.services import folha_import
    _func()                                        # igual à folha? não: 2000 vs 2130.40
    _func(nome='JOAO FORA', cpf='11144477735', funcao='PADEIRO')
    linhas, _ = folha_import.ler_folha(_xlsx([
        L1,
        ['41', 'MARIA NOVA', '531.913.688-92', '02/04/2026',
         'ATENDENTE', 2130.4],
    ]))
    prev = folha_import.comparar(linhas)
    assert [n['nome'] for n in prev['novos']] == ['MARIA NOVA']
    assert [a['nome'] for a in prev['alterados']] == ['AMANDA DE SOUZA']
    assert prev['alterados'][0]['difs']['salario'] == (2000.0, 2130.4)
    assert [f.nome for f in prev['fora_da_folha']] == ['JOAO FORA']


def test_comparar_nao_grava_nada(app):
    from app.models import Funcionario
    from app.services import folha_import
    f = _func()
    linhas, _ = folha_import.ler_folha(_xlsx([L1]))
    folha_import.comparar(linhas)
    db.session.expire_all()
    assert Funcionario.query.get(f.id).salario_base == 2000.0


def test_aplicar_so_o_marcado(app):
    from app.models import Funcionario
    from app.services import folha_import
    f = _func()
    fora = _func(nome='JOAO FORA', cpf='11144477735')
    stats = folha_import.aplicar({
        'atualizar': [{'nome': 'AMANDA', 'cpf': '42326669827',
                       'cargo': 'ATENDENTE 2', 'salario': 2130.4,
                       'admissao': '2026-01-12'}],
        'criar': [], 'desligar': []})
    assert stats['atualizados'] == 1 and stats['erros'] == []
    db.session.expire_all()
    f = Funcionario.query.get(f.id)
    assert f.salario_base == 2130.4 and f.funcao == 'ATENDENTE 2'
    assert Funcionario.query.get(fora.id).ativo is True   # NÃO desligou


def test_desligar_e_gesto_explicito_por_pessoa(app):
    from app.models import Funcionario
    from app.services import folha_import
    fora = _func(nome='JOAO FORA', cpf='11144477735')
    stats = folha_import.aplicar({'criar': [], 'atualizar': [],
                                  'desligar': [fora.id]})
    assert stats['desligados'] == 1
    db.session.expire_all()
    f = Funcionario.query.get(fora.id)
    assert f.ativo is False and f.data_demissao is not None


def test_reativa_quem_voltou_pra_folha(app):
    from app.models import Funcionario
    from app.services import folha_import
    f = _func(ativo=False)
    stats = folha_import.aplicar({
        'atualizar': [{'nome': 'AMANDA', 'cpf': '42326669827',
                       'cargo': '', 'salario': 2130.4, 'admissao': None}],
        'criar': [], 'desligar': []})
    assert stats['reativados'] == 1
    db.session.expire_all()
    assert Funcionario.query.get(f.id).ativo is True


def test_criar_com_cpf_ja_cadastrado_recusa(app):
    from app.services import folha_import
    _func()
    stats = folha_import.aplicar({
        'criar': [{'nome': 'CLONE', 'cpf': '423.266.698-27',
                   'cargo': 'X', 'salario': 1000, 'admissao': None}],
        'atualizar': [], 'desligar': []})
    assert stats['criados'] == 0
    assert any('já cadastrado' in e for e in stats['erros'])


# ── Rotas (owner-only pelo gate do RH) ───────────────────────────────────

def _owner_client(app):
    from app.models import Usuario
    u = Usuario(nome='Dono', login='dono_rh', papel='admin', is_owner=True)
    u.set_senha('x' * 8)
    db.session.add(u)
    db.session.commit()
    c = app.test_client()
    with c.session_transaction() as s:
        s['_user_id'] = str(u.id)
        s['_fresh'] = True
    return c


def test_fluxo_completo_pela_tela(app):
    from app.models import Funcionario
    c = _owner_client(app)
    r = c.post('/rh/folha/importar',
               data={'arquivo': (io.BytesIO(_xlsx([L1])), 'folha.xlsx')},
               content_type='multipart/form-data')
    html = r.data.decode()
    assert 'AMANDA DE SOUZA' in html and 'não no sistema' in html
    # aplica o que a prévia embutiu
    import json
    r = c.post('/rh/folha/importar/aplicar', data={
        'criar': json.dumps({'nome': 'AMANDA DE SOUZA',
                             'cpf': '42326669827', 'cargo': 'ATENDENTE',
                             'salario': 2130.4, 'admissao': '2026-01-12'}),
    }, follow_redirects=True)
    assert r.status_code == 200
    f = Funcionario.query.filter_by(cpf='42326669827').first()
    assert f is not None and f.salario_base == 2130.4


def test_admin_comum_nao_entra(app):
    from app.models import Usuario
    u = Usuario(nome='Adm', login='adm_rh', papel='admin')
    u.set_senha('x' * 8)
    db.session.add(u)
    db.session.commit()
    c = app.test_client()
    with c.session_transaction() as s:
        s['_user_id'] = str(u.id)
        s['_fresh'] = True
    assert c.get('/rh/folha/importar').status_code in (302, 403)
