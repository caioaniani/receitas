"""Editorial filters preserve publication controls and work without accents."""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from app.extensions import db
from app.models import TreinoTrilha, TreinoVideo


def test_admin_preserva_publicacao_por_aula_e_formularios_com_csrf(app, admin_user):
    with app.app_context():
        module = TreinoTrilha(nome='Atendimento', ativa=False)
        db.session.add(module)
        db.session.flush()
        published = TreinoVideo(trilha_id=module.id, titulo='Recepção',
                               ativo=True, video_externo_id='a' * 32,
                               duracao_segundos=90)
        draft = TreinoVideo(trilha_id=module.id, titulo='Café',
                           ativo=False, video_externo_id='b' * 32,
                           duracao_segundos=90)
        empty = TreinoVideo(trilha_id=module.id, titulo='Balcão', ativo=False)
        db.session.add_all([published, draft, empty])
        db.session.commit()
        published_id, draft_id, empty_id = published.id, draft.id, empty.id

    client = app.test_client()
    with client.session_transaction() as session:
        session['_user_id'] = str(admin_user.id)
        session['_fresh'] = True
    response = client.get('/treino/admin/?v2=1')
    assert response.status_code == 200
    html = response.get_data(as_text=True).split('<main class="treino-page treino-admin">')[1]
    assert 'Publicada · módulo ainda oculto' in html
    assert 'data-lesson-status="publicada"' in html
    assert 'data-lesson-status="rascunho"' in html
    assert 'data-lesson-status="sem-video"' in html
    forms = re.findall(r'<form\b[^>]*>.*?</form>', html, re.S)
    for form in forms:
        assert 'name="csrf_token"' in form
    published_form = next(f for f in forms if f'action="/treino/admin/video/{published_id}/toggle"' in f)
    draft_form = next(f for f in forms if f'action="/treino/admin/video/{draft_id}/toggle"' in f)
    assert 'name="acao" value="ocultar"' in published_form
    assert 'name="acao" value="publicar"' in draft_form
    assert f'action="/treino/admin/video/{empty_id}/toggle"' not in html
    assert f'href="/treino/admin/video/{empty_id}"' in html
    assert re.search(r'<details class="treino-details treino-admin-module-settings">', html)
    assert 'class="treino-metrics"' not in html


def test_busca_combina_nome_e_status_e_restaura_modulos():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js não disponível')
    script = Path(__file__).resolve().parents[1] / 'app/static/js/treino-admin.js'
    harness = r"""
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
function element(data = {}) {
  return {dataset: data, hidden: false, open: false, value: '', listeners: {},
    addEventListener(event, fn) { this.listeners[event] = fn; },
    fire(event) { this.listeners[event](); },
    focus() { this.focused = true; }};
}
function lesson(name, status, processing = 'false') {
  return element({lessonName: name, lessonStatus: status, processing});
}
const coffee = lesson('Preparação do café', 'publicada');
const welcome = lesson('Recepção', 'rascunho');
const bread = lesson('Fermentação', 'rascunho', 'true');
const upload = lesson('Balcão', 'sem-video');
function module(name, lessons) {
  const value = element({moduleName: name});
  value.tagName = 'DETAILS';
  value.querySelectorAll = () => lessons;
  return value;
}
const service = module('Atendimento', [coffee, welcome, upload]);
const production = module('Produção', [bread]);
const emptyModule = module('Higiene', []);
const modules = [service, production, emptyModule];
const search = element(), status = element(), results = element();
const noResults = element(), clear = element(), toolbar = element();
status.value = 'todos'; toolbar.hidden = true;
const selectors = {'#treino-conteudo-busca': search, '#treino-conteudo-status': status,
  '[data-admin-results]': results, '[data-admin-no-results]': noResults,
  '[data-admin-clear]': clear, '[data-admin-filters]': toolbar};
const page = {querySelector: key => selectors[key],
  querySelectorAll: key => key === '[data-admin-module]' ? modules : [],
  contains: value => modules.includes(value)};
const window = {location: {hash: '#t2'}, addEventListener() {}};
vm.runInNewContext(fs.readFileSync(process.argv[1], 'utf8'), {
  document: {querySelector: () => page, getElementById: id => id === 't2' ? production : null},
  window
});
assert.equal(toolbar.hidden, false);
assert.equal(production.open, true, 'A URL reabre o módulo de destino');
assert.equal(service.open, false);
search.value = 'CAFE'; search.fire('input');
assert.equal(coffee.hidden, false, 'Busca ignora acentos e maiúsculas');
assert.equal(welcome.hidden, true);
assert.equal(production.hidden, true);
assert.equal(service.open, true);
assert.match(results.textContent, /1 módulo · 1 aula encontrada/);
status.value = 'rascunho'; status.fire('change');
assert.equal(noResults.hidden, false, 'Status e nome devem coincidir');
search.value = 'atendimento'; search.fire('input');
assert.equal(welcome.hidden, false, 'O nome do módulo encontra suas aulas');
assert.equal(coffee.hidden, true);
search.value = ''; status.value = 'processando'; status.fire('change');
assert.equal(bread.hidden, false);
assert.equal(service.hidden, true);
status.value = 'sem-video'; status.fire('change');
assert.equal(upload.hidden, false);
assert.equal(production.hidden, true);
clear.fire('click');
assert.equal(service.hidden, false);
assert.equal(production.hidden, false);
assert.equal(emptyModule.hidden, false);
assert.equal(service.open, false, 'Limpar restaura a abertura anterior à busca');
assert.equal(production.open, true);
assert.equal(results.hidden, true);
assert.equal(noResults.hidden, true);
assert.equal(search.focused, true);
search.value = 'higiene'; search.fire('input');
assert.equal(emptyModule.hidden, false, 'Módulos vazios também são encontrados');
assert.match(results.textContent, /1 módulo · 0 aulas encontradas/);
"""
    result = subprocess.run([node, '-e', harness, str(script)], text=True,
                            capture_output=True, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
