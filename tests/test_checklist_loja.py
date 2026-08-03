"""Checklist de loja (03/08/2026) — abertura / troca de turno / fechamento.

Pedido do dono: responsável do turno preenche no celular com foto
comprovando os pontos marcados. Decisões (AskUserQuestion): tela no celular,
itens cadastráveis, foto por item selecionado, pendência na home.

Dropbox SEMPRE mockado (padrão da casa)."""
from datetime import time as _time
from datetime import timedelta
from io import BytesIO
from unittest.mock import patch

import pytest

from app.extensions import db
from app.models import (
    ChecklistItemModelo,
    ChecklistPreenchimento,
    ChecklistResposta,
    Loja,
    Usuario,
)
from app.services import checklist_loja
from app.utils import hoje


def _loja(nome='Loja A', dias=None):
    lj = Loja(nome=nome, ativa=True, dias_funcionamento=dias)
    db.session.add(lj)
    db.session.commit()
    return lj


def _item(tipo='abertura', texto='Vitrine montada', exige_foto=False,
          loja_id=None, ordem=0, ativo=True):
    it = ChecklistItemModelo(tipo=tipo, texto=texto, exige_foto=exige_foto,
                             loja_id=loja_id, ordem=ordem, ativo=ativo)
    db.session.add(it)
    db.session.commit()
    return it


def _user(login='atend', papel='funcionario', loja_id=None):
    u = Usuario(nome='Atendente Chefe', login=login, papel=papel,
                loja_id=loja_id)
    u.set_senha('123')
    db.session.add(u)
    db.session.commit()
    return u


def _login(client, login, senha='123'):
    client.post('/auth/login', data={'login': login, 'senha': senha})


def _resp(itens, overrides=None):
    """Respostas OK pra todos os itens; `overrides` = {item_id: {campo: v}}."""
    base = {it.id: {'ok': True, 'observacao': None, 'foto': None}
            for it in itens}
    for iid, d in (overrides or {}).items():
        base[iid] = {**base[iid], **d}
    return base


@pytest.fixture
def dropbox_ok():
    with patch('app.services.dropbox_storage.disponivel', return_value=True), \
         patch('app.utils.comprimir_imagem', side_effect=lambda b, **kw: b), \
         patch('app.services.dropbox_storage.upload_publico',
               return_value={'url': 'https://dl.dropbox.com/x?raw=1',
                             'storage_path': '/checklists/x.jpg',
                             'tamanho': 3}) as up:
        yield up


# ── itens_para / tipos_configurados ─────────────────────────────────

def test_itens_globais_mais_da_loja_na_ordem(app):
    with app.app_context():
        lj = _loja()
        outra = _loja('Loja B')
        _item(texto='Global 2', ordem=2)
        _item(texto='Global 1', ordem=1)
        _item(texto='Só da A', loja_id=lj.id, ordem=3)
        _item(texto='Só da B', loja_id=outra.id)
        _item(texto='Inativo', ativo=False)
        _item(texto='De fechamento', tipo='fechamento')
        itens = checklist_loja.itens_para(lj.id, 'abertura')
        assert [i.texto for i in itens] == ['Global 1', 'Global 2', 'Só da A']


def test_tipos_configurados_so_com_item_ativo(app):
    with app.app_context():
        lj = _loja()
        _item(tipo='abertura')
        _item(tipo='fechamento', ativo=False)
        cfg = checklist_loja.tipos_configurados(lj.id)
        assert 'abertura' in cfg
        assert 'fechamento' not in cfg
        assert 'troca_turno' not in cfg


# ── registrar ───────────────────────────────────────────────────────

def test_registrar_grava_com_snapshot(app):
    with app.app_context():
        lj = _loja()
        u = _user()
        i1 = _item(texto='Vitrine montada')
        i2 = _item(texto='Caixa conferido', ordem=1)
        p = checklist_loja.registrar(
            lj, 'abertura', u.id,
            _resp([i1, i2], {i2.id: {'ok': False, 'observacao': 'faltou troco'}}))
        assert p.data == hoje()
        assert p.usuario_id == u.id
        assert len(p.respostas) == 2
        r2 = next(r for r in p.respostas if r.item_id == i2.id)
        assert r2.ok is False and r2.observacao == 'faltou troco'
        assert p.n_problemas == 1

        # SNAPSHOT: editar o cadastro depois não reescreve a história
        i1.texto = 'Texto novo'
        db.session.commit()
        r1 = ChecklistResposta.query.filter_by(item_id=i1.id).first()
        assert r1.item_texto == 'Vitrine montada'


