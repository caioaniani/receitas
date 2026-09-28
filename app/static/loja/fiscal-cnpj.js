/* Cadastro fiscal independente do contato do comprador e da entrega. */
(function () {
  'use strict';

  /* Documento CPF/CNPJ — espelho de app.utils.normalizar_documento/
   * cnpj_valido (CNPJ alfanumérico, IN RFB 2.229/2024: 12 posições [0-9A-Z]
   * + 2 dígitos verificadores). Reduzir a dígitos TRUNCA o CNPJ novo.
   * Exposto em window.DocumentoFiscal para a máscara do checkout usar a
   * MESMA regra (fonte única no navegador). */
  var DocumentoFiscal = {
    normalizar: function (valor) {
      return String(valor || '').replace(/[^0-9A-Za-z]/g, '').toUpperCase();
    },
    // Formato de CNPJ (numérico ou alfanumérico); o servidor confere o DV.
    ehCnpj: function (valor) {
      return /^[0-9A-Z]{12}[0-9]{2}$/.test(DocumentoFiscal.normalizar(valor));
    },
    // Máscara progressiva: com letra ou mais de 11 posições vira CNPJ
    // (XX.XXX.XXX/XXXX-XX); senão, CPF (XXX.XXX.XXX-XX).
    mascarar: function (valor) {
      var d = DocumentoFiscal.normalizar(valor).slice(0, 14);
      if (d.length > 11 || /[A-Z]/.test(d)) {
        var out = d.slice(0, 2);
        if (d.length > 2) out += '.' + d.slice(2, 5);
        if (d.length > 5) out += '.' + d.slice(5, 8);
        if (d.length > 8) out += '/' + d.slice(8, 12);
        if (d.length > 12) out += '-' + d.slice(12);
        return out;
      }
      if (d.length > 9) return d.slice(0, 3) + '.' + d.slice(3, 6) + '.' + d.slice(6, 9) + '-' + d.slice(9);
      if (d.length > 6) return d.slice(0, 3) + '.' + d.slice(3, 6) + '.' + d.slice(6);
      if (d.length > 3) return d.slice(0, 3) + '.' + d.slice(3);
      return d;
    }
  };
  window.DocumentoFiscal = DocumentoFiscal;

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
      var atual = DocumentoFiscal.normalizar(documento.value);
      var sequencia = 0;
      var controlador = null;
      var timer = null;
      var ultimoConsultado = '';

      function campo(nome) { return form.elements.namedItem('fiscal_' + nome); }
      function doc() { return DocumentoFiscal.normalizar(documento.value); }
      function avisar(texto) { status.textContent = texto || ''; }
      // O servidor corta ao limite da nota fiscal o que veio longo da base
      // pública; avisa só dos campos que a tela de fato preencheu.
      function avisoAbreviados(abreviados, aplicados) {
        var rotulos = (Array.isArray(abreviados) ? abreviados : []).filter(function (item) {
          return item && aplicados.indexOf(item.campo) !== -1 && item.rotulo;
        }).map(function (item) { return String(item.rotulo); });
        if (!rotulos.length) return '';
        return ' Abreviamos ' + rotulos.join(', ')
          + ' para caber no limite da nota fiscal; confira antes de continuar.';
      }
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
        var proximo = doc();
        if (proximo !== atual) {
          cancelar();
          limpar();
          atual = proximo;
        }
        var pj = DocumentoFiscal.ehCnpj(atual);
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
        var cnpj = doc();
        if (!DocumentoFiscal.ehCnpj(cnpj)) return;
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
          if (rodada !== sequencia || cnpj !== doc()) return;
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
          var aplicados = [];
          nomes.forEach(function (nome) {
            if (alterados.has(nome) || (ufManualDiferente && nome !== 'nome')) return;
            var valor = dados[nome];
            if (nome === 'situacao_ie' && !valor) valor = 'desconhecida';
            if (valor === undefined || valor === null) valor = '';
            aplicados.push(nome);
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
            + avisoAbreviados(resultado.abreviados, aplicados)
            + origemConsulta(resultado));
          form.dispatchEvent(new Event('change', {bubbles: true}));
        } catch (erro) {
          if (rodada !== sequencia || cnpj !== doc() || erro.name === 'AbortError') return;
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
