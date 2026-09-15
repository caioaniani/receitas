/* Resumo visual; publicação e preços continuam validados no servidor. */
(() => {
  const editor = document.getElementById('kit-editor');
  if (!editor) return;
  const rows = [...editor.querySelectorAll('.kit-product')];
  const total = document.getElementById('kit-total');
  const resumo = document.getElementById('kit-resumo-itens');
  const vazio = document.getElementById('kit-resumo-vazio');
  const nome = document.getElementById('kit-nome');
  const busca = document.getElementById('kit-busca');
  const selecionados = document.getElementById('kit-so-selecionados');
  const sucos = [...editor.querySelectorAll('#kit-sucos input[name="suco_ids"]')];
  const fotos = [...editor.querySelectorAll('.kits-foto-select')];
  const grupos = [...editor.querySelectorAll('[data-opcoes-grupo]')].map(container => ({
    container, nome: container.dataset.nome,
    opcoes: [...container.querySelectorAll('input[type="checkbox"]')]
  }));
  const moeda = new Intl.NumberFormat('pt-BR', {style: 'currency', currency: 'BRL'});
  const normalizar = texto => texto.normalize('NFD').replace(/[\u0300-\u036f]/g, '').toLocaleLowerCase('pt-BR').trim();
  function precoEmCentavos(valor) {
    if (valor == null || String(valor).trim() === '') return null;
    const numero = Number(valor);
    return Number.isFinite(numero) && numero > 0 ? Math.round(numero * 100) : null;
  }

  function filtrar() {
    const termo = normalizar(busca.value);
    rows.forEach(row => {
      const qtd = Number(row.querySelector('.kit-quantidade').value) || 0;
      row.hidden = !normalizar(row.dataset.busca).includes(termo) || (selecionados.checked && qtd <= 0);
    });
    const mensagem = document.getElementById('kit-sem-resultado');
    if (mensagem) mensagem.hidden = rows.some(row => !row.hidden);
  }

  function adicionarResumo(fragmento, qtd, descricao) {
    const li = document.createElement('li');
    const quantidade = document.createElement('span');
    quantidade.textContent = qtd + ' ×';
    const texto = document.createElement('div');
    texto.textContent = descricao;
    li.append(quantidade, texto);
    fragmento.append(li);
  }

  function atualizarFotos() {
    const permitidos = new Set([
      ...rows.filter(row => Number(row.querySelector('.kit-quantidade').value) > 0)
        .map(row => row.dataset.kind + ':' + row.dataset.id),
      ...sucos.filter(suco => suco.checked).map(suco => 'produto:' + suco.value),
      ...grupos.flatMap(grupo => grupo.opcoes.filter(opcao => opcao.checked).map(opcao => opcao.value))
    ]);
    const urls = new Set();
    const avisos = [];
    for (const select of fotos) {
      for (const option of select.options) {
        const ocultar = Boolean(option.value) && !permitidos.has(option.value) && !option.selected;
        option.hidden = ocultar;
        option.disabled = ocultar;
      }
      const option = select.selectedOptions[0];
      const url = option?.dataset.fotoUrl || '';
      const erro = !select.value ? '' : !permitidos.has(select.value)
        ? 'Escolha uma foto de um item que faz parte deste plano.'
        : !url ? 'Essa foto não está disponível. Escolha outra ou remova.'
          : urls.has(url) ? 'Escolha fotos diferentes para cada posição.' : '';
      select.setCustomValidity(erro);
      if (erro) avisos.push(erro);
      if (url) urls.add(url);
      const preview = select.closest('.kits-photo-slot').querySelector('.kits-photo-preview');
      const img = preview.querySelector('img');
      img.hidden = !url || Boolean(erro);
      if (url && img.getAttribute('src') !== url) img.src = url;
      preview.querySelector('span').hidden = !img.hidden;
    }
    const aviso = document.getElementById('kit-fotos-aviso');
    if (aviso) {
      aviso.hidden = avisos.length === 0;
      aviso.textContent = [...new Set(avisos)].join(' ');
    }
  }

  function atualizar() {
    let centavos = 0;
    const avisos = [];
    let precoVaria = false;
    const fragmento = document.createDocumentFragment();
    const escolhidos = sucos.filter(suco => suco.checked);
    const escolhasGrupos = grupos.map(grupo => ({...grupo,
      escolhidos: grupo.opcoes.filter(opcao => opcao.checked)}));
    const chavesEscolhidas = [...escolhidos.map(suco => 'produto:' + suco.value),
      ...escolhasGrupos.flatMap(grupo => grupo.escolhidos.map(opcao => opcao.value))];
    const idsEscolhas = new Set(chavesEscolhidas);
    if (idsEscolhas.size !== chavesEscolhidas.length) {
      avisos.push('Um produto foi selecionado em mais de um grupo. Mantenha cada produto em um único grupo.');
    }
    for (const row of rows) {
      const campo = row.querySelector('.kit-quantidade');
      const qtd = Number(campo.value) || 0;
      const adotarPadrao = row.querySelector('input[type="checkbox"]');
      const preco = precoEmCentavos(adotarPadrao?.checked ? row.dataset.precoPadrao : row.dataset.preco);
      if (!campo.validity.valid) avisos.push('Revise as quantidades dos itens fixos.');
      row.classList.toggle('is-selected', qtd > 0);
      row.querySelector('[data-step="-1"]').disabled = qtd <= 0;
      row.querySelector('[data-step="1"]').disabled = qtd >= 999;
      if (qtd <= 0) continue;
      if (preco === null || row.dataset.indisponivel === '1') avisos.push(row.dataset.nome + ': revise a disponibilidade e o preço no catálogo.');
      if (idsEscolhas.has(row.dataset.kind + ':' + row.dataset.id)) {
        avisos.push(row.dataset.nome + ': está nos itens fixos e nas opções. Zere a quantidade fixa ou remova a opção.');
      }
      centavos += (preco || 0) * qtd;
      adicionarResumo(fragmento, qtd, row.dataset.nome);
    }
    const precos = escolhidos.map(suco => precoEmCentavos(suco.dataset.preco));
    if (precos.includes(null)) avisos.push('Revise o preço dos sucos selecionados.');
    if (escolhidos.length === 1 || escolhidos.length > 10) avisos.push('Sucos: selecione de 2 a 10 opções ou deixe todas desmarcadas.');
    if (precos.length) {
      if (!precos.includes(null)) centavos += Math.min(...precos);
      adicionarResumo(fragmento, 1, 'Suco à escolha (' + escolhidos.length + ' opções)');
    }
    precoVaria = new Set(precos).size > 1;
    for (const grupo of escolhasGrupos) {
      const quantas = grupo.escolhidos.length;
      grupo.container.querySelector('.kits-option-count').textContent = quantas + (quantas === 1 ? ' selecionada' : ' selecionadas');
      if (!quantas) continue;
      const valores = grupo.escolhidos.map(opcao => precoEmCentavos(opcao.dataset.preco));
      if (quantas < 2 || quantas > 10) avisos.push(grupo.nome + ': selecione de 2 a 10 opções ou deixe todas desmarcadas.');
      if (valores.includes(null) || grupo.escolhidos.some(opcao => opcao.dataset.indisponivel === '1')) {
        avisos.push(grupo.nome + ': há opções sem preço ou indisponíveis. Revise a seleção.');
      } else {
        centavos += Math.min(...valores);
      }
      precoVaria = precoVaria || new Set(valores).size > 1;
      adicionarResumo(fragmento, 1, grupo.nome + ' à escolha (' + quantas + ' opções)');
    }
    const incompleto = avisos.length > 0;
    const prefixo = precoVaria ? 'A partir de ' : '';
    total.textContent = incompleto ? 'Revise os itens e as opções' : prefixo + moeda.format(centavos / 100);
    total.classList.toggle('is-incomplete', incompleto);
    vazio.hidden = fragmento.childElementCount > 0;
    resumo.replaceChildren(fragmento);
    document.getElementById('kit-sucos-contagem').textContent = escolhidos.length + (escolhidos.length === 1 ? ' selecionada' : ' selecionadas');
    const aviso = document.getElementById('kit-resumo-aviso');
    if (aviso) {
      aviso.hidden = !incompleto;
      aviso.textContent = [...new Set(avisos)].join(' ');
    }
    atualizarFotos();
  }

  for (const row of rows) {
    const campo = row.querySelector('.kit-quantidade');
    row.querySelectorAll('[data-step]').forEach(button => {
      button.hidden = false;
      button.addEventListener('click', () => {
        const atual = Number(campo.value) || 0;
        campo.value = Math.min(999, Math.max(0, Math.trunc(atual) + Number(button.dataset.step)));
        atualizar();
        // Um item removido fica visível até o próximo filtro, preservando o foco.
      });
    });
    campo.addEventListener('input', atualizar);
    row.querySelector('input[type="checkbox"]')?.addEventListener('change', atualizar);
  }
  nome.addEventListener('input', () => {
    document.getElementById('kit-resumo-nome').textContent = nome.value.trim() || 'Seu novo kit';
  });
  busca.addEventListener('input', filtrar);
  selecionados.addEventListener('change', filtrar);
  editor.addEventListener('invalid', event => {
    if (!event.target.matches('.kit-quantidade')) return;
    // A validação nativa precisa conseguir focar a quantidade inválida.
    busca.value = '';
    selecionados.checked = false;
    filtrar();
  }, true);
  sucos.forEach(suco => suco.addEventListener('change', atualizar));
  fotos.forEach(foto => foto.addEventListener('change', atualizarFotos));
  document.getElementById('kit-busca-sucos').addEventListener('input', event => {
    const termo = normalizar(event.target.value);
    const opcoes = [...document.getElementById('kit-sucos').querySelectorAll('.kit-opcao-suco')];
    opcoes.forEach(row => { row.hidden = !normalizar(row.dataset.busca).includes(termo); });
    document.getElementById('kit-sem-sucos').hidden = opcoes.length === 0 || opcoes.some(row => !row.hidden);
  });
  for (const grupo of grupos) {
    grupo.opcoes.forEach(opcao => opcao.addEventListener('change', atualizar));
    grupo.container.querySelector('.kit-busca-grupo').addEventListener('input', event => {
      const termo = normalizar(event.target.value);
      const opcoes = [...grupo.container.querySelectorAll('.kit-opcao-grupo')];
      opcoes.forEach(row => { row.hidden = !normalizar(row.dataset.busca).includes(termo); });
      grupo.container.querySelector('.kit-grupo-vazio').hidden = opcoes.length === 0 || opcoes.some(row => !row.hidden);
    });
  }
  atualizar();
})();