def test_item_sem_resposta_recusa_e_nada_grava(app):
    with app.app_context():
        lj = _loja()
        u = _user()
        i1 = _item(texto='Vitrine')
        _item(texto='Caixa')
        with pytest.raises(ValueError, match='Caixa'):
            checklist_loja.registrar(lj, 'abertura', u.id, _resp([i1]))
        assert ChecklistPreenchimento.query.count() == 0


def test_exige_foto_sem_foto_recusa(app):
    with app.app_context():
        lj = _loja()
        u = _user()
        it = _item(texto='Loja trancada', exige_foto=True)
        with pytest.raises(ValueError, match='FOTO'):
            checklist_loja.registrar(lj, 'abertura', u.id, _resp([it]))
        assert ChecklistPreenchimento.query.count() == 0


def test_problema_sem_observacao_recusa(app):
    with app.app_context():
        lj = _loja()
        u = _user()
        it = _item(texto='Freezer ligado')
        with pytest.raises(ValueError, match='observa'):
            checklist_loja.registrar(
                lj, 'abertura', u.id,
                _resp([it], {it.id: {'ok': False}}))


def test_foto_sobe_pro_dropbox_e_grava_url(app, dropbox_ok):
    with app.app_context():
        lj = _loja()
        u = _user()
        it = _item(texto='Vitrine', exige_foto=True)
        p = checklist_loja.registrar(
            lj, 'abertura', u.id,
            _resp([it], {it.id: {'ok': True, 'foto': b'jpg'}}))
        assert dropbox_ok.called
        path = dropbox_ok.call_args[0][1]
        assert path.startswith(f'/checklists/{lj.id}/{hoje().isoformat()}/')
        r = p.respostas[0]
        assert r.foto_url == 'https://dl.dropbox.com/x?raw=1'
        assert r.exigia_foto is True


def test_dropbox_fora_recusa_fail_close(app):
    """A foto é a PROVA pedida pelo dono — aceitar sem ela seria o registro
    mentindo em silêncio. Dropbox fora = erro claro, nada gravado."""
    with app.app_context():
        lj = _loja()
        u = _user()
        it = _item(texto='Vitrine', exige_foto=True)
        with patch('app.services.dropbox_storage.disponivel',
                   return_value=False):
            with pytest.raises(ValueError, match='indispon'):
                checklist_loja.registrar(
                    lj, 'abertura', u.id,
                    _resp([it], {it.id: {'ok': True, 'foto': b'jpg'}}))
        assert ChecklistPreenchimento.query.count() == 0


def test_foto_ilegivel_recusa_com_o_nome_do_item(app):
    with app.app_context():
        lj = _loja()
        u = _user()
        it = _item(texto='Vitrine', exige_foto=True)
        with patch('app.services.dropbox_storage.disponivel',
                   return_value=True), \
             patch('app.utils.comprimir_imagem',
                   side_effect=ValueError('heic')):
            with pytest.raises(ValueError, match='Vitrine'):
                checklist_loja.registrar(
                    lj, 'abertura', u.id,
                    _resp([it], {it.id: {'ok': True, 'foto': b'heic'}}))
        assert ChecklistPreenchimento.query.count() == 0


def test_sem_item_cadastrado_recusa(app):
    with app.app_context():
        lj = _loja()
        u = _user()
        with pytest.raises(ValueError, match='Nenhum item'):
            checklist_loja.registrar(lj, 'abertura', u.id, {})


# ── rotas ───────────────────────────────────────────────────────────

def test_preencher_exige_login(app):
    c = app.test_client()
    r = c.get('/checklist/')
    assert r.status_code == 302 and '/auth/login' in r.headers['Location']


