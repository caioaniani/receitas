(function () {
  'use strict';
  const source = document.getElementById('kits-config');
  if (!source) return;
  const cfg = JSON.parse(source.textContent);
  const form = document.getElementById('kit-form');
  const agenda = document.getElementById('kit-agenda');
  const template = document.getElementById('kit-dia-template');
  const continuar = document.getElementById('kit-continuar');
  const aviso = document.getElementById('frete-aviso');
  const money = value => (value / 100).toLocaleString('pt-BR', {style: 'currency', currency: 'BRL'});
  const campo = name => form.elements.namedItem(name);
  const suco = document.getElementById('kit-suco');
  const grupos = Array.isArray(cfg.grupos) ? cfg.grupos : [];
  const escolhas = grupos.map(grupo => document.getElementById('kit-escolha-' + grupo));
  const precoKit = document.getElementById('kit-preco');
  const precoInicial = precoKit ? precoKit.textContent : '';
  const errosBox = document.getElementById('kit-erros');
  const errosLista = document.getElementById('kit-erros-lista');
  const textoContinuar = continuar.textContent;
  const avisosCampos = new Map();
  let tentouContinuar = false;
  let errosAgenda = [];
  let sequenciaEntrega = 0;
  let sequenciaAviso = 0;
  let assinaturaPendencias = '';
  let frete = null;
  let distancia = null;
  let versaoEndereco = 0;
  let enviando = false;
  let agendaValida = false;
  const ofertas = new Map((cfg.adicionais || []).map(item => [item.chave, item]));
  const extras = new Map((cfg.adicionaisSelecionados || []).map(item => [item.kind + ':' + item.id, {...item}]));
  const dialogExtras = document.getElementById('kit-adicionais-dialog');
  const abrirExtras = document.getElementById('kit-adicional-abrir');
  const quantidadeExtra = document.getElementById('kit-adicional-qtd');
  const listaExtras = document.getElementById('kit-adicionais-lista');
  const avisoExtra = document.getElementById('kit-adicional-aviso');

  function totalExtras() {
    return [...extras.entries()].reduce((total, [chave, item]) =>
      total + (ofertas.get(chave)?.precoCentavos || 0) * item.qtd, 0);
  }
  function salvarExtras() {
    const input = document.getElementById('adicionais-json');
    if (input) input.value = JSON.stringify([...extras.values()]);
  }
  function renderizarExtras() {
    if (!listaExtras) return;
    listaExtras.replaceChildren();
    extras.forEach((item, chave) => {
      const oferta = ofertas.get(chave);
      const row = document.createElement('li');
      const nome = document.createElement('strong');
      nome.textContent = oferta ? oferta.nome : 'Produto indisponível — remova este adicional';
      row.appendChild(nome);
      if (oferta?.descricao) {
        const descricao = document.createElement('span');
        descricao.className = 'kits-inline-note';
        descricao.textContent = 'Composição: ' + oferta.descricao;
        row.appendChild(descricao);
      }
      const controles = document.createElement('div');
      controles.className = 'kits-extra-controls';
      const label = document.createElement('label');
      label.textContent = 'Por kit';
      const qtd = document.createElement('input');
      qtd.type = 'number'; qtd.min = '1'; qtd.max = '999'; qtd.step = '1'; qtd.required = true;
      qtd.inputMode = 'numeric'; qtd.value = item.qtd;
      qtd.id = 'kit-extra-qtd-' + item.kind + '-' + item.id;
      qtd.setAttribute('aria-label', 'Quantidade por kit de ' + (oferta?.nome || 'adicional'));
      qtd.addEventListener('input', () => {
        const n = Number(qtd.value);
        const valido = /^\d+$/.test(qtd.value) && Number.isInteger(n) && n >= 1 && n <= 999;
        qtd.setCustomValidity(valido ? '' : 'Informe uma quantidade inteira entre 1 e 999.');
        if (valido) {
          item.qtd = n;
          valor.textContent = money((oferta?.precoCentavos || 0) * n);
          salvarExtras(); atualizar();
        }
      });
      label.appendChild(qtd); controles.appendChild(label);
      const valor = document.createElement('span');
      valor.textContent = money((oferta?.precoCentavos || 0) * item.qtd);
      controles.appendChild(valor);
      const remover = document.createElement('button');
      remover.type = 'button'; remover.className = 'kit-remover'; remover.textContent = 'Remover';
      remover.setAttribute('aria-label', 'Remover ' + (oferta?.nome || 'adicional'));
      remover.addEventListener('click', () => {
        extras.delete(chave); renderizarExtras(); atualizarEscolhas();
      });
      controles.appendChild(remover); row.appendChild(controles); listaExtras.appendChild(row);
    });
    salvarExtras();
  }

  function diaCurto(valor) { return valor.split('-').reverse().join('/'); }
  function irPara(input) {
    const destino = input && !input.disabled ? input : errosBox;
    destino.focus({preventScroll: true});
    destino.scrollIntoView({behavior: 'auto', block: 'center'});
  }
  function pendencias() {
    const erros = [];
    const incluir = (input, mensagem) => erros.push({campo: input, mensagem});
    const opcoes = [suco, ...escolhas].filter(Boolean);
    const extraIndisponivel = [...extras.keys()].some(chave => !ofertas.has(chave));
    if (extraIndisponivel) incluir(listaExtras, 'Remova o adicional indisponível para continuar. Os outros produtos foram preservados.');
    opcoes.forEach(input => {
      if (!input.value) incluir(input, input === suco ? 'Escolha o suco da sua opção.'
        : 'Escolha o ' + input.getAttribute('data-grupo') + ' da sua opção.');
    });
    if (!configuracao() && !erros.length) {
      incluir(opcoes[0] || continuar, 'Esta combinação não está disponível. Revise os itens do kit.');
    }
    erros.push(...errosAgenda);
    const rotulos = {cep: 'o CEP', logradouro: 'a rua ou avenida', numero: 'o número do endereço',
      bairro: 'o bairro', cidade: 'a cidade', uf: 'o estado (UF)', nome: 'seu nome',
      sobrenome: 'seu sobrenome', email: 'seu e-mail', telefone: 'seu telefone com DDD', cpf: 'seu CPF ou CNPJ'};
    Object.entries(rotulos).forEach(([nome, rotulo]) => {
      const input = campo(nome);
      if (input.validity.valueMissing || !input.value.trim()) incluir(input, 'Informe ' + rotulo + '.');
      else if (!input.validity.valid) incluir(input, nome === 'email'
        ? 'Confira seu e-mail. Use o formato nome@exemplo.com.'
        : nome === 'numero' ? 'Informe apenas números no número do endereço.' : 'Confira ' + rotulo + '.');
    });
    if (frete === null) incluir(document.getElementById('kit-calcular-frete'),
      'Calcule os fretes para o endereço informado antes de continuar.');
    if (!campo('aceite_lgpd').checked) incluir(campo('aceite_lgpd'),
      'Leia e aceite os termos de compra e a política de privacidade para continuar.');
    const camposAgenda = new Set(linhas().flatMap(row =>
      [row.querySelector('.kit-data'), row.querySelector('.kit-janela')]));
    Array.from(form.elements).forEach(input => {
      // Agenda errors are contextual: fix an unavailable day before asking for its time.
      if (!camposAgenda.has(input) && input.willValidate && !input.validity.valid
          && !erros.some(erro => erro.campo === input)) {
        incluir(input, input.validationMessage || 'Confira este campo antes de continuar.');
      }
    });
    return erros;
  }
  function mostrarPendencias() {
    const erros = pendencias();
    const assinatura = JSON.stringify(erros.map(erro => [erro.campo.id, erro.mensagem]));
    // Do not rebuild the live alert on every keystroke while errors are unchanged.
    if (assinatura === assinaturaPendencias) return erros;
    assinaturaPendencias = assinatura;
    avisosCampos.forEach((info, input) => {
      input.removeAttribute('aria-invalid');
      if (info.descricao) input.setAttribute('aria-describedby', info.descricao);
      else input.removeAttribute('aria-describedby');
      info.aviso.hidden = true;
    });
    errosLista.replaceChildren();
    erros.forEach(({campo: input, mensagem}) => {
      let info = avisosCampos.get(input);
      if (!info) {
        const avisoCampo = document.createElement('p');
        avisoCampo.className = 'kit-campo-erro';
        sequenciaAviso += 1;
        avisoCampo.id = 'kit-campo-erro-' + sequenciaAviso;
        // Keep the checkbox label and its links intact.
        const ancora = input.type === 'checkbox' ? input.closest('label') : input;
        ancora.insertAdjacentElement('afterend', avisoCampo);
        info = {aviso: avisoCampo, descricao: input.getAttribute('aria-describedby') || ''};
        avisosCampos.set(input, info);
      }
      info.aviso.textContent = mensagem;
      info.aviso.hidden = false;
      input.setAttribute('aria-invalid', 'true');
      input.setAttribute('aria-describedby', [info.descricao, info.aviso.id].filter(Boolean).join(' '));
      const item = document.createElement('li');
      const link = document.createElement('button');
      link.type = 'button';
      link.textContent = mensagem;
      link.addEventListener('click', () => irPara(input));
      item.appendChild(link);
      errosLista.appendChild(item);
    });
    errosBox.hidden = !erros.length;
    return erros;
  }

  function linhas() { return Array.from(agenda.querySelectorAll('.kit-dia')); }
  function configuracaoBase() {
    const sucoId = suco ? suco.value : '';
    if (cfg.sucos && Object.keys(cfg.sucos).length && !cfg.sucos[sucoId]) return null;
    if (grupos.length) {
      const selecionadas = escolhas.map(escolha => escolha ? escolha.value : '');
      if (selecionadas.some(valor => !valor)) return null;
      const chave = [sucoId, ...selecionadas].join('|');
      return (cfg.combinacoes || {})[chave] || null;
    }
    return suco ? ((cfg.sucos || {})[sucoId] || null) : cfg;
  }
  function configuracao() {
    const atual = configuracaoBase();
    if (!atual) return null;
    let lead = atual.leadDias || 0;
    for (const chave of extras.keys()) {
      if (!ofertas.has(chave)) return null;
      lead = Math.max(lead, ofertas.get(chave).leadDias || 0);
    }
    return {...atual, ...((cfg.calendarios || {})[lead] || {}),
      precoCentavos: atual.precoCentavos + totalExtras()};
  }
  function janelasDisponiveis(atual) {
    return atual ? (distancia !== null && distancia >= cfg.corteKm ? atual.janelasLonge : atual.janelas) : {};
  }
  function horarios(row, escolhido) {
    const atual = configuracao();
    const input = row.querySelector('.kit-data');
    const data = input.value;
    input.min = atual ? atual.dataMin : '';
    input.max = atual ? atual.dataMax : '';
    input.disabled = !atual;
    const select = row.querySelector('.kit-janela');
    select.disabled = !atual;
    const anterior = escolhido || select.value;
    select.replaceChildren();
    const mapa = janelasDisponiveis(atual);
    const opcoes = mapa[data] || [];
    const inicial = new Option(opcoes.length ? 'Escolha o horário' : 'Sem horários nesta data', '');
    select.add(inicial);
    opcoes.forEach(h => select.add(new Option(h, h)));
    select.value = opcoes.includes(anterior) ? anterior : '';
  }
  function atualizar() {
    const atual = configuracao();
    const lista = linhas();
    const dias = lista.map(r => r.querySelector('.kit-data').value);
    const mapa = janelasDisponiveis(atual);
    errosAgenda = [];
    if (atual && !lista.length) errosAgenda.push({campo: document.getElementById('adicionar-data'),
      mensagem: 'Adicione pelo menos uma entrega e escolha o dia e o horário.'});
    agendaValida = lista.length > 0 && Boolean(atual);
    lista.forEach((r, index) => {
      const data = r.querySelector('.kit-data');
      const janela = r.querySelector('.kit-janela');
      const opcoes = mapa[data.value] || [];
      const erroData = data.value && dias.filter(d => d === data.value).length > 1
        ? 'Cada dia corresponde a um kit. Escolha uma data diferente.'
        : data.value && atual && (data.value < atual.dataMin || data.value > atual.dataMax)
          ? 'Escolha uma data entre ' + diaCurto(atual.dataMin) + ' e ' + diaCurto(atual.dataMax) + '.'
          : data.value && atual && !opcoes.length
            ? 'Não há horários disponíveis nesta data para sua opção e região. Escolha outro dia.' : '';
      const erroJanela = janela.value && !opcoes.includes(janela.value)
        ? 'Escolha um horário disponível para esta entrega.' : '';
      data.setCustomValidity(erroData);
      janela.setCustomValidity(erroJanela);
      if (!data.value || erroData || !janela.value || erroJanela) agendaValida = false;
      if (atual) {
        if (!data.value || erroData) errosAgenda.push({campo: data,
          mensagem: 'Entrega ' + (index + 1) + ': ' + (erroData || 'escolha a data de entrega.')});
        else if (!janela.value || erroJanela) errosAgenda.push({campo: janela,
          mensagem: 'Entrega ' + (index + 1) + ' (' + diaCurto(data.value) + '): '
            + (erroJanela || 'escolha o horário de entrega.')});
      }
      data.setAttribute('aria-label', 'Dia da entrega ' + (index + 1));
      janela.setAttribute('aria-label', 'Horário da entrega ' + (index + 1));
      const numero = r.querySelector('.kit-dia-numero');
      if (numero) numero.textContent = String(index + 1).padStart(2, '0');
      const titulo = r.querySelector('.kit-dia-titulo');
      if (titulo && !numero) titulo.textContent = 'Entrega ' + (index + 1);
      const feedback = r.querySelector('.kit-dia-aviso');
      if (feedback) {
        feedback.textContent = erroData || erroJanela;
        feedback.hidden = tentouContinuar || !(erroData || erroJanela);
      }
    });
    const quantidade = lista.length;
    const resumoExtras = document.getElementById('kit-adicionais-total');
    if (resumoExtras) resumoExtras.textContent = extras.size
      ? 'Adicionais: ' + money(totalExtras()) + ' por kit · ' + money(totalExtras() * quantidade) + ' em todas as entregas. Já incluídos no total.'
      : 'Nenhum produto adicional. Seu kit mantém a composição original.';
    const valorProdutos = atual ? atual.precoCentavos * quantidade : null;
    if (precoKit) precoKit.textContent = atual ? money(atual.precoCentavos) + ' por kit' : precoInicial;
    document.getElementById('kits-quantidade').textContent = quantidade + (quantidade === 1 ? ' kit' : ' kits');
    document.getElementById('kits-subtotal').textContent = atual ? money(valorProdutos)
      : grupos.length ? 'Escolha as opções' : 'Escolha o suco';
    document.getElementById('kits-fretes').textContent = frete === null ? 'Informe seu endereço' : money(frete * quantidade);
    document.getElementById('kits-total').textContent = frete === null || !atual ? '—' : money(valorProdutos + frete * quantidade);
    document.getElementById('adicionar-data').disabled = !atual || quantidade >= 31;
    // An incomplete form must still respond to a tap or keyboard submission.
    continuar.disabled = enviando;
    const ajuda = document.getElementById('kit-agenda-ajuda');
    if (ajuda) ajuda.textContent = !atual
      ? (grupos.length ? 'Escolha os itens do kit para liberar os dias.' : 'Escolha o suco para liberar os dias.')
      : 'Uma entrega por data. Escolha o dia e o horário de cada kit.';
    const proximo = document.getElementById('kit-proximo-passo');
    if (proximo) proximo.textContent = !atual
      ? (grupos.length ? 'Escolha os itens do kit para começar.' : 'Escolha seu suco para começar.')
      : quantidade === 0 ? 'Adicione pelo menos uma entrega.'
        : !agendaValida ? 'Escolha uma data e um horário disponíveis para cada entrega.'
          : frete === null ? 'Informe seu endereço e calcule os fretes para continuar.'
            : 'Confira seus dados e aceite os termos para seguir ao pagamento.';
    const resumoDatas = document.getElementById('kits-resumo-datas');
    if (resumoDatas) {
      resumoDatas.replaceChildren();
      lista.forEach((r, index) => {
        const data = r.querySelector('.kit-data').value;
        const janela = r.querySelector('.kit-janela').value;
        const dia = data ? new Date(data + 'T12:00:00') : null;
        const dataFormatada = dia && !Number.isNaN(dia.getTime())
          ? dia.toLocaleDateString('pt-BR', {weekday: 'short', day: 'numeric', month: 'short'})
          : 'Escolha a data';
        const item = document.createElement('li');
        item.textContent = (index + 1) + '. ' + dataFormatada + ' · ' + (janela || 'Escolha o horário');
        resumoDatas.appendChild(item);
      });
    }
    document.getElementById('agenda-json').value = JSON.stringify(lista.map(r => ({
      data: r.querySelector('.kit-data').value, janela: r.querySelector('.kit-janela').value
    })));
    if (tentouContinuar) mostrarPendencias();
  }
  function adicionar(data, janela) {
    if (linhas().length >= 31) return;
    const row = template.content.firstElementChild.cloneNode(true);
    sequenciaEntrega += 1;
    row.querySelector('.kit-data').id = 'kit-data-' + sequenciaEntrega;
    row.querySelector('.kit-janela').id = 'kit-janela-' + sequenciaEntrega;
    row.querySelector('.kit-data').value = data || '';
    row.querySelector('.kit-data').addEventListener('change', () => { horarios(row); atualizar(); });
    row.querySelector('.kit-janela').addEventListener('change', atualizar);
    row.querySelector('.kit-remover').addEventListener('click', () => {
      avisosCampos.delete(row.querySelector('.kit-data'));
      avisosCampos.delete(row.querySelector('.kit-janela'));
      row.remove(); atualizar();
    });
    agenda.appendChild(row);
    horarios(row, janela);
    atualizar();
  }
  document.getElementById('adicionar-data').addEventListener('click', () => {
    const atual = configuracao();
    if (!atual) return;
    const lista = linhas();
    const ultima = lista.length ? lista[lista.length - 1].querySelector('.kit-data').value : '';
    let proxima = '';
    if (ultima) {
      const d = new Date(ultima + 'T12:00:00Z');
      d.setUTCDate(d.getUTCDate() + 7);
      const sugestao = d.toISOString().slice(0, 10);
      if (atual.janelas[sugestao]) proxima = sugestao;
    }
    adicionar(proxima);
    linhas().at(-1).querySelector('.kit-data').focus();
  });
  function atualizarEscolhas() {
    linhas().forEach(r => horarios(r));
    atualizar();
  }
  if (suco) suco.addEventListener('change', atualizarEscolhas);
  escolhas.forEach(escolha => {
    if (escolha) escolha.addEventListener('change', atualizarEscolhas);
  });
  if (dialogExtras && abrirExtras) {
    const cards = Array.from(dialogExtras.querySelectorAll('.kits-product-card'));
    const busca = document.getElementById('kit-adicional-busca');
    const incluir = document.getElementById('kit-adicional-incluir');
    const mensagem = document.getElementById('kit-adicional-dialog-aviso');
    let selecionado = null;
    const normalizar = valor => valor.normalize('NFD').replace(/[\u0300-\u036f]/g, '').toLocaleLowerCase('pt-BR');
    function selecionar(chave) {
      selecionado = ofertas.get(chave) || null;
      cards.forEach(card => {
        const ativo = card.dataset.chave === chave;
        card.setAttribute('aria-pressed', String(ativo));
        card.querySelector('.kits-product-action').textContent = ativo ? 'Selecionado' : 'Selecionar';
      });
      document.getElementById('kit-adicional-selecao').textContent = selecionado
        ? selecionado.nome + ' · ' + money(selecionado.precoCentavos) : 'Selecione um produto acima';
      document.getElementById('kit-adicional-descricao').textContent = selecionado?.descricao
        ? 'Composição incluída: ' + selecionado.descricao : '';
      quantidadeExtra.value = '1';
      quantidadeExtra.disabled = !selecionado;
      incluir.disabled = !selecionado;
      mensagem.textContent = '';
    }
    function filtrar() {
      const termos = normalizar(busca.value).trim().split(/\s+/).filter(Boolean);
      cards.forEach(card => {
        const nome = normalizar(ofertas.get(card.dataset.chave)?.nome || '');
        card.hidden = !termos.every(termo => nome.includes(termo));
      });
      document.getElementById('kit-adicional-vazio').hidden = cards.some(card => !card.hidden);
    }
    cards.forEach(card => card.addEventListener('click', () => selecionar(card.dataset.chave)));
    busca.addEventListener('input', filtrar);
    abrirExtras.addEventListener('click', () => {
      selecionar(null);
      busca.value = ''; filtrar();
      // Defer image requests until the customer opens the picker.
      dialogExtras.querySelectorAll('img[data-src]').forEach(img => {
        img.addEventListener('error', () => {
          img.hidden = true;
          img.parentElement.querySelector('.kits-product-no-photo').hidden = false;
        }, {once: true});
        img.src = img.dataset.src;
        img.removeAttribute('data-src');
      });
      dialogExtras.showModal();
      document.body.classList.add('kits-dialog-open');
      busca.focus();
    });
    document.getElementById('kit-adicional-fechar').addEventListener('click', () => dialogExtras.close());
    dialogExtras.addEventListener('keydown', event => {
      // Search inputs can consume Escape to clear their value instead of closing.
      if (event.key === 'Escape') { event.preventDefault(); dialogExtras.close(); }
      if (event.key === 'Tab') {
        const controles = Array.from(dialogExtras.querySelectorAll('button:not([disabled]), input:not([disabled])'))
          .filter(input => !input.closest('[hidden]'));
        const primeiro = controles[0], ultimo = controles.at(-1);
        if ((!event.shiftKey && document.activeElement === ultimo)
            || (event.shiftKey && document.activeElement === primeiro)) {
          event.preventDefault();
          (event.shiftKey ? ultimo : primeiro).focus();
        }
      }
    });
    dialogExtras.addEventListener('close', () => {
      document.body.classList.remove('kits-dialog-open');
      abrirExtras.focus({preventScroll: true});
    });
    dialogExtras.addEventListener('click', event => {
      if (event.target !== dialogExtras) return;
      const rect = dialogExtras.getBoundingClientRect();
      if (event.clientX < rect.left || event.clientX > rect.right
          || event.clientY < rect.top || event.clientY > rect.bottom) dialogExtras.close();
    });
    incluir.addEventListener('click', () => {
      const oferta = selecionado;
      const qtd = Number(quantidadeExtra.value);
      if (!oferta) { mensagem.textContent = 'Escolha um produto para adicionar.'; busca.focus(); return; }
      if (!/^\d+$/.test(quantidadeExtra.value) || !Number.isInteger(qtd) || qtd < 1 || qtd > 999) {
        mensagem.textContent = 'Informe uma quantidade inteira entre 1 e 999.'; quantidadeExtra.focus(); return;
      }
      const anterior = extras.get(oferta.chave);
      if ((!anterior && extras.size >= cfg.maxAdicionais) || (anterior?.qtd || 0) + qtd > 999) {
        mensagem.textContent = 'Limite atingido. Ajuste os produtos e as quantidades já adicionados.'; return;
      }
      extras.set(oferta.chave, {kind: oferta.kind, id: oferta.id, qtd: (anterior?.qtd || 0) + qtd,
        ...(oferta.comp ? {comp: oferta.comp} : {})});
      renderizarExtras(); atualizarEscolhas();
      avisoExtra.textContent = oferta.nome + ' adicionado a cada entrega.';
      dialogExtras.close();
    });
  }
  const enderecoCampos = ['cep', 'logradouro', 'numero', 'complemento', 'bairro', 'cidade', 'uf'];
  function invalidarFrete() {
    versaoEndereco += 1;
    frete = null;
    distancia = null;
    aviso.textContent = 'Calcule os fretes para este endereço.';
    linhas().forEach(r => horarios(r));
    atualizar();
  }
  enderecoCampos.forEach(name => campo(name).addEventListener('input', invalidarFrete));
  campo('cep').addEventListener('blur', async () => {
    const cep = campo('cep').value.replace(/\D/g, '');
    if (cep.length !== 8) return;
    const msg = document.getElementById('cep-aviso');
    msg.textContent = 'Consultando CEP…';
    try {
      const res = await fetch(cfg.cepUrl.replace('00000000', cep));
      const body = await res.json();
      if (campo('cep').value.replace(/\D/g, '') !== cep) return;
      if (!res.ok || !body.ok) throw new Error(body.erro || 'Confira o endereço manualmente.');
      ['logradouro', 'bairro', 'cidade', 'uf'].forEach(name => { campo(name).value = body[name] || ''; });
      msg.textContent = 'Confira o endereço e informe o número e o complemento.';
      invalidarFrete();
    } catch (err) { msg.textContent = err.message || 'Não foi possível consultar. Preencha o endereço.'; }
  });
  document.getElementById('kit-calcular-frete').addEventListener('click', async function () {
    for (const name of enderecoCampos) {
      if (!campo(name).reportValidity()) return;
    }
    const versao = versaoEndereco;
    const botao = this;
    botao.disabled = true;
    aviso.textContent = 'Calculando os fretes…';
    try {
      const endereco = ['logradouro', 'numero', 'bairro', 'cidade', 'uf'].map(n => campo(n).value).join(', ');
      const res = await fetch(cfg.freteUrl, {
        method: 'POST', headers: {'Content-Type': 'application/json',
          'X-CSRFToken': campo('csrf_token').value},
        body: JSON.stringify({endereco: endereco, cep: campo('cep').value})
      });
      const body = await res.json();
      if (versao !== versaoEndereco) return;
      if (!res.ok || !body.ok || body.fora_area || body.valor == null
          || !Number.isFinite(Number(body.valor))) {
        throw new Error(body.erro || 'Este endereço está fora da nossa área de entrega.');
      }
      frete = Math.round(Number(body.valor) * 100);
      if (frete < 0) throw new Error('Não foi possível calcular o frete.');
      distancia = Number(body.distancia_km);
      aviso.textContent = money(frete) + ' por entrega. O resumo soma os fretes de todas as datas.';
      linhas().forEach(r => horarios(r));
    } catch (err) {
      frete = null;
      aviso.textContent = err.message || 'Não foi possível calcular os fretes. Tente novamente.';
    } finally { botao.disabled = false; atualizar(); }
  });
  form.addEventListener('submit', event => {
    if (enviando) { event.preventDefault(); return; }
    tentouContinuar = true;
    atualizar();
    const erros = pendencias();
    if (erros.length) {
      event.preventDefault();
      irPara(erros[0].campo);
      return;
    }
    enviando = true;
    continuar.disabled = true;
    continuar.textContent = 'Preparando sua compra…';
  });
  // Native validation otherwise prevents submit from reaching our error summary.
  // Constraints remain on the fields and are checked explicitly above.
  form.noValidate = true;
  ['input', 'change'].forEach(evento => form.addEventListener(evento, () => {
    if (tentouContinuar && !enviando) atualizar();
  }));
  window.addEventListener('pageshow', event => {
    if (!event.persisted) return;
    enviando = false;
    continuar.textContent = textoContinuar;
    atualizar();
  });
  renderizarExtras();
  (Array.isArray(cfg.agenda) && cfg.agenda.length ? cfg.agenda
    : [{data: suco || grupos.length ? '' : (Object.keys(cfg.janelas || {})[0] || ''), janela: ''}])
    .filter(a => a && typeof a === 'object').forEach(a => adicionar(a.data, a.janela));
  atualizar();
})();
