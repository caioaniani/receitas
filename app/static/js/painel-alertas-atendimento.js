/* Pendências vêm do servidor. Adiar este aviso nunca reconhece nem resolve atendimento. */
(function () {
  'use strict';

  function iniciar() {
    var raiz = document.getElementById('painel-alertas-atendimento');
    if (!raiz || raiz.dataset.iniciado === '1') return;
    raiz.dataset.iniciado = '1';
    document.body.classList.add('pa-at-painel');
    var botao = raiz.querySelector('[data-pa-botao]');
    var contador = raiz.querySelector('[data-pa-contador]');
    var dialog = raiz.querySelector('[data-pa-dialog]');
    var lista = raiz.querySelector('[data-pa-lista]');
    var resumo = raiz.querySelector('[data-pa-resumo]');
    var avisoLocal = raiz.querySelector('[data-pa-adiamento]');
    var erroFora = raiz.querySelector('[data-pa-erro-fora]');
    var erroDentro = raiz.querySelector('[data-pa-erro-dentro]');
    var chaveStorage = 'painel.atendimento.adiamentos.v1';
    var intervalo = 20000;
    var adiamentoMs = 5 * 60 * 1000;
    var alertas = [];
    var cartoes = new Map();
    var adiados = new Map();
    var requisicao = null;
    var consultarDepois = false;
    var versao = 0;
    var suspenso = false;
    var compondo = false;
    var timerPoll = null;
    var timerReabrir = null;
    var timerSom = null;
    var audioCtx = null;
    var audioSolicitado = false;
    var audioFalhou = false;
    var proximoSomEm = 0;
    var tonsAtivos = [];
    var erroAtual = '';

    function texto(valor, padrao) {
      return typeof valor === 'string' && valor.trim() ? valor : (padrao || '');
    }

    try {
      var salvos = JSON.parse(window.sessionStorage.getItem(chaveStorage) || '{}');
      if (salvos && typeof salvos === 'object' && !Array.isArray(salvos)) {
        Object.keys(salvos).forEach(function (chave) {
          var ate = salvos[chave];
          if (typeof ate === 'number' && Number.isFinite(ate) && ate > Date.now()) {
            adiados.set(chave, Math.min(ate, Date.now() + adiamentoMs));
          }
        });
      }
    } catch (_) { /* Storage indisponível: o adiamento continua em memória nesta página. */ }

    function persistir() {
      var salvos = Object.create(null);
      adiados.forEach(function (ate, chave) { salvos[chave] = ate; });
      try {
        window.sessionStorage.setItem(chaveStorage, JSON.stringify(salvos));
      } catch (_) { /* Navegadores que bloqueiam storage mantêm o comportamento em memória. */ }
    }

    function limparAdiamentos() {
      var presentes = new Set(alertas.map(function (alerta) { return alerta.chave; }));
      adiados.forEach(function (ate, chave) {
        if (ate <= Date.now() || !presentes.has(chave)) adiados.delete(chave);
      });
      persistir();
    }

    function atualizarErro() {
      erroDentro.textContent = erroAtual;
      erroFora.textContent = erroAtual;
      erroDentro.hidden = !erroAtual;
      erroFora.hidden = !erroAtual || dialog.open;
    }

    function fechar() {
      if (dialog.open) dialog.close();
      botao.setAttribute('aria-expanded', 'false');
      atualizarErro();
    }

    function haRascunho() {
      var compose = document.getElementById('compose-texto');
      return compondo || Boolean(compose && compose.value.length);
    }

    function threadAberta() {
      // Conversa aberta na coluna da direita.
      var thread = document.getElementById('at-thread');
      return Boolean(thread && !thread.classList.contains('hidden'));
    }

    // "Alguém está atendendo" = conversa aberta OU rascunho, COM atividade
    // recente nesta página. Sem a janela de atividade, uma thread esquecida
    // aberta na TV (a própria foto do caso) silenciaria o cartão por dias, e
    // um rascunho deixado ao clicar "Voltar" o bloquearia para sempre
    // (revisão 22/09/2026).
    var ultimaAtividade = Date.now();
    var inatividadeMs = 10 * 60 * 1000;
    function atividadeRecente() {
      return Date.now() - ultimaAtividade < inatividadeMs;
    }
    function atendendo() {
      return atividadeRecente() && (threadAberta() || haRascunho());
    }

    function pararSom() {
      clearTimeout(timerSom);
      timerSom = null;
      tonsAtivos.forEach(function (tom) {
        try { tom.osc.stop(); } catch (_) { /* O tom pode já ter terminado. */ }
        tom.osc.disconnect();
        tom.gain.disconnect();
      });
      tonsAtivos = [];
    }

    function agendarSom() {
      clearTimeout(timerSom);
      timerSom = null;
      if (suspenso || document.hidden || !audioCtx || audioCtx.state !== 'running' || !alertas.length) return;
      var elegivelEm = Math.min.apply(null, alertas.map(function (alerta) {
        return adiados.get(alerta.chave) || 0;
      }));
      // O vencimento é absoluto: os polls de 20s não reiniciam a contagem.
      var quando = Math.max(Date.now(), proximoSomEm, elegivelEm);
      // Quem está atendendo não leva bipe; reavalia em 1 min.
      if (atendendo()) quando = Math.max(quando, Date.now() + 60 * 1000);
      timerSom = setTimeout(tocarSom, Math.max(0, quando - Date.now()));
    }

    function tocarSom() {
      timerSom = null;
      var elegivel = alertas.some(function (alerta) {
        return (adiados.get(alerta.chave) || 0) <= Date.now();
      });
      if (suspenso || document.hidden || !audioCtx || audioCtx.state !== 'running'
          || !elegivel || proximoSomEm > Date.now() || atendendo()) {
        agendarSom();
        return;
      }
      // Um único reforço por janela, sem acumular disparos após aba suspensa.
      proximoSomEm = Date.now() + adiamentoMs;
      try {
        var inicio = audioCtx.currentTime;
        [660, 880].forEach(function (frequencia, indice) {
          var osc = audioCtx.createOscillator();
          var gain = audioCtx.createGain();
          var tom = { osc: osc, gain: gain };
          tonsAtivos.push(tom);
          osc.type = 'sine';
          osc.frequency.value = frequencia;
          osc.connect(gain);
          gain.connect(audioCtx.destination);
          var t = inicio + indice * 0.2;
          gain.gain.setValueAtTime(0.0001, t);
          gain.gain.exponentialRampToValueAtTime(0.12, t + 0.02);
          gain.gain.exponentialRampToValueAtTime(0.0001, t + 0.17);
          osc.onended = function () {
            osc.disconnect();
            gain.disconnect();
            tonsAtivos = tonsAtivos.filter(function (atual) { return atual !== tom; });
          };
          osc.start(t);
          osc.stop(t + 0.19);
        });
      } catch (erro) {
        pararSom();
        console.warn('Não foi possível tocar o aviso breve de atendimento.', erro.name);
      }
      agendarSom();
    }

    function armarSom() {
      if (suspenso || document.hidden) return;
      audioSolicitado = true;
      var TipoAudio = window.AudioContext || window.webkitAudioContext;
      if (!TipoAudio) return;
      function falhaAudio(erro) {
        if (!audioFalhou) console.warn('Áudio de atendimento aguarda permissão do navegador.', erro.name);
        audioFalhou = true;
      }
      try {
        if (!audioCtx || audioCtx.state === 'closed') {
          audioCtx = new TipoAudio();
          audioCtx.addEventListener('statechange', agendarSom);
        }
        var retomada = audioCtx.state === 'suspended' ? audioCtx.resume() : Promise.resolve();
        Promise.resolve(retomada).then(function () {
          if (audioCtx.state === 'running') audioFalhou = false;
          // Revalida fila, adiamento e visibilidade após um resume assíncrono.
          agendarSom();
        }).catch(falhaAudio);
      } catch (erro) { falhaAudio(erro); }
    }

    function armarReabertura() {
      clearTimeout(timerReabrir);
      timerReabrir = null;
      if (suspenso) return;
      var futuro = alertas.map(function (alerta) { return adiados.get(alerta.chave) || 0; })
        .filter(function (ate) { return ate > Date.now(); });
      if (futuro.length) {
        timerReabrir = setTimeout(function () {
          limparAdiamentos();
          mostrar(false);
          armarReabertura();
          agendarSom();
        }, Math.max(1, Math.min.apply(null, futuro) - Date.now()));
      }
    }

    function mostrar(manual) {
      if (suspenso || !alertas.length || dialog.open) return;
      var vencido = alertas.some(function (alerta) {
        return (adiados.get(alerta.chave) || 0) <= Date.now();
      });
      // Nunca por cima de quem está atendendo (conversa aberta ou rascunho,
      // com atividade recente). Caso real 22/09/2026: o aviso reabria 20 s
      // depois do "Abrir conversa" (a outra pendência seguia vencida) e, na
      // TV, onde ninguém clica, cobria o painel inteiro. O gesto manual
      // (botão "!") continua abrindo.
      if (!manual && (!vencido || atendendo() || document.hidden)) return;
      // Não-modal de propósito: a coluna de pedidos e a caixa de resposta
      // seguem utilizáveis (a lista fica atrás do cartão até × / Esc / abrir
      // uma conversa). A posição flutuante vem do CSS.
      var ativo = document.activeElement;
      dialog.show();
      // Abertura automática não rouba o foco de quem digita em outro lugar
      // (ex.: no painel de pedidos); o `autofocus` do × só vale no gesto manual.
      if (!manual && ativo && ativo !== document.body && typeof ativo.focus === 'function') {
        try { ativo.focus(); } catch (_) { /* elemento pode ter sumido */ }
      }
      botao.setAttribute('aria-expanded', 'true');
      avisoLocal.hidden = true;
      atualizarErro();
    }

    function adiar(convId) {
      var ate = Date.now() + adiamentoMs;
      alertas.forEach(function (alerta) {
        if (convId === undefined || alerta.conv_id === convId) adiados.set(alerta.chave, ate);
      });
      persistir();
      fechar();
      avisoLocal.textContent = 'Aviso adiado por 5 minutos nesta aba. As pendências continuam abertas.';
      avisoLocal.hidden = !alertas.length;
      armarReabertura();
      pararSom();
      agendarSom();
    }

    function elemento(tag, classe, conteudo) {
      var no = document.createElement(tag);
      no.className = classe;
      if (conteudo) no.textContent = conteudo;
      return no;
    }

    function criarCartao(chave) {
      var item = elemento('li', 'pa-at-item');
      var topo = elemento('div', 'pa-at-item-topo');
      var nome = elemento('h3', 'pa-at-nome');
      var grave = elemento('span', 'pa-at-gravidade', 'Urgente');
      var estado = elemento('span', 'pa-at-estado');
      topo.append(nome, grave, estado);
      var motivo = elemento('p', 'pa-at-motivo');
      var mensagem = elemento('p', 'pa-at-mensagem');
      var abrir = elemento('button', 'pa-at-abrir', 'Abrir conversa');
      abrir.type = 'button';
      abrir.addEventListener('click', function () {
        var alerta = alertas.find(function (atual) { return atual.chave === chave; });
        if (!alerta) return;
        // Adia só a pendência clicada: quem segura o cartão enquanto a
        // conversa está aberta é `atendendo()`; ao voltar à lista, as outras
        // pendências voltam a cobrar na hora (revisão 22/09/2026).
        adiar(alerta.conv_id);
        document.dispatchEvent(new CustomEvent('atendimento:abrir', {
          detail: { conv_id: alerta.conv_id, nome: alerta.cliente }
        }));
      });
      item.append(topo, motivo, mensagem, abrir);
      return { item: item, nome: nome, grave: grave, estado: estado,
        motivo: motivo, mensagem: mensagem, abrir: abrir };
    }

    function renderizar() {
      var presentes = new Set(alertas.map(function (alerta) { return alerta.chave; }));
      cartoes.forEach(function (cartao, chave) {
        if (!presentes.has(chave)) {
          cartao.item.remove();
          cartoes.delete(chave);
        }
      });
      alertas.forEach(function (alerta, indice) {
        var cartao = cartoes.get(alerta.chave);
        if (!cartao) {
          cartao = criarCartao(alerta.chave);
          cartoes.set(alerta.chave, cartao);
        }
        cartao.nome.textContent = alerta.cliente;
        cartao.grave.hidden = !alerta.grave;
        cartao.item.classList.toggle('pa-at-item-grave', alerta.grave);
        cartao.estado.textContent = (alerta.estado === 'em_atendimento' ? 'Em atendimento' : 'Aguardando atendimento')
          + ' · ' + (alerta.ha_minutos > 0 ? alerta.ha_minutos + ' min' : 'Agora');
        cartao.motivo.textContent = alerta.motivo;
        cartao.mensagem.textContent = alerta.mensagem;
        cartao.mensagem.hidden = !alerta.mensagem;
        cartao.abrir.setAttribute('aria-label', 'Abrir conversa de ' + alerta.cliente);
        // Não recria botões a cada poll: preserva o foco quando a ordem não muda.
        if (lista.children[indice] !== cartao.item) {
          lista.insertBefore(cartao.item, lista.children[indice] || null);
        }
      });
      contador.textContent = String(alertas.length);
      botao.hidden = !alertas.length;
      botao.setAttribute('aria-label', 'Atendimento: ' + alertas.length + ' pendência(s)');
      resumo.textContent = alertas.length === 1 ? '1 pendência de atendimento' : alertas.length + ' pendências de atendimento';
      if (!alertas.length) {
        fechar();
        avisoLocal.hidden = true;
        pararSom();
      }
      limparAdiamentos();
      mostrar(false);
      armarReabertura();
      agendarSom();
    }

    function normalizar(payload) {
      if (!payload || !Array.isArray(payload.alertas)) throw new Error('Resposta de alertas inválida.');
      var chaves = new Set();
      return payload.alertas.map(function (alerta) {
        if (!alerta || typeof alerta.chave !== 'string' || !alerta.chave.trim()
            || !Number.isSafeInteger(Number(alerta.conv_id)) || Number(alerta.conv_id) <= 0
            || !['aguardando', 'em_atendimento'].includes(alerta.estado) || chaves.has(alerta.chave)) {
          throw new Error('Alerta inválido.');
        }
        chaves.add(alerta.chave);
        return { chave: alerta.chave, conv_id: Number(alerta.conv_id),
          cliente: texto(alerta.cliente, 'Cliente'), motivo: texto(alerta.motivo, 'Precisa de atendimento humano.'),
          mensagem: texto(alerta.mensagem), grave: alerta.grave === true, estado: alerta.estado,
          ha_minutos: Number.isFinite(Number(alerta.ha_minutos)) ? Math.max(0, Math.floor(Number(alerta.ha_minutos))) : 0 };
      });
    }

    function falhou() {
      erroAtual = alertas.length
        ? 'Não foi possível atualizar os alertas. A última lista foi mantida; tentaremos novamente.'
        : 'Não foi possível consultar os alertas. Tentaremos novamente em instantes.';
      atualizarErro();
    }

    async function consultar(imediata) {
      if (suspenso) return;
      if (requisicao) {
        if (imediata) {
          consultarDepois = true;
          requisicao.obsoleta = true;
          requisicao.controller.abort();
        }
        return;
      }
      consultarDepois = false;
      var atual = { versao: ++versao, controller: new AbortController(), obsoleta: false, expirou: false };
      requisicao = atual;
      var timeout = setTimeout(function () {
        atual.expirou = true;
        atual.controller.abort();
        if (!suspenso && !atual.obsoleta && requisicao === atual) falhou();
      }, 10000);
      try {
        var resposta = await fetch(raiz.dataset.url, { method: 'GET', credentials: 'same-origin',
          cache: 'no-store', headers: { Accept: 'application/json' }, signal: atual.controller.signal });
        if (!resposta.ok) throw new Error('Falha na consulta de alertas.');
        var payload = await resposta.json();
        if (suspenso || atual.obsoleta || atual.expirou || atual.versao !== versao) return;
        alertas = normalizar(payload);
        if (typeof payload.csrf === 'string' && payload.csrf) {
          document.dispatchEvent(new CustomEvent('atendimento:csrf', { detail: { csrf: payload.csrf } }));
        }
        erroAtual = '';
        atualizarErro();
        renderizar();
      } catch (_) {
        if (!suspenso && !atual.obsoleta && atual.versao === versao) falhou();
      } finally {
        clearTimeout(timeout);
        if (requisicao === atual) requisicao = null;
        if (consultarDepois && !suspenso) consultar(false);
      }
    }

    botao.addEventListener('click', function () { mostrar(true); });
    raiz.querySelector('[data-pa-fechar]').addEventListener('click', function () { adiar(); });
    raiz.querySelector('[data-pa-adiar]').addEventListener('click', function () { adiar(); });
    dialog.addEventListener('cancel', function (evento) {
      evento.preventDefault();
      adiar();
    });
    document.addEventListener('keydown', function (evento) {
      // Diálogo não-modal não recebe `cancel`: Esc adia como o botão ×.
      if (evento.key === 'Escape' && dialog.open) adiar();
    });
    dialog.addEventListener('close', function () {
      botao.setAttribute('aria-expanded', 'false');
      atualizarErro();
    });
    document.addEventListener('atendimento:atualizado', function () { consultar(true); });
    document.addEventListener('compositionstart', function (evento) {
      if (evento.target && evento.target.id === 'compose-texto') compondo = true;
    });
    document.addEventListener('compositionend', function (evento) {
      if (evento.target && evento.target.id === 'compose-texto') compondo = false;
    });
    // A thread abre/fecha por vários caminhos (lista, vigia, "Chamar",
    // "Voltar", resolver): abriu com o cartão na frente → o cartão sai da
    // frente; voltou à lista → cobra na hora, sem esperar o poll.
    var threadEl = document.getElementById('at-thread');
    if (threadEl && typeof window.MutationObserver === 'function') {
      new window.MutationObserver(function () {
        if (threadAberta()) {
          if (dialog.open) fechar();
          pararSom();
        } else {
          mostrar(false);
        }
        agendarSom();
      }).observe(threadEl, { attributes: true, attributeFilter: ['class'] });
    }
    ['click', 'pointerdown', 'keydown', 'touchend'].forEach(function (tipo) {
      window.addEventListener(tipo, function (evento) {
        if (evento.isTrusted && !evento.repeat) {
          ultimaAtividade = Date.now();
          armarSom();
        }
      }, { passive: true });
    });
    window.addEventListener('message', function (evento) {
      var painel = document.querySelector('#pane-painel iframe');
      if (evento.origin !== window.location.origin || !painel || evento.source !== painel.contentWindow) return;
      if (evento.data && evento.data.tipo === 'painel-audio-armado') {
        ultimaAtividade = Date.now();
        armarSom();
      }
    });
    document.addEventListener('visibilitychange', function () {
      if (document.hidden) pararSom();
      else {
        mostrar(false);
        if (audioSolicitado) armarSom();
      }
    });
    window.addEventListener('pagehide', function () {
      suspenso = true;
      versao += 1;
      clearInterval(timerPoll);
      clearTimeout(timerReabrir);
      pararSom();
      if (requisicao) {
        requisicao.obsoleta = true;
        requisicao.controller.abort();
      }
    });
    window.addEventListener('pageshow', function () {
      if (!suspenso) return;
      suspenso = false;
      timerPoll = setInterval(function () { consultar(false); }, intervalo);
      consultar(true);
      armarReabertura();
      if (audioSolicitado) armarSom();
    });
    timerPoll = setInterval(function () { consultar(false); }, intervalo);
    consultar(false);
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', iniciar, { once: true });
  else iniciar();
}());
