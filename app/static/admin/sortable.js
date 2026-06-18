/* Sortable mínimo (drag-and-drop) — vanilla, mouse + touch (pointer events).
 *
 * Uso:
 *   sortable(container, { onSort: function(novaOrdem) { ... } })
 *
 *   - `container`: elemento que contém os filhos arrastáveis.
 *   - Cada filho precisa de `data-id` (qualquer string que identifica o
 *     item). O callback recebe a lista de ids na nova ordem.
 *   - `onSort` dispara DEPOIS de soltar o item — bom pra fazer POST.
 *
 * Decisões deliberadas:
 *   - Sem dependência externa (CSP/Railway). Pointer events cobrem
 *     mouse + touch sem dois caminhos diferentes.
 *   - Sem fantasma flutuante: usamos o próprio item posicionado em fixed
 *     enquanto arrasta (mais rápido, menos código).
 *   - Inserção entre irmãos por bounding box: se o ponteiro está acima
 *     do centro do alvo, insere antes; senão, depois.
 */
(function () {
  'use strict';

  function sortable(container, opts) {
    opts = opts || {};
    var arrastando = null;
    var offY = 0;       // diferença ponteiro→top do item ao começar
    var startX = 0, startY = 0;

    function ondPointerDown(e) {
      var item = e.target.closest('[data-id]');
      if (!item || item.parentElement !== container) return;
      // Se clicou num <a>, <button> ou <input>, NÃO arrasta — deixa o
      // click normal acontecer.
      if (e.target.closest('a, button, input, select, textarea')) return;
      arrastando = item;
      var rect = item.getBoundingClientRect();
      offY = e.clientY - rect.top;
      startX = e.clientX; startY = e.clientY;
      item.classList.add('sortable-dragging');
      // Captura o pointer pra continuar recebendo events fora do item.
      try { item.setPointerCapture(e.pointerId); } catch (err) { /* opcional */ }
      // Sem preventDefault aqui — só prevenimos no MOVE pra não bloquear
      // clique acidental.
    }

    function onPointerMove(e) {
      if (!arrastando) return;
      // Só vira "arrasto" depois de mover uns 4px (anti-jitter no click).
      if (!arrastando.classList.contains('sortable-moving')) {
        if (Math.abs(e.clientX - startX) < 4 && Math.abs(e.clientY - startY) < 4) return;
        arrastando.classList.add('sortable-moving');
      }
      e.preventDefault();
      // Posiciona o item segundo o ponteiro.
      arrastando.style.position = 'relative';
      arrastando.style.top = (e.clientY - startY) + 'px';
      arrastando.style.zIndex = '100';
      arrastando.style.pointerEvents = 'none';

      // Procura o irmão alvo (que NÃO seja o arrastando) com o centro
      // mais próximo do ponteiro. Insere antes/depois conforme posição.
      var irmaos = Array.from(container.children).filter(function (c) {
        return c !== arrastando && c.dataset && c.dataset.id;
      });
      for (var i = 0; i < irmaos.length; i++) {
        var alvo = irmaos[i];
        var r = alvo.getBoundingClientRect();
        var meio = r.top + r.height / 2;
        if (e.clientY < meio) {
          // Inserir antes do alvo — só mexe se ainda não está aí.
          if (alvo.previousElementSibling !== arrastando) {
            container.insertBefore(arrastando, alvo);
            arrastando.style.top = '0px';
            startY = e.clientY - offY - arrastando.getBoundingClientRect().top + arrastando.getBoundingClientRect().top;
          }
          return;
        }
      }
      // Está abaixo de todos — vai pro final.
      var ultimo = irmaos[irmaos.length - 1];
      if (ultimo && ultimo.nextElementSibling !== arrastando) {
        container.appendChild(arrastando);
        arrastando.style.top = '0px';
      }
    }

    function onPointerUp() {
      if (!arrastando) return;
      var item = arrastando;
      arrastando = null;
      item.classList.remove('sortable-dragging', 'sortable-moving');
      item.style.position = ''; item.style.top = '';
      item.style.zIndex = ''; item.style.pointerEvents = '';

      if (opts.onSort) {
        var ids = Array.from(container.children)
          .map(function (c) { return c.dataset && c.dataset.id; })
          .filter(Boolean);
        opts.onSort(ids);
      }
    }

    container.addEventListener('pointerdown', ondPointerDown);
    document.addEventListener('pointermove', onPointerMove);
    document.addEventListener('pointerup', onPointerUp);
    document.addEventListener('pointercancel', onPointerUp);
  }

  window.sortable = sortable;
})();
