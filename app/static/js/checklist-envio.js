(() => {
  'use strict';
  const form = document.getElementById('form-checklist');
  if (!form || !window.FormData || !window.XMLHttpRequest) return;
  const botao = document.getElementById('btn-fechar');
  const erro = document.getElementById('checklist-envio-erro');
  const status = document.getElementById('checklist-envio-status');
  const progresso = document.getElementById('checklist-envio-progresso');
  const rascunho = document.getElementById('checklist-rascunho');
  const enviado = document.getElementById('checklist-enviado');
  const textoBotao = botao.textContent;
  const chave = form.dataset.rascunhoChave;
  const limite = Number(form.dataset.limiteBytes) || 25 * 1024 * 1024;
  const ttl = 12 * 60 * 60 * 1000;
  const fotosPendentes = new Set();
  const campo = nome => form.elements.namedItem(nome);
  const cards = () => Array.from(form.querySelectorAll('[data-checklist-item]'));
  const itens = () => cards().map(card => ({card, id:card.id.replace('checklist-item-', '')}));
  let ocupado = false, salvo = false, alterado = false, incerto = false, estados = [];
  let requisicao = null, revisao = 0, precisaRecarregar = false;
  let rascunhoRecuperado = false, itensAtualizados = false;

  function informarErro(mensagem) {
    erro.textContent = mensagem;
    erro.hidden = !mensagem;
  }

  function focar(elemento) {
    if (!elemento) return;
    const destino = elemento.closest('[data-checklist-item]') || elemento;
    destino.scrollIntoView({block:'center', behavior:'smooth'});
    try { elemento.focus({preventScroll:true}); } catch (_) { elemento.focus(); }
  }

  function ocupar(valor, mensagem) {
    const jaOcupado = ocupado;
    ocupado = valor;
    form.dataset.envioOcupado = valor ? '1' : '0';
    form.setAttribute('aria-busy', valor ? 'true' : 'false');
    if (valor) {
      if (!jaOcupado) {
        estados = Array.from(form.querySelectorAll('input, select, textarea, button'))
          .map(elemento => [elemento, elemento.disabled]);
        estados.forEach(([elemento]) => { elemento.disabled = true; });
      }
      botao.textContent = 'Aguarde…';
    } else {
      estados.forEach(([elemento, disabled]) => { elemento.disabled = disabled; });
      estados = [];
      botao.disabled = false;
      botao.textContent = textoBotao;
      progresso.hidden = true;
    }
    if (mensagem !== undefined) status.textContent = mensagem;
  }

  function mensagemRascunho() {
    if (salvo) return;
    rascunho.textContent = [
      rascunhoRecuperado ? 'Recuperamos as marcações e observações desta aba.' : 'Rascunho guardado nesta aba por até 12 horas.',
      itensAtualizados ? 'Alguns pontos foram alterados; preencha os pontos atualizados novamente.' : '',
      fotosPendentes.size ? 'Selecione novamente as fotos: o navegador não guarda os arquivos após recarregar.' : ''
    ].filter(Boolean).join(' ');
  }

  function guardar() {
    if (!chave || salvo || (!alterado && !incerto)) return;
    const respostas = {};
    itens().forEach(({card, id}) => {
      const marcado = card.querySelector('input[type="radio"]:checked');
      const foto = campo('foto_' + id);
      respostas[id] = {
        versao:campo('versao_' + id)?.value || '',
        ok:marcado?.value || '', obs:campo('obs_' + id)?.value || '',
        tinhaFoto:Boolean(foto?.files?.length || fotosPendentes.has(id))
      };
    });
    try {
      window.sessionStorage.setItem(chave, JSON.stringify({
        v:1, atualizado:Date.now(), respostas, observacao:campo('observacao')?.value || '',
        envio_token:campo('envio_token')?.value || '', incerto
      }));
      mensagemRascunho();
    } catch (_) {
      rascunho.textContent = 'O navegador não permitiu guardar o rascunho. Mantenha esta aba aberta até confirmar o envio.';
    }
  }

  function restaurar() {
    if (!chave) return false;
    try {
      const dado = JSON.parse(window.sessionStorage.getItem(chave) || 'null');
      if (!dado) return false;
      if (dado.v !== 1 || !Number.isFinite(dado.atualizado) || Date.now() - dado.atualizado > ttl
          || dado.atualizado > Date.now() || !dado.respostas || typeof dado.respostas !== 'object') {
        window.sessionStorage.removeItem(chave);
        return false;
      }
      let mudou = false;
      itens().forEach(({card, id}) => {
        const resposta = dado.respostas[id];
        if (!resposta) return;
        if (String(resposta.versao) !== campo('versao_' + id)?.value) { mudou = true; return; }
        card.querySelectorAll('input[type="radio"]').forEach(radio => {
          radio.checked = ['ok', 'problema'].includes(resposta.ok) && radio.value === resposta.ok;
        });
        const obs = campo('obs_' + id);
        if (obs && typeof resposta.obs === 'string') obs.value = resposta.obs.slice(0, 500);
        if (resposta.tinhaFoto && campo('foto_' + id)) fotosPendentes.add(id);
      });
      if (campo('observacao') && typeof dado.observacao === 'string') campo('observacao').value = dado.observacao.slice(0, 500);
      if (/^[a-f0-9]{32}$/i.test(dado.envio_token || '') && campo('envio_token')) campo('envio_token').value = dado.envio_token;
      incerto = Boolean(dado.incerto);
      alterado = true;
      rascunhoRecuperado = true;
      itensAtualizados = mudou;
      mensagemRascunho();
      return true;
    } catch (_) { return false; }
  }

  function validar() {
    let primeiro = null;
    let mensagem = '';
    itens().forEach(({card, id}, indice) => {
      const marcado = card.querySelector('input[type="radio"]:checked');
      const obs = campo('obs_' + id);
      const foto = campo('foto_' + id);
      if (obs) {
        obs.required = marcado?.value === 'problema';
        obs.setCustomValidity(obs.required && !obs.value.trim() ? 'Descreva o problema observado neste ponto.' : '');
      }
      card.querySelectorAll('[aria-invalid="true"]').forEach(el => el.removeAttribute('aria-invalid'));
      let invalido = null, detalhe = '';
      if (!marcado) { invalido = card.querySelector('input[type="radio"]'); detalhe = 'marque OK ou Problema'; }
      else if (obs && !obs.checkValidity()) { invalido = obs; detalhe = 'descreva o problema observado'; }
      else if (foto && foto.required && !foto.files.length) { invalido = foto; detalhe = 'adicione a foto obrigatória'; }
      if (invalido) {
        invalido.setAttribute('aria-invalid', 'true');
        if (!primeiro) { primeiro = invalido; mensagem = `Ponto ${indice + 1}: ${detalhe}. Suas respostas continuam nesta tela.`; }
      }
    });
    if (!primeiro && !form.checkValidity()) {
      primeiro = Array.from(form.elements).find(el => el.willValidate && !el.checkValidity());
      mensagem = 'Confira o campo destacado antes de enviar. Suas respostas continuam nesta tela.';
    }
    if (primeiro) {
      informarErro(mensagem);
      focar(primeiro);
      primeiro.reportValidity();
      return false;
    }
    return true;
  }

  function confirmar(dado) {
    if (dado?.ok !== true || !Number.isInteger(dado.preenchimento_id) || dado.preenchimento_id <= 0) return false;
    let destino;
    try {
      destino = new URL(dado.url_confirmacao, window.location.href);
      if (!dado.url_confirmacao || destino.origin !== window.location.origin || !/^https?:$/.test(destino.protocol)) return false;
    } catch (_) { return false; }
    salvo = true;
    incerto = false;
    alterado = false;
    fotosPendentes.clear();
    try { if (chave) window.sessionStorage.removeItem(chave); } catch (_) { /* O recibo do servidor é a confirmação. */ }
    ocupar(false, 'Checklist salvo.');
    informarErro('');
    form.hidden = true;
    enviado.hidden = false;
    document.getElementById('checklist-confirmacao-texto').textContent = dado.mensagem || 'Checklist salvo com sucesso.';
    document.getElementById('checklist-comprovante').href = destino.href;
    focar(enviado);
    return true;
  }

  function consultarStatus() {
    return new Promise((resolve, reject) => {
      let url;
      try {
        url = new URL(form.dataset.statusUrl, window.location.href);
        if (!form.dataset.statusUrl || url.origin !== window.location.origin) throw new Error('Endereço inválido');
        url.searchParams.set('token', campo('envio_token').value);
        url.searchParams.set('loja', campo('loja').value);
        url.searchParams.set('tipo', campo('tipo').value);
      } catch (_) { reject(new Error('Não foi possível conferir o envio.')); return; }
      const xhr = new XMLHttpRequest();
      xhr.open('GET', url.href, true);
      xhr.timeout = 15000;
      xhr.setRequestHeader('Accept', 'application/json');
      xhr.setRequestHeader('X-Checklist-Request', '1');
      xhr.onload = () => {
        try {
          if (xhr.status !== 200 || !(xhr.getResponseHeader('Content-Type') || '').includes('application/json')) throw new Error();
          const dado = JSON.parse(xhr.responseText);
          if (dado.ok === true && (dado.salvo === false || Number.isInteger(dado.preenchimento_id))) resolve(dado);
          else throw new Error();
        } catch (_) { reject(new Error('Não foi possível conferir se o checklist foi salvo.')); }
      };
      xhr.onerror = xhr.ontimeout = xhr.onabort = () => reject(new Error('Não foi possível conferir a conexão.'));
      xhr.send();
    });
  }

  async function recuperar() {
    const rodada = ++revisao;
    incerto = true;
    ocupar(true, 'Conferindo se o checklist já foi salvo…');
    try {
      const dado = await consultarStatus();
      if (rodada !== revisao) return null;
      if (confirmar(dado)) return true;
      if (dado.ok === true && dado.salvo === false) {
        incerto = false;
        ocupar(false, 'Envio ainda não confirmado. Suas respostas continuam nesta tela.');
        guardar();
        return false;
      }
      throw new Error('Confirmação inválida');
    } catch (_) {
      if (rodada !== revisao) return null;
      incerto = true;
      ocupar(false, 'Não conseguimos confirmar o envio.');
      informarErro('Confira sua conexão e sua sessão. Ao tentar novamente, vamos verificar primeiro se o checklist já foi salvo, sem duplicar o envio.');
    }
    guardar();
    return null;
  }

  function carregarImagem(arquivo) {
    const imagemComDados = () => new Promise((resolve, reject) => {
      // data: já é permitido pela CSP. blob: não é liberado no sistema.
      const leitor = new FileReader(), imagem = new Image();
      const timer = setTimeout(() => {
        leitor.abort(); imagem.src = ''; reject(new Error('Foto demorou para abrir'));
      }, 20000);
      const falhou = () => { clearTimeout(timer); imagem.src = ''; reject(new Error('Foto não abriu')); };
      imagem.onload = () => { clearTimeout(timer); resolve({imagem, liberar:() => { imagem.src = ''; }}); };
      imagem.onerror = falhou;
      leitor.onerror = falhou;
      leitor.onload = () => { imagem.src = leitor.result; };
      leitor.readAsDataURL(arquivo);
    });
    if (typeof window.createImageBitmap !== 'function') return imagemComDados();
    // Safari/HEIC pode recusar ImageBitmap; o decodificador de <img> ainda pode funcionar.
    return new Promise((resolve, reject) => {
      let encerrou = false;
      const timer = setTimeout(() => { encerrou = true; reject(new Error('Foto demorou para abrir')); }, 20000);
      window.createImageBitmap(arquivo).then(imagem => {
        if (encerrou) { imagem.close(); return; }
        clearTimeout(timer); resolve({imagem, liberar:() => imagem.close()});
      }, error => { clearTimeout(timer); reject(error); });
    }).catch(imagemComDados);
  }

  async function compactar(arquivo) {
    if (arquivo.size <= 800 * 1024 || !arquivo.type.startsWith('image/')) return arquivo;
    let carregada;
    try {
      carregada = await carregarImagem(arquivo);
      const imagem = carregada.imagem;
      const largura = imagem.naturalWidth || imagem.width, altura = imagem.naturalHeight || imagem.height;
      if (!largura || !altura) return arquivo;
      const proporcao = Math.min(1, 1600 / Math.max(largura, altura));
      const canvas = document.createElement('canvas');
      canvas.width = Math.max(1, Math.round(largura * proporcao));
      canvas.height = Math.max(1, Math.round(altura * proporcao));
      const contexto = canvas.getContext('2d');
      if (!contexto || typeof canvas.toBlob !== 'function') return arquivo;
      contexto.fillStyle = '#fff';
      contexto.fillRect(0, 0, canvas.width, canvas.height);
      contexto.drawImage(imagem, 0, 0, canvas.width, canvas.height);
      const blob = await new Promise(resolve => {
        const timer = setTimeout(() => resolve(null), 20000);
        canvas.toBlob(resultado => { clearTimeout(timer); resolve(resultado); }, 'image/jpeg', 0.82);
      });
      return blob && blob.size < arquivo.size ? blob : arquivo;
    } catch (_) { return arquivo; }
    finally { if (carregada) carregada.liberar(); }
  }

  async function falhaIncerta(mensagem) {
    informarErro(mensagem);
    const resultado = await recuperar();
    if (resultado === false) informarErro(`${mensagem} Suas respostas e fotos continuam nesta tela. Tente novamente.`);
  }

  function enviar(payload, rodada) {
    return new Promise(resolve => {
      const xhr = new XMLHttpRequest();
      requisicao = xhr;
      xhr.open('POST', form.action, true);
      xhr.timeout = 180000;
      xhr.setRequestHeader('Accept', 'application/json');
      xhr.setRequestHeader('X-Checklist-Request', '1');
      xhr.setRequestHeader('X-CSRFToken', payload.get('csrf_token') || '');
      xhr.upload.onprogress = event => {
        if (rodada !== revisao) return;
        progresso.hidden = false;
        if (event.lengthComputable) {
          progresso.max = event.total; progresso.value = event.loaded;
          status.textContent = event.loaded >= event.total
            ? 'Fotos enviadas. Aguardando confirmação de que o checklist foi salvo…'
            : `Enviando checklist e fotos: ${Math.round(event.loaded / event.total * 100)}%. Mantenha esta tela aberta.`;
        } else status.textContent = 'Enviando checklist e fotos. Mantenha esta tela aberta.';
      };
      const terminar = async (motivo) => {
        if (rodada !== revisao) { resolve(); return; }
        requisicao = null;
        if (motivo) { await falhaIncerta(motivo); resolve(); return; }
        let dado = null;
        const json = (xhr.getResponseHeader('Content-Type') || '').includes('application/json');
        if (json) { try { dado = JSON.parse(xhr.responseText); } catch (_) { /* Nunca interpretar HTML como recibo. */ } }
        if (xhr.status >= 200 && xhr.status < 300 && confirmar(dado)) { resolve(); return; }
        const sessao = xhr.status === 401 || dado?.erro === 'csrf_expirada'
          || dado?.erro === 'sessao_expirada' || (!json && xhr.status !== 413);
        if (sessao) {
          precisaRecarregar = true;
          ocupar(false, 'O envio não foi confirmado.');
          informarErro(typeof dado?.mensagem === 'string' ? dado.mensagem
            : 'Sua sessão pode ter expirado. Entre novamente no sistema em outra aba e recarregue esta página. As fotos precisarão ser anexadas de novo. Suas respostas continuam nesta aba até recarregar.');
        } else if ([400, 403, 409, 413, 422].includes(xhr.status)) {
          incerto = false;
          ocupar(false, 'Checklist não enviado. Corrija o aviso e tente novamente.');
          precisaRecarregar = dado?.erro === 'checklist_alterado';
          informarErro(precisaRecarregar
            ? 'Os pontos deste checklist foram atualizados. Recarregue a página para conferir: vamos recuperar as marcações dos pontos que não mudaram. Será preciso selecionar as fotos novamente.'
            : xhr.status === 413
              ? 'As fotos ficaram grandes demais para o envio. Escolha fotos menores e tente novamente. Suas respostas foram mantidas.'
              : (typeof dado?.mensagem === 'string' ? dado.mensagem : 'Não foi possível enviar. Confira os campos e tente novamente.'));
        } else await falhaIncerta('O servidor não confirmou o envio.');
        guardar();
        resolve();
      };
      xhr.onload = () => terminar();
      xhr.onerror = () => terminar('A conexão caiu durante o envio.');
      xhr.ontimeout = () => terminar('O envio demorou mais do que o esperado.');
      xhr.onabort = () => terminar('O envio foi interrompido.');
      incerto = true;
      guardar();
      status.textContent = 'Enviando checklist e fotos. Mantenha esta tela aberta.';
      xhr.send(payload);
    });
  }

  async function submeter(event) {
    event.preventDefault();
    if (ocupado || salvo) return;
    if (precisaRecarregar) { focar(erro); return; }
    if (incerto) {
      if (await recuperar() !== false) return;
    }
    if (!validar()) return;
    informarErro('');
    alterado = true;
    guardar();
    // Snapshot antes de desabilitar campos: FormData ignora controles disabled.
    const payload = new FormData(form);
    const fotos = Array.from(form.querySelectorAll('input[type="file"]')).flatMap(input =>
      Array.from(input.files || []).map(arquivo => ({nome:input.name, arquivo})));
    const rodada = ++revisao;
    ocupar(true, 'Preparando as fotos para enviar…');
    try {
      for (let indice = 0; indice < fotos.length; indice++) {
        const {nome, arquivo} = fotos[indice];
        status.textContent = `Preparando foto ${indice + 1} de ${fotos.length}…`;
        const foto = await compactar(arquivo);
        if (rodada !== revisao) return;
        payload.set(nome, foto, foto === arquivo ? arquivo.name : `${arquivo.name.replace(/\.[^.]*$/, '') || 'foto'}.jpg`);
      }
      let bytes = 128 * 1024;
      for (const [nome, valor] of payload.entries()) {
        bytes += new Blob([nome]).size + (typeof valor === 'string' ? new Blob([valor]).size : valor.size) + 256;
      }
      if (bytes > limite) {
        ocupar(false, 'As respostas e fotos continuam nesta tela.');
        informarErro(`As fotos ainda estão grandes demais. O limite por envio é ${Math.floor(limite / 1024 / 1024)} MB. Escolha fotos menores e tente novamente; não é preciso preencher tudo de novo.`);
        focar(erro);
        return;
      }
      await enviar(payload, rodada);
    } catch (_) {
      ocupar(false, 'Não foi possível concluir o envio.');
      informarErro('Não foi possível preparar ou enviar o checklist. Suas respostas e fotos continuam nesta tela. Confira a conexão e tente novamente.');
    }
  }

  form.noValidate = true;
  form.addEventListener('submit', submeter);
  function mudou(event) {
    if (ocupado || salvo) return;
    if (event.target?.type === 'file') fotosPendentes.delete(event.target.name.replace('foto_', ''));
    alterado = true;
    // Retira a obrigação de observação assim que Problema é trocado por OK.
    itens().forEach(({card, id}) => {
      const obs = campo('obs_' + id);
      if (obs) {
        obs.required = card.querySelector('input[type="radio"]:checked')?.value === 'problema';
        obs.setCustomValidity(obs.required && !obs.value.trim() ? 'Descreva o problema observado neste ponto.' : '');
      }
    });
    guardar();
  }
  form.addEventListener('input', mudou);
  form.addEventListener('change', mudou);
  document.addEventListener('checklist:itens-alterados', mudou);
  window.addEventListener('beforeunload', event => {
    if (!salvo && (ocupado || alterado)) { guardar(); event.preventDefault(); event.returnValue = ''; }
  });
  window.addEventListener('pageshow', event => {
    if (!event.persisted || salvo) return;
    ++revisao;
    if (requisicao) { const antiga = requisicao; requisicao = null; antiga.abort(); }
    ocupar(false);
    if (incerto) recuperar();
    else status.textContent = 'Você pode continuar o preenchimento. Suas respostas foram mantidas.';
  });
  const recuperado = restaurar();
  // HTML devolvido após falha do POST legado também já pode conter respostas.
  if (!recuperado && form.querySelector('input[type="radio"]:checked')) { alterado = true; guardar(); }
  if (recuperado) recuperar();
})();