def test_funcionario_preenche_pela_tela(app, dropbox_ok):
    with app.app_context():
        lj = _loja()
        _user(login='atend', loja_id=lj.id)
        i1 = _item(texto='Vitrine montada')
        i2 = _item(texto='Loja trancada', exige_foto=True)
        lid, i1id, i2id = lj.id, i1.id, i2.id
    c = app.test_client()
    _login(c, 'atend')
    r = c.post('/checklist/preencher', data={
        'loja': lid, 'tipo': 'abertura',
        f'ok_{i1id}': 'ok',
        f'ok_{i2id}': 'problema', f'obs_{i2id}': 'fechadura solta',
        f'foto_{i2id}': (BytesIO(b'jpg'), 'foto.jpg'),
    }, content_type='multipart/form-data', follow_redirects=True)
    assert r.status_code == 200
    with app.app_context():
        p = ChecklistPreenchimento.query.one()
        assert p.tipo == 'abertura' and p.loja_id == lid
        assert p.n_problemas == 1
        r2 = next(x for x in p.respostas if x.item_id == i2id)
        assert r2.foto_url


def test_post_incompleto_reapresenta_com_erro(app):
    with app.app_context():
        lj = _loja()
        _user(login='atend')
        it = _item(texto='Loja trancada', exige_foto=True)
        lid, iid = lj.id, it.id
    c = app.test_client()
    _login(c, 'atend')
    r = c.post('/checklist/preencher', data={
        'loja': lid, 'tipo': 'abertura', f'ok_{iid}': 'ok',
    }, content_type='multipart/form-data')
    assert r.status_code == 422
    assert 'FOTO' in r.get_data(as_text=True)
    with app.app_context():
        assert ChecklistPreenchimento.query.count() == 0


def test_padeiro_barrado(app):
    with app.app_context():
        _user(login='pad', papel='padeiro')
    c = app.test_client()
    _login(c, 'pad')
    assert c.get('/checklist/').status_code == 403


def test_config_e_conferencia_sao_admin_only(app):
    with app.app_context():
        _user(login='atend')
    c = app.test_client()
    _login(c, 'atend')
    assert c.get('/checklist/config').status_code == 403
    assert c.get('/checklist/conferencia').status_code == 403


def test_admin_cadastra_item_pela_tela(app, admin_user):
    c = app.test_client()
    _login(c, 'admin')
    r = c.post('/checklist/config', data={
        'acao': 'novo', 'texto': 'Vitrine montada e limpa',
        'tipo': 'abertura', 'exige_foto': '1', 'ordem': '2',
    }, follow_redirects=True)
    assert r.status_code == 200
    with app.app_context():
        it = ChecklistItemModelo.query.one()
        assert it.exige_foto is True and it.ordem == 2
        assert it.loja_id is None


def test_excluir_item_usado_apenas_desativa(app, admin_user):
    with app.app_context():
        lj = _loja()
        u = _user()
        it = _item(texto='Vitrine')
        checklist_loja.registrar(lj, 'abertura', u.id, _resp([it]))
        iid = it.id
    c = app.test_client()
    _login(c, 'admin')
    c.post('/checklist/config', data={'acao': 'excluir', 'item_id': iid})
    with app.app_context():
        it = db.session.get(ChecklistItemModelo, iid)
        assert it is not None and it.ativo is False
        # resposta histórica intacta
        assert ChecklistResposta.query.filter_by(item_id=iid).count() == 1


def test_excluir_item_sem_uso_apaga(app, admin_user):
    with app.app_context():
        it = _item(texto='Nunca usado')
        iid = it.id
    c = app.test_client()
    _login(c, 'admin')
    c.post('/checklist/config', data={'acao': 'excluir', 'item_id': iid})
    with app.app_context():
        assert db.session.get(ChecklistItemModelo, iid) is None


def test_conferencia_mostra_preenchimento_e_faltas(app, admin_user):
    with app.app_context():
        lj = _loja()
        u = _user()
        it = _item(texto='Vitrine montada')
        checklist_loja.registrar(lj, 'abertura', u.id, _resp([it]))
        _loja('Loja Devendo')          # funciona hoje, item global, sem preencher
    c = app.test_client()
    _login(c, 'admin')
    html = c.get('/checklist/conferencia').get_data(as_text=True)
    assert 'Vitrine montada' in html
    assert 'Loja Devendo' in html      # no bloco de faltas de hoje


# ── pendência na home ───────────────────────────────────────────────

def _tarde(monkeypatch):
    """Trava o relógio DEPOIS da hora de cobrança da abertura."""
    from app.utils import agora as _agora
    alvo = _agora().replace(hour=14, minute=0)
    monkeypatch.setattr(checklist_loja, 'agora', lambda: alvo)


