/* Registro dos trechos assistidos; o servidor decide cobertura e conclusão. */
(function (global) {
  'use strict';

  global.TreinoProgressoPlayer = function (opcoes) {
    var player = opcoes.player;
    var fila = Promise.resolve();
    var falhou = false;
    var heartbeatPendente = null;
    var geracao = 0, inicioPendente = null, playAutorizado = null;
    var pausaInterna = false, buscando = false, retomarBusca = false;

    function cancelarHeartbeat() {
      if (heartbeatPendente) heartbeatPendente.cancelado = true;
      heartbeatPendente = null;
    }

    function salvar(evento) {
      if (evento === 'heartbeat' && heartbeatPendente) return fila;
      var amostra = {posicao: player.currentTime || 0, velocidade: player.playbackRate || 1};
      var pedido = {cancelado: false};
      if (evento === 'heartbeat') {
        heartbeatPendente = pedido;
      } else if (heartbeatPendente) {
        // A pausa/fim fecha este trecho. Uma amostra futura não pode passar
        // desse limite e depois fazer o servidor voltar à posição da pausa.
        cancelarHeartbeat();
      }
      fila = fila.then(function () {
        if (pedido.cancelado) return;
        if (evento === 'heartbeat') {
          heartbeatPendente = null;
          if (player.paused || player.seeking) return;
          // Rede lenta: a posição periódica deve corresponder ao envio, não
          // ao instante antigo em que o timer entrou na fila.
          amostra = {posicao: player.currentTime || 0, velocidade: player.playbackRate || 1};
        }
        var corpo = 't=' + encodeURIComponent(amostra.posicao) +
          '&v=' + encodeURIComponent(amostra.velocidade) +
          '&evento=' + encodeURIComponent(evento);
        return fetch(opcoes.url, {
          method: 'POST',
          headers: {'X-CSRFToken': opcoes.csrf, 'Content-Type': 'application/x-www-form-urlencoded'},
          body: corpo,
          keepalive: evento === 'pause' || evento === 'ended'
        }).then(function (resposta) {
          if (!resposta.ok) throw new Error('Falha ao salvar progresso');
          return resposta.json();
        }).then(function (dados) {
          if (!dados || !dados.ok) throw new Error('Progresso não confirmado');
          var valor = dados.concluido ? 100 : dados.pct;
          opcoes.bar.style.width = valor + '%';
          opcoes.pct.textContent = Math.round(valor) + '%';
          if (dados.concluido) {
            opcoes.msg.textContent = 'Concluído! Você pode fazer a avaliação.';
          } else if (evento === 'ended') {
            opcoes.msg.textContent = 'Progresso salvo. Ainda faltam trechos ou perguntas para concluir. Reproduza novamente os trechos pendentes.';
          } else if (falhou) {
            opcoes.msg.textContent = 'O progresso voltou a ser salvo. Continue a aula.';
          }
          falhou = false;
          return dados;
        });
      }).catch(function () {
        falhou = true;
        opcoes.msg.textContent = 'Não foi possível salvar o progresso. Verifique a conexão e retome a aula para tentar novamente.';
      });
      return fila;
    }

    function aguardando(valor) {
      if (opcoes.onAguardar) opcoes.onAguardar(valor);
    }

    function cancelarInicio() {
      geracao += 1;
      inicioPendente = null;
      aguardando(false);
    }

    function falhaAoReproduzir() {
      aguardando(false);
      opcoes.msg.textContent = 'Não foi possível iniciar a aula. Toque em Reproduzir para tentar novamente.';
    }

    function confirmarPausa() {
      var inicio = inicioPendente;
      if (!inicio || inicio.enviado || buscando) return;
      inicio.enviado = true;
      salvar('play').then(function (dados) {
        // Cliques repetidos e buscas invalidam confirmações antigas.
        if (inicioPendente !== inicio || inicio.geracao !== geracao) return;
        inicioPendente = null;
        if (!dados || !dados.ok) {
          aguardando(false);
          return; // A mensagem de erro do envio permanece; o vídeo fica parado.
        }
        var permissao = {geracao: geracao};
        playAutorizado = permissao;
        try {
          var retomada = player.play();
          if (retomada && typeof retomada.catch === 'function') retomada.catch(function () {
            if (playAutorizado === permissao) {
              playAutorizado = null;
              falhaAoReproduzir();
            }
          });
        } catch (erro) {
          playAutorizado = null;
          falhaAoReproduzir();
        }
      });
    }

    function iniciarComConfirmacao() {
      geracao += 1;
      inicioPendente = {geracao: geracao, enviado: false};
      cancelarHeartbeat();
      aguardando(true);
      opcoes.msg.textContent = 'Aguarde: confirmando o progresso antes de iniciar a aula…';
      // pause/play do SDK são assíncronos. Só mede a baseline depois da
      // confirmação de pausa, para não assistir enquanto a rede responde.
      if (player.paused) confirmarPausa();
      else {
        pausaInterna = true;
        player.pause();
      }
    }

    player.addEventListener('play', function () {
      if (playAutorizado) {
        var permissao = playAutorizado;
        playAutorizado = null;
        if (permissao.geracao === geracao && !inicioPendente && !buscando) {
          aguardando(false);
          opcoes.msg.textContent = 'O progresso é salvo automaticamente. Continue a aula.';
          return;
        }
        // Um play do SDK já solicitado pode chegar depois de uma busca.
        pausaInterna = true;
        player.pause();
        return;
      }
      if (buscando) {
        retomarBusca = true;
        pausaInterna = true;
        player.pause();
        return;
      }
      iniciarComConfirmacao();
    });
    player.addEventListener('pause', function () {
      if (pausaInterna) {
        pausaInterna = false;
        confirmarPausa();
        return;
      }
      cancelarInicio();
      salvar('pause');
    });
    player.addEventListener('ended', function () {
      cancelarInicio();
      salvar('ended');
    });
    player.addEventListener('seeking', function () {
      retomarBusca = !player.paused || !!inicioPendente || !!playAutorizado;
      buscando = true;
      cancelarInicio();
      cancelarHeartbeat();
      if (!player.paused) {
        pausaInterna = true;
        player.pause();
      }
    });
    player.addEventListener('seeked', function () {
      buscando = false;
      if (retomarBusca) {
        retomarBusca = false;
        iniciarComConfirmacao();
      } else salvar('play');
    });
    setInterval(function () {
      if (!player.paused && !player.seeking && !inicioPendente && !playAutorizado && !buscando) salvar('heartbeat');
    }, 15000);

    return {salvar: salvar};
  };
})(window);
