"""Regressões executáveis do envio e recuperação do checklist, sem navegador/rede."""

import shutil
import subprocess
from pathlib import Path

import pytest

_HARNESS = r'''
const vm = require('node:vm'), fs = require('node:fs'), assert = require('node:assert/strict');
const scenario = process.argv[2], fields = {}, nodes = {}, cards = [], requests = [], windowEvents = {}, documentEvents = {};
let focused = null, storageWrites = 0, bitmapClosed = 0, imageFallbacks = 0, imageReleased = 0;
const stored = new Map(), KEY = 'checklist:12:4:abertura:2026-09-15';
function element(extra = {}) {
  return {value:'', type:'text', name:'', id:'', textContent:'', disabled:false, hidden:false, required:false,
    checked:false, files:[], attrs:{}, handlers:{}, dataset:{},
    addEventListener(name, fn) {(this.handlers[name] ||= []).push(fn);},
    fire(name, event = {}) {event.target ||= this; return Promise.all((this.handlers[name] || []).map(fn => fn(event)));},
    setAttribute(name, value) {this.attrs[name] = value;}, removeAttribute(name) {delete this.attrs[name];},
    setCustomValidity(message) {this.customError = message;},
    get willValidate() {return !this.disabled && this.type !== 'hidden';},
    checkValidity() {
      if (!this.willValidate) return true;
      if (this.customError) return false;
      if (!this.required) return true;
      if (this.type === 'file') return this.files.length > 0;
      if (this.type === 'radio') return Object.values(fields).some(field => field.name === this.name && field.checked);
      return !!this.value;
    },
    reportValidity() {this.reported = true; return this.checkValidity();},
    closest() {return this.card || null;}, scrollIntoView() {this.scrolled = true;}, focus() {focused = this;},
    ...extra};
}
function field(name, extra = {}) {
  const result = element({name, ...extra}); fields[extra.key || name] = result; return result;
}
field('csrf_token', {type:'hidden', value:'csrf-secreto-nao-gravar'});
field('envio_token', {type:'hidden', value:'a'.repeat(32)});
field('loja', {type:'hidden', value:'4'}); field('tipo', {type:'hidden', value:'abertura'});
field('revisao_itens', {type:'hidden', value:'1'}); field('observacao', {value:'Tudo conferido'});
function addCard(id, version = '1') {
  const ok = field('ok_' + id, {type:'radio', value:'ok', required:true, checked:true});
  const problem = field('ok_' + id, {key:'pb_' + id, type:'radio', value:'problema'});
  const obs = field('obs_' + id), photo = field('foto_' + id, {type:'file', files:[new Blob(['foto'], {type:'image/jpeg'})]});
  photo.files[0].name = 'original.jpg';
  field('versao_' + id, {type:'hidden', value:version});
  field('itens_presentes_' + id, {type:'hidden', name:'itens_presentes', value:String(id)});
  const card = element({id:'checklist-item-' + id});
  card.controls = [ok, problem, obs, photo];
  card.controls.forEach(c => c.card = card);
  card.querySelector = selector => selector.includes(':checked') ? [ok, problem].find(c => c.checked) || null : ok;
  card.querySelectorAll = selector => selector.includes('aria-invalid')
    ? card.controls.filter(c => c.attrs['aria-invalid'] === 'true') : [ok, problem];
  cards.push(card); return card;
}
addCard(7); addCard(9);
const form = element({action:'https://gestao.example/checklist/preencher', dataset:{
  rascunhoChave:KEY, statusUrl:'/checklist/envio-status', limiteBytes:String(25 * 1024 * 1024)}});
form.elements = {namedItem:name => fields[name], [Symbol.iterator]:function* () {yield* Object.values(fields);}};
form.querySelectorAll = selector => selector === '[data-checklist-item]' ? cards
  : selector === 'input[type="file"]' ? Object.values(fields).filter(f => f.type === 'file')
  : [...Object.values(fields), nodes['btn-fechar'], nodes['adicionar-ponto']];
form.querySelector = () => Object.values(fields).find(f => f.type === 'radio' && f.checked);
form.checkValidity = () => Object.values(fields).every(f => f.checkValidity());
nodes['form-checklist'] = form;
for (const id of ['btn-fechar', 'adicionar-ponto', 'checklist-envio-erro', 'checklist-envio-status',
  'checklist-envio-progresso', 'checklist-rascunho', 'checklist-enviado', 'checklist-confirmacao-texto', 'checklist-comprovante']) {
  nodes[id] = element({id, hidden:id !== 'btn-fechar' && id !== 'adicionar-ponto'});
}
nodes['btn-fechar'].textContent = 'Fechar checklist';
nodes['adicionar-ponto'].disabled = true; // A restauração não pode liberar controle previamente bloqueado.
class FakeFormData {
  constructor() {
    this.data = new Map();
    for (const f of Object.values(fields)) {
      if (f.disabled || (f.type === 'radio' && !f.checked)) continue;
      if (f.type === 'file') {if (f.files.length) this.data.set(f.name, f.files[0]);}
      else this.data.set(f.name, f.value);
    }
  }
  get(name) {return this.data.get(name);}
  set(name, value, filename) {this.data.set(name, value); if (filename) (this.filenames ||= {})[name] = filename;}
  entries() {return this.data.entries();}
}
class FakeXHR {
  constructor() {this.upload = {}; this.headers = {}; this.responseHeaders = {'Content-Type':'application/json'};}
  open(method, url) {this.method = method; this.url = url;}
  setRequestHeader(key, value) {this.headers[key] = value;}
  getResponseHeader(key) {return this.responseHeaders[key];}
  send(payload) {this.payload = payload; requests.push(this);}
  abort() {return this.onabort?.();}
  respond(code, body, contentType = 'application/json') {
    this.status = code; this.responseHeaders['Content-Type'] = contentType;
    this.responseText = typeof body === 'string' ? body : JSON.stringify(body); return this.onload();
  }
}
const receipt = {ok:true, preenchimento_id:12, mensagem:'Checklist salvo em 15/09 às 11h29.',
  url_confirmacao:'/checklist/conferencia?loja=4&preenchimento=12'};
function draft(extra = {}) {
  return {v:1, atualizado:Date.now(), respostas:{7:{versao:'1', ok:'problema', obs:'Porta solta', tinhaFoto:true},
    9:{versao:'1', ok:'ok', obs:'Conferido', tinhaFoto:false}}, observacao:'Rascunho salvo', envio_token:'b'.repeat(32), incerto:false, ...extra};
}
if (['restore', 'versions', 'uncertain', 'saved_restore', 'expiry', 'bfcache_lookup'].includes(scenario)) {
  stored.set(KEY, JSON.stringify(draft({incerto:scenario === 'uncertain',
    ...(scenario === 'expiry' ? {atualizado:Date.now() - 13 * 60 * 60 * 1000} : {})})));
  fields.foto_7.files = []; fields.foto_9.files = [];
  if (scenario === 'versions') fields.versao_7.value = '2';
}
const fakeImage = class {
  constructor() {this.width = 4000; this.height = 3000; imageFallbacks++;}
  set src(value) {if (value) queueMicrotask(() => scenario === 'decode_error' ? this.onerror() : this.onload()); else imageReleased++;}
};
const fakeReader = class {
  readAsDataURL() {this.result = 'data:image/jpeg;base64,aW1hZ2U='; queueMicrotask(() => this.onload());}
  abort() {}
};
const sandbox = {console, URL, Blob, Image:fakeImage, FileReader:fakeReader, FormData:FakeFormData, XMLHttpRequest:FakeXHR,
  setTimeout, clearTimeout, document:{getElementById:id => nodes[id],
    addEventListener(name, fn) {documentEvents[name] = fn;}, createElement(tag) {
      assert.equal(tag, 'canvas');
      return {getContext:() => ({fillRect() {}, drawImage() {}}),
        toBlob(fn) {fn(new Blob(['compacta'], {type:'image/jpeg'}));}};
    }},
  window:{FormData:FakeFormData, XMLHttpRequest:FakeXHR, location:{href:form.action, origin:'https://gestao.example'},
    addEventListener(name, fn) {windowEvents[name] = fn;},
    createImageBitmap:async () => {
      if (['decode_error','safari_fallback'].includes(scenario)) throw new Error('Safari HEIC');
      return {width:4000, height:3000, close() {bitmapClosed++;}};
    },
    sessionStorage:{getItem:key => {if (scenario === 'storage_blocked') throw new Error('blocked'); return stored.get(key) || null;},
      setItem(key, value) {if (scenario === 'storage_blocked') throw new Error('blocked'); storageWrites++; stored.set(key, value);},
      removeItem:key => stored.delete(key)}}};
vm.runInNewContext(fs.readFileSync(process.argv[1], 'utf8'), sandbox);
const flush = async () => {for (let i = 0; i < 15; i++) await Promise.resolve();};
const last = () => requests.at(-1), postCount = () => requests.filter(r => r.method === 'POST').length;
const button = nodes['btn-fechar'], errorText = () => nodes['checklist-envio-erro'].textContent;
const submit = () => {
  let prevented = false;
  const pending = form.fire('submit', {preventDefault() {prevented = true;}});
  assert.equal(prevented, true); return pending;
};
const change = () => form.fire('change', {target:fields.obs_7});
async function sent() {
  const pending = submit(); await flush();
  assert.equal(last().method, 'POST'); return {pending, xhr:last()};
}
(async () => {
  assert.equal(form.noValidate, true);
  if (scenario === 'validation') {
    fields.ok_7.checked = false;
    await submit(); assert.equal(postCount(), 0); assert.match(errorText(), /Ponto 1: marque/); assert.equal(focused, fields.ok_7);
    fields.pb_7.checked = true; fields.obs_7.value = '   ';
    await submit(); assert.match(errorText(), /descreva/); assert.equal(fields.obs_7.required, true);
    fields.obs_7.value = 'Porta solta'; fields.foto_7.required = true; fields.foto_7.files = [];
    await submit(); assert.match(errorText(), /foto obrigatória/); assert.equal(focused, fields.foto_7);
    fields.ok_7.checked = true; fields.pb_7.checked = false; await change(); assert.equal(fields.obs_7.required, false);
  } else if (scenario === 'success') {
    const {pending, xhr} = await sent();
    assert.equal(button.disabled, true); assert.equal(fields.ok_7.disabled, true);
    assert.equal(xhr.payload.get('ok_7'), 'ok', 'Snapshot inclui campos antes do bloqueio');
    assert.equal(xhr.headers['X-CSRFToken'], 'csrf-secreto-nao-gravar');
    assert.equal(xhr.headers.Accept, 'application/json');
    xhr.upload.onprogress({lengthComputable:true, total:100, loaded:100});
    assert.match(nodes['checklist-envio-status'].textContent, /Aguardando confirmação/);
    await submit(); assert.equal(postCount(), 1, 'Duplo clique não repete POST');
    xhr.respond(200, receipt); await pending;
    assert.equal(form.hidden, true); assert.equal(nodes['checklist-enviado'].hidden, false);
    assert.match(nodes['checklist-confirmacao-texto'].textContent, /15\/09 às 11h29/);
    assert.equal(stored.has(KEY), false); assert.equal(nodes['adicionar-ponto'].disabled, true);
    let warning = false; windowEvents.beforeunload({preventDefault() {warning = true;}}); assert.equal(warning, false);
  } else if (scenario === 'network' || scenario === 'timeout') {
    const original = fields.foto_7.files[0]; const {pending, xhr} = await sent();
    (scenario === 'timeout' ? xhr.ontimeout : xhr.onerror)(); await flush();
    assert.equal(last().method, 'GET'); assert.match(last().url, /token=aaaa/);
    last().respond(200, {ok:true, salvo:false}); await pending;
    assert.equal(button.disabled, false); assert.equal(fields.ok_7.disabled, false);
    assert.equal(fields.foto_7.files[0], original); assert.equal(fields.observacao.value, 'Tudo conferido');
    assert.match(errorText(), /Tente novamente/); assert.equal(postCount(), 1);
    assert.equal(nodes['adicionar-ponto'].disabled, true);
  } else if (scenario === 'session') {
    const {pending, xhr} = await sent(); xhr.respond(200, '<html>Entrar</html>', 'text/html'); await pending;
    assert.equal(form.hidden, false); assert.equal(button.disabled, false); assert.match(errorText(), /outra aba/);
    assert.match(errorText(), /recarregue/); assert.match(errorText(), /fotos.*novo/);
    await submit(); assert.equal(postCount(), 1, 'Sessão expirada exige recarregar; não repete POST com token antigo');
    assert.equal(form.hidden, false);
  } else if (scenario === 'csrf') {
    const {pending, xhr} = await sent(); xhr.respond(400, {ok:false, erro:'csrf_expirada',
      mensagem:'Sua sessão expirou. Entre novamente e recarregue a página; anexe as fotos de novo.'}); await pending;
    assert.equal(form.hidden, false); assert.match(errorText(), /recarregue/); assert.match(errorText(), /fotos/);
    assert.equal(button.disabled, false);
    await submit(); assert.equal(postCount(), 1); assert.equal(JSON.parse(stored.get(KEY)).respostas['7'].ok, 'ok');
  } else if (scenario === 'http_error') {
    const {pending, xhr} = await sent(); xhr.respond(422, {ok:false, erro:'validacao', mensagem:'Informe a observação.'}); await pending;
    assert.match(errorText(), /Informe a observação/); assert.equal(button.disabled, false); assert.equal(form.hidden, false);
    assert.equal(fields.ok_7.checked, true); assert.equal(fields.foto_7.files.length, 1);
  } else if (scenario === 'conflict') {
    const {pending, xhr} = await sent(); xhr.respond(409, {ok:false, erro:'checklist_alterado'}); await pending;
    assert.match(errorText(), /pontos.*atualizados/); assert.match(errorText(), /selecionar as fotos novamente/);
    await submit(); assert.equal(postCount(), 1, 'Checklist desatualizado exige recarregar, não POST repetido');
  } else if (scenario === 'oversize') {
    const original = new Blob([new Uint8Array(26 * 1024 * 1024)], {type:'application/octet-stream'});
    original.name = 'foto.heic'; fields.foto_7.files = [original];
    await submit(); assert.equal(postCount(), 0); assert.equal(button.disabled, false);
    assert.match(errorText(), /25 MB/); assert.equal(fields.foto_7.files[0], original);
  } else if (['compression','decode_error','safari_fallback'].includes(scenario)) {
    const original = new Blob([new Uint8Array(1024 * 1024)], {type:'image/heic'});
    original.name = 'original.heic'; fields.foto_7.files = [original];
    const {pending, xhr} = await sent();
    assert.equal(fields.foto_7.files[0], original, 'Não altera FileList nem perde a foto original');
    if (scenario === 'decode_error') assert.equal(xhr.payload.get('foto_7'), original);
    else {assert.equal(xhr.payload.get('foto_7').type, 'image/jpeg'); assert.equal(xhr.payload.filenames.foto_7, 'original.jpg');}
    if (scenario === 'compression') assert.equal(bitmapClosed, 1);
    else {assert.equal(imageFallbacks, 1); assert.equal(imageReleased, 1);}
    xhr.respond(200, receipt); await pending;
  } else if (scenario === 'restore' || scenario === 'versions') {
    await flush(); assert.equal(last().method, 'GET'); assert.equal(fields.envio_token.value, 'b'.repeat(32));
    assert.equal(fields.csrf_token.value, 'csrf-secreto-nao-gravar');
    assert.equal(fields.observacao.value, 'Rascunho salvo');
    if (scenario === 'restore') {assert.equal(fields.pb_7.checked, true); assert.equal(fields.obs_7.value, 'Porta solta');}
    else {assert.equal(fields.ok_7.checked, true); assert.equal(fields.obs_7.value, '');}
    last().respond(200, {ok:true, salvo:false}); await flush();
    assert.match(nodes['checklist-rascunho'].textContent, scenario === 'restore' ? /fotos/ : /pontos.*alterados/);
    assert.equal(postCount(), 0);
    await change(); const raw = stored.get(KEY);
    assert.equal(raw.includes('csrf-secreto'), false); assert.equal(raw.includes('senha'), false);
    assert.equal(JSON.parse(raw).respostas['7'].tinhaFoto, scenario === 'restore');
  } else if (scenario === 'uncertain') {
    await flush(); last().onerror(); await flush();
    const retry = submit(); await flush(); assert.equal(last().method, 'GET'); assert.equal(postCount(), 0);
    last().respond(200, {ok:true, salvo:false}); await flush();
    assert.equal(last().method, 'POST'); last().respond(200, receipt); await retry;
    assert.equal(postCount(), 1);
  } else if (scenario === 'saved_restore') {
    await flush(); last().respond(200, receipt); await flush();
    assert.equal(form.hidden, true); assert.equal(stored.has(KEY), false); assert.equal(postCount(), 0);
  } else if (scenario === 'expiry') {
    await flush(); assert.equal(requests.length, 0); assert.equal(fields.envio_token.value, 'a'.repeat(32));
    assert.equal(fields.observacao.value, 'Tudo conferido');
  } else if (scenario === 'bfcache') {
    const {pending, xhr} = await sent();
    windowEvents.pageshow({persisted:true}); await flush(); assert.equal(last().method, 'GET');
    last().respond(200, {ok:true, salvo:false}); await flush();
    assert.equal(button.disabled, false); assert.equal(fields.ok_7.disabled, false); assert.equal(postCount(), 1);
    await pending; assert.equal(form.hidden, false);
    xhr.respond(200, receipt); await flush(); assert.equal(form.hidden, false, 'Callback de requisição antiga é ignorado');
  } else if (scenario === 'bfcache_lookup') {
    await flush(); const oldLookup = last();
    windowEvents.pageshow({persisted:true}); await flush(); const latestLookup = last();
    assert.notEqual(latestLookup, oldLookup);
    latestLookup.respond(200, {ok:true, salvo:false}); await flush();
    const {pending, xhr} = await sent();
    oldLookup.respond(200, receipt); await flush();
    assert.equal(form.hidden, false, 'GET antigo não confirma recibo depois de novo envio');
    assert.equal(button.disabled, true, 'GET antigo não desbloqueia envio ativo');
    assert.equal(fields.ok_7.disabled, true);
    xhr.respond(200, receipt); await pending;
  } else if (scenario === 'storage_blocked') {
    await change(); assert.match(nodes['checklist-rascunho'].textContent, /não permitiu/);
    const {pending, xhr} = await sent(); xhr.respond(200, receipt); await pending; assert.equal(form.hidden, true);
  } else if (scenario === 'dynamic') {
    fields.pb_7.checked = true; fields.ok_7.checked = false; await change();
    assert.equal(fields.obs_7.required, true);
    cards.splice(0, 1); for (const name of ['ok_7','pb_7','obs_7','foto_7','versao_7','itens_presentes_7']) delete fields[name];
    addCard(20, '4'); fields.ok_20.checked = false;
    documentEvents['checklist:itens-alterados']({});
    await submit(); assert.match(errorText(), /Ponto 2: marque/); assert.equal(focused, fields.ok_20);
    const data = JSON.parse(stored.get(KEY)); assert.equal(data.respostas['7'], undefined); assert.equal(data.respostas['20'].versao, '4');
  } else if (scenario === 'unsafe_receipt') {
    const {pending, xhr} = await sent(); xhr.respond(200, {...receipt, url_confirmacao:'https://evil.invalid/exfil'});
    await flush(); assert.equal(last().method, 'GET');
    last().respond(200, {ok:true, salvo:false}); await pending; assert.equal(form.hidden, false);
    assert.equal(nodes['checklist-comprovante'].href, undefined);
  }
})().then(() => console.log('Cenário concluído')).catch(error => {console.error(error); process.exitCode = 1;});
'''


@pytest.mark.parametrize('cenario', [
    'validation', 'success', 'network', 'timeout', 'session', 'csrf', 'http_error',
    'conflict', 'oversize', 'compression', 'decode_error', 'safari_fallback',
    'restore', 'versions', 'uncertain', 'saved_restore', 'expiry', 'bfcache',
    'bfcache_lookup', 'storage_blocked', 'dynamic', 'unsafe_receipt',
])
def test_envio_checklist_no_navegador(cenario):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js não disponível')
    script = Path(__file__).parents[1] / 'app/static/js/checklist-envio.js'
    result = subprocess.run(
        [node, '-e', _HARNESS, str(script), cenario],
        capture_output=True, text=True, check=False, timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'Cenário concluído' in result.stdout, 'O cenário terminou com uma Promise ainda pendente.'