def test_pendencia_abertura_apos_a_hora(app, monkeypatch):
    with app.app_context():
        _loja()
        _item(tipo='abertura')
        _tarde(monkeypatch)
        pend = checklist_loja.pendencias_checklist()
    assert any(p['chave'] == 'checklist_abertura' and p['qtd'] == 1
               for p in pend)


def test_pendencia_abertura_nao_grita_de_madrugada(app, monkeypatch):
    with app.app_context():
        _loja()
        _item(tipo='abertura')
        from app.utils import agora as _agora
        cedo = _agora().replace(hour=7, minute=0)
        monkeypatch.setattr(checklist_loja, 'agora', lambda: cedo)
        pend = checklist_loja.pendencias_checklist()
    assert not any(p['chave'] == 'checklist_abertura' for p in pend)


def test_pendencia_respeita_dias_de_funcionamento(app, monkeypatch):
    """Cantina só sáb/dom: em dia que ela não abre, não é cobrada."""
    with app.app_context():
        dow = hoje().weekday()
        fechado_hoje = ''.join(str(d) for d in range(7) if d != dow)
        _loja('Cantina', dias=fechado_hoje)
        _item(tipo='abertura')
        _tarde(monkeypatch)
        pend = checklist_loja.pendencias_checklist()
    assert not any(p['chave'] == 'checklist_abertura' for p in pend)


def test_sem_item_cadastrado_nao_cobra(app, monkeypatch):
    """Feature sem configuração não cobra ninguém."""
    with app.app_context():
        _loja()
        _tarde(monkeypatch)
        pend = checklist_loja.pendencias_checklist()
    assert pend == []


def test_preenchido_some_da_pendencia(app, monkeypatch):
    with app.app_context():
        lj = _loja()
        u = _user()
        it = _item(tipo='abertura')
        checklist_loja.registrar(lj, 'abertura', u.id, _resp([it]))
        _tarde(monkeypatch)
        pend = checklist_loja.pendencias_checklist()
    assert not any(p['chave'] == 'checklist_abertura' for p in pend)


def test_pendencia_fechamento_de_ontem(app):
    with app.app_context():
        lj = _loja()
        u = _user()
        it = _item(tipo='fechamento', texto='Caixa fechado')
        # O item precisa EXISTIR ontem pra ontem ser cobrado (item criado
        # hoje não acusa retroativo — ver teste do fix da revisão).
        from datetime import datetime as _dt
        it.criado_em = _dt.combine(hoje() - timedelta(days=2), _time(12, 0))
        _loja('Loja B')
        # Loja A fechou ontem; Loja B não.
        p = checklist_loja.registrar(lj, 'fechamento', u.id, _resp([it]))
        p.data = hoje() - timedelta(days=1)
        db.session.commit()
        pend = checklist_loja.pendencias_checklist()
    item = next(p for p in pend if p['chave'] == 'checklist_fechamento')
    assert item['qtd'] == 1
    assert 'Loja B' in item['rotulo']


def test_pendencia_entra_no_briefing_do_dono(app, monkeypatch):
    from app.services import briefing_dono
    with app.app_context():
        _loja()
        _item(tipo='abertura')
        _tarde(monkeypatch)
        chaves = {p['chave'] for p in briefing_dono.pendencias()}
    assert 'checklist_abertura' in chaves


def test_industria_nunca_e_cobrada(app, monkeypatch):
    with app.app_context():
        _loja('Industria')
        _item(tipo='abertura')
        _tarde(monkeypatch)
        pend = checklist_loja.pendencias_checklist()
    assert pend == []


def test_hora_de_cobranca_documentada():
    assert checklist_loja.HORA_COBRA_ABERTURA == _time(10, 0)


# ── manual de operação ──────────────────────────────────────────────

def test_manual_registra_o_checklist(app, admin_user):
    c = app.test_client()
    _login(c, 'admin')
    html = c.get('/admin/manual').get_data(as_text=True)
    assert 'Checklist do turno' in html
    assert '/checklist/' in html


# ── fixes da revisão (03/08/2026) ───────────────────────────────────

