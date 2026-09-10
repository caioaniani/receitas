(() => {
  'use strict';
  const dialog = document.getElementById('editor-checklist');
  if (!dialog) return;
  const editor = document.getElementById('form-editor-checklist');
  const erro = document.getElementById('editor-erro');
  let ocupado = false;
  function abrir(botao) {
    editor.reset();
    erro.textContent = '';
    const dados = botao ? botao.dataset : {};
    editor.elements.item_id.value = dados.id || '';
    editor.elements.versao.value = dados.versao || '';
    editor.elements.texto.value = dados.texto || '';
    editor.elements.setor.value = dados.setor || '';
    editor.elements.exige_foto.checked = dados.foto === '1';
    document.getElementById('editor-exclusao').hidden = !botao;
    dialog.showModal();
  }
  document.getElementById('adicionar-ponto').addEventListener('click', () => abrir(null));
  document.getElementById('checklist-itens').addEventListener('click', event => {
    const botao = event.target.closest('[data-editar-item]');
    if (botao) abrir(botao);
  });
  document.getElementById('cancelar-editor').addEventListener('click', () => dialog.close());
  dialog.addEventListener('close', () => { editor.elements.senha.value = ''; });
  dialog.addEventListener('cancel', event => { if (ocupado) event.preventDefault(); });
  async function salvar(acao) {
    if (ocupado) return;
    if (acao !== 'excluir' && !editor.reportValidity()) return;
    if (acao === 'excluir' && !editor.elements.senha.value) {
      erro.textContent = 'Digite a senha da sua própria conta para excluir.';
      editor.elements.senha.focus();
      return;
    }
    ocupado = true;
    erro.textContent = '';
    const payload = new FormData(editor);
    payload.set('acao', acao);
    if (acao !== 'excluir') payload.delete('senha');
    const botoes = editor.querySelectorAll('button');
    botoes.forEach(b => { b.disabled = true; });
    document.getElementById('btn-fechar').disabled = true;
    try {
      const resposta = await fetch(editor.action, { method: 'POST', body: payload });
      if (!(resposta.headers.get('content-type') || '').includes('application/json')) {
        throw new Error('Não foi possível salvar. Confira sua conexão e sessão antes de tentar novamente.');
      }
      const dados = await resposta.json();
      if (!resposta.ok) throw new Error(dados.erro || 'Não foi possível salvar. Tente novamente.');
      const antigo = document.getElementById(`checklist-item-${dados.item_id}`);
      if (dados.html) {
        const template = document.createElement('template');
        template.innerHTML = dados.html;
        if (antigo) antigo.replaceWith(template.content);
        else document.getElementById('checklist-itens').append(template.content);
      } else if (antigo) antigo.remove();
      const lista = document.getElementById('checklist-itens');
      lista.querySelectorAll('[data-setor-titulo]').forEach(titulo => titulo.remove());
      const grupos = new Map();
      lista.querySelectorAll('[data-checklist-item]').forEach(card => {
        const setor = card.dataset.setor;
        if (!grupos.has(setor)) grupos.set(setor, []);
        grupos.get(setor).push(card);
      });
      grupos.forEach((cards, setor) => {
        if (grupos.size > 1) {
          const titulo = document.createElement('h2');
          titulo.dataset.setorTitulo = '';
          titulo.className = 'h6 text-uppercase text-muted mt-3 mb-2';
          titulo.textContent = `${setor} (${cards.length})`;
          lista.append(titulo);
        }
        cards.forEach(card => lista.append(card));
      });
      document.getElementById('checklist-edicao-status').textContent = acao === 'excluir'
        ? 'Ponto excluído desta loja. Histórico preservado.'
        : 'Ponto salvo para esta loja. Preencha o ponto atualizado; as outras respostas foram mantidas.';
      dialog.close();
    } catch (error) {
      erro.textContent = error.message;
    } finally {
      editor.elements.senha.value = '';
      ocupado = false;
      botoes.forEach(b => { b.disabled = false; });
      document.getElementById('btn-fechar').disabled = false;
    }
  }
  editor.addEventListener('submit', event => {
    event.preventDefault();
    salvar(editor.elements.item_id.value ? 'editar' : 'novo');
  });
  document.getElementById('excluir-ponto').addEventListener('click', () => salvar('excluir'));
})();
