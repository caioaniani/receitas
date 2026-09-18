/* Cadastro fiscal independente do contato do comprador e da entrega. */
(function () {
  'use strict';

  function iniciar() {
    document.querySelectorAll('[data-fiscal-cnpj]').forEach(function (bloco) {
      var form = bloco.closest('form');
      var documento = form && form.querySelector('[name="cpf"]');
      if (!documento) return;
      var campos = bloco.querySelector('fieldset');
      var status = bloco.querySelector('[data-fiscal-status]');
      var botao = bloco.querySelector('[data-fiscal-consultar]');
      var token = form.elements.namedItem('fiscal_token');
      var confirmado = form.elements.namedItem('fiscal_confirmado');
      var nomes = ['nome', 'endereco', 'numero', 'complemento', 'bairro', 'cidade', 'uf', 'cep', 'ie', 'situacao_ie'];
      var alterados = new Set();
      var atual = digitos(documento.value);
      var sequencia = 0;
      var controlador = null;
      var timer = null;
      var ultimoConsultado = '';

      function campo(nome) { return form.elements.namedItem('fiscal_' + nome); }
      function digitos(valor) { return String(valor || '').replace(/\D/g, ''); }
      function avisar(texto) { status.textContent = texto || ''; }
      function origemConsulta(resultado) {
        var fontes = {cnpj_ws: 'CNPJ.ws', cnpj_publico: 'cadastro público de CNPJ'};
        var texto = ' Base consultada: ' + (fontes[resultado.origem] || 'cadastro público de CNPJ') + '.';
        var partes = /^(\d{4})-(\d{2})-(\d{2})(?:T|$)/.exec(String(resultado.atualizado_em || ''));
        if (partes) {
          var ano = Number(partes[1]), mes = Number(partes[2]), dia = Number(partes[3]);
          var data = new Date(Date.UTC(ano, mes - 1, dia));
          if (ano >= 1900 && data.getUTCFullYear() === ano
              && data.getUTCMonth() === mes - 1 && data.getUTCDate() === dia) {
            texto += ' Atualização informada pela fonte: '
              + data.toLocaleDateString('pt-BR', {timeZone: 'UTC'}) + '.';
          }
        }
        return texto + ' Não é uma validação em tempo real da SEFAZ.';
      }
      function notificar() { form.dispatchEvent(new CustomEvent('fiscal:tipo', {bubbles: true})); }
      function atualizarIE() {
        var situacao = campo('situacao_ie').value;
        var exige = situacao === 'contribuinte';
        var permite = exige || situacao === '' || situacao === 'desconhecida';
        campo('ie').required = exige;
        campo('ie').disabled = campos.disabled || !permite;
        bloco.querySelector('[data-fiscal-ie-campo]').hidden = !permite;
      }
      function cancelar() {
        sequencia += 1;
        clearTimeout(timer);
        if (controlador) controlador.abort();
        controlador = null;
        botao.disabled = false;
        bloco.removeAttribute('aria-busy');
      }
      function limpar() {
        token.value = '';
        confirmado.checked = false;
        nomes.forEach(function (nome) { campo(nome).value = ''; });
        alterados.clear();
        ultimoConsultado = '';
        avisar('');
      }
      function atualizarTipo() {
        var proximo = digitos(documento.value);
        if (proximo !== atual) {
          cancelar();
          limpar();
          atual = proximo;
        }
        var pj = atual.length === 14;
        var mudouTipo = campos.disabled === pj;
        bloco.hidden = !pj;
        campos.disabled = !pj;
        atualizarIE();
        if (mudouTipo) notificar();
        if (pj && atual !== ultimoConsultado && !campo('nome').value.trim()) {
          clearTimeout(timer);
          timer = setTimeout(consultar, 450);
        }
      }
      async function consultar() {
        var cnpj = digitos(documento.value);
        if (cnpj.length !== 14) return;
        cancelar();
        var rodada = sequencia;
        controlador = new AbortController();
        var ufInicial = campo('uf').value;
        var erroConsulta = '';
        botao.disabled = true;
        bloco.setAttribute('aria-busy', 'true');
        avisar('Consultando os dados da empresa…');
        try {
          var resposta = await fetch(bloco.dataset.consultaUrl, {
            method: 'POST', credentials: 'same-origin', signal: controlador.signal,
            headers: {'Content-Type': 'application/json',
              'X-CSRFToken': (form.elements.namedItem('csrf_token') || {}).value || ''},
            body: JSON.stringify({cnpj: cnpj})
          });
          var resultado = await resposta.json();
          if (rodada !== sequencia || cnpj !== digitos(documento.value)) return;
          if (!resposta.ok || resultado.erro || !resultado.dados) {
            erroConsulta = typeof resultado.erro === 'string' ? resultado.erro : '';
            throw new Error('consulta indisponivel');
          }
          ultimoConsultado = cnpj;
          if (campo('uf').value !== ufInicial) {
            avisar('Você alterou o estado durante a consulta. Seus dados foram mantidos; confira o endereço fiscal.');
            return;
          }
          var dados = resultado.dados;
          var ufManualDiferente = alterados.has('uf') && campo('uf').value.trim().toUpperCase() !== String(dados.uf || '').trim().toUpperCase();
          var aplicou = false;
          nomes.forEach(function (nome) {
            if (alterados.has(nome) || (ufManualDiferente && nome !== 'nome')) return;
            var valor = dados[nome];
            if (nome === 'situacao_ie' && !valor) valor = 'desconhecida';
            if (valor === undefined || valor === null) valor = '';
            if (campo(nome).value !== String(valor)) {
              campo(nome).value = String(valor);
              aplicou = true;
            }
          });
          token.value = String(resultado.token || '');
          if (aplicou) confirmado.checked = false;
          atualizarIE();
          avisar((resultado.aviso || (ufManualDiferente
            ? 'Mantivemos o endereço fiscal que você informou. Confira os dados antes de continuar.'
            : 'Dados da empresa preenchidos. Confira o endereço fiscal e a situação da inscrição estadual.'))
            + origemConsulta(resultado));
          form.dispatchEvent(new Event('change', {bubbles: true}));
        } catch (erro) {
          if (rodada !== sequencia || cnpj !== digitos(documento.value) || erro.name === 'AbortError') return;
          ultimoConsultado = cnpj;
          avisar((erroConsulta || 'A consulta está indisponível no momento.')
            + ' Você pode preencher os dados fiscais manualmente e conferir antes de continuar.');
        } finally {
          if (rodada === sequencia) {
            botao.disabled = false;
            bloco.removeAttribute('aria-busy');
            controlador = null;
          }
        }
      }
      nomes.forEach(function (nome) {
        var input = campo(nome);
        if (input.value.trim()) alterados.add(nome); // preserva o POST reapresentado com erros
        function editar() {
          alterados.add(nome);
          confirmado.checked = false;
          if (nome === 'uf') input.value = input.value.toUpperCase();
          if (nome === 'situacao_ie') atualizarIE();
        }
        input.addEventListener('input', editar);
        input.addEventListener('change', editar);
      });
      documento.addEventListener('input', atualizarTipo);
      documento.addEventListener('change', atualizarTipo);
      botao.addEventListener('click', consultar);
      atualizarTipo();
    });
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', iniciar);
  else iniciar();
})();