def test_fechamento_de_madrugada_conta_pro_dia_anterior(app, monkeypatch):
    """Turno de segunda fechado à 00:15 de terça é o fechamento de SEGUNDA —
    gravar a data corrente geraria falso "devendo" e calaria a cobrança do
    dia seguinte (mesma classe do problema do padeiro pós-meia-noite)."""
    with app.app_context():
        lj = _loja()
        u = _user()
        it = _item(tipo='fechamento', texto='Loja trancada')
        from app.utils import agora as _agora
        madrugada = _agora().replace(hour=0, minute=15)
        monkeypatch.setattr(checklist_loja, 'agora', lambda: madrugada)
        p = checklist_loja.registrar(lj, 'fechamento', u.id, _resp([it]))
        assert p.data == hoje() - timedelta(days=1)
        # e a pendência de "fechamento de ontem" se cala
        assert checklist_loja.lojas_faltando(
            'fechamento', hoje() - timedelta(days=1)) == []


def test_fechamento_a_noite_conta_pro_dia_corrente(app, monkeypatch):
    with app.app_context():
        lj = _loja()
        u = _user()
        it = _item(tipo='fechamento', texto='Loja trancada')
        from app.utils import agora as _agora
        noite = _agora().replace(hour=21, minute=30)
        monkeypatch.setattr(checklist_loja, 'agora', lambda: noite)
        p = checklist_loja.registrar(lj, 'fechamento', u.id, _resp([it]))
        assert p.data == hoje()


def test_item_criado_hoje_nao_cobra_fechamento_de_ontem(app):
    """Cadastrar o 1º item de fechamento hoje não pode acusar todas as
    lojas de 'fechamento de ontem ausente' retroativo."""
    with app.app_context():
        _loja()
        _item(tipo='fechamento', texto='Novo de hoje')
        assert checklist_loja.lojas_faltando(
            'fechamento', hoje() - timedelta(days=1)) == []
        # mas cobra normalmente o dia de HOJE em diante
        assert checklist_loja.lojas_faltando('fechamento', hoje()) == ['Loja A']


def test_duplo_submit_em_30s_nao_duplica(app):
    with app.app_context():
        lj = _loja()
        _user(login='atend')
        it = _item(texto='Vitrine')
        lid, iid = lj.id, it.id
    c = app.test_client()
    _login(c, 'atend')
    dados = {'loja': lid, 'tipo': 'abertura', f'ok_{iid}': 'ok'}
    c.post('/checklist/preencher', data=dados,
           content_type='multipart/form-data')
    r = c.post('/checklist/preencher', data=dados,
               content_type='multipart/form-data', follow_redirects=True)
    assert 'não gravei em dobro' in r.get_data(as_text=True)
    with app.app_context():
        assert ChecklistPreenchimento.query.count() == 1


def test_item_id_forjado_no_config_nao_da_500(app, admin_user):
    c = app.test_client()
    _login(c, 'admin')
    for acao in ('editar', 'toggle', 'excluir'):
        r = c.post('/checklist/config',
                   data={'acao': acao, 'item_id': 'abc'},
                   follow_redirects=True)
        assert r.status_code == 200


def test_loja_invalida_no_novo_item_nao_vira_global(app, admin_user):
    """Loja desativada/forjada no cadastro: criar como 'todas as lojas' em
    silêncio cobraria a padaria inteira — recusa com aviso."""
    with app.app_context():
        lj = Loja(nome='Desativada', ativa=False)
        db.session.add(lj)
        db.session.commit()
        lid = lj.id
    c = app.test_client()
    _login(c, 'admin')
    r = c.post('/checklist/config', data={
        'acao': 'novo', 'texto': 'X', 'tipo': 'abertura', 'loja_id': lid,
    }, follow_redirects=True)
    assert 'não está mais disponível' in r.get_data(as_text=True)
    with app.app_context():
        assert ChecklistItemModelo.query.count() == 0


def test_upload_com_erro_de_rede_vira_erro_amigavel(app):
    """ConnectionError do retry do Dropbox não pode escapar como 500 — vira
    ValueError com mensagem legível (fail-close preservado)."""
    import requests as _rq
    with app.app_context():
        lj = _loja()
        u = _user()
        it = _item(texto='Vitrine', exige_foto=True)
        with patch('app.services.dropbox_storage.disponivel',
                   return_value=True), \
             patch('app.utils.comprimir_imagem', side_effect=lambda b: b), \
             patch('app.services.dropbox_storage.upload_publico',
                   side_effect=_rq.exceptions.ConnectionError('rede')):
            with pytest.raises(ValueError, match='Falha ao subir'):
                checklist_loja.registrar(
                    lj, 'abertura', u.id,
                    _resp([it], {it.id: {'ok': True, 'foto': b'jpg'}}))
        assert ChecklistPreenchimento.query.count() == 0
