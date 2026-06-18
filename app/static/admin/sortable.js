/* Sortable mínimo (drag-and-drop) — vanilla, mouse + touch via pointer events.
 *
 * Uso:
 *   sortable(container, { onSort: function(novaOrdem) { ... } });
 *
 *   - container: elemento que contém os filhos arrastáveis.
 *   - cada filho precisa de data-id (string que identifica o item).
 *   - onSort recebe a lista de ids na ordem nova, DEPOIS de soltar.
 *
 * Estratégia: durante o drag, o item arrastado fica `position:fixed` colado
 * no ponteiro; os irmãos ficam normais e a gente swap-eia ele entre eles
 * conforme a posição vertical do ponteiro. Sem clone fantasma — o próprio
 * item é o que se move.
 *
 * Sem dependência externa (CSP, Railway, mobile, etc).
 */
(function () {
  'use strict';

  function sortable(container, opts) {
    opts = opts || {};

    var item = null;        // item sendo arrastado
    var startY = 0;         // posição Y do ponteiro ao começar
    var moveu = false;      // já passou do threshold de 4px? (anti-click acidental)
    var savedStyles = null; // estilos originais pra restaurar

    function emItem(e) {
      var it = e.target.closest('[data-id]');
      if (!it || it.parentElement !== container) return null;
      // Não arrasta a partir de elementos interativos (deixa o click rolar)
      if (e.target.closest('a, button, input, select, textarea, [contenteditable]'))
        return null;
      return it;
    }

    function pegar(it, e) {
      item = it;
      startY = e.clientY;
      moveu = false;
      var r = it.getBoundingClientRect();
      savedStyles = {
        position: it.style.position, top: it.style.top,
        left: it.style.left, width: it.style.width,
        zIndex: it.style.zIndex, pointerEvents: it.style.pointerEvents,
        boxShadow: it.style.boxShadow, background: it.style.background,
      };
      it._sortable_top = r.top;
      it._sortable_left = r.left;
      it._sortable_width = r.width;
      try { it.setPointerCapture(e.pointerId); } catch (err) { /* opcional */ }
    }

    function iniciarVisual() {
      item.style.position = 'fixed';
      item.style.top = item._sortable_top + 'px';
      item.style.left = item._sortable_left + 'px';
      item.style.width = item._sortable_width + 'px';
      item.style.zIndex = '1000';
      item.style.pointerEvents = 'none';
      item.style.boxShadow = '0 8px 22px rgba(0,0,0,.18)';
      item.classList.add('sortable-dragging');
    }

    function mover(e) {
      // Move o item
      var dy = e.clientY - startY;
      item.style.top = (item._sortable_top + dy) + 'px';

      // Acha o irmão alvo e swap-eia se necessário
      var irmaos = [];
      for (var i = 0; i < container.children.length; i++) {
        var c = container.children[i];
        if (c !== item && c.dataset && c.dataset.id) irmaos.push(c);
      }
      var inseriuAntes = false;
      for (var j = 0; j < irmaos.length; j++) {
        var alvo = irmaos[j];
        var ar = alvo.getBoundingClientRect();
        if (e.clientY < ar.top + ar.height / 2) {
          if (alvo.previousElementSibling !== item) {
            container.insertBefore(item, alvo);
          }
          inseriuAntes = true;
          break;
        }
      }
      if (!inseriuAntes) {
        var ultimo = irmaos[irmaos.length - 1];
        if (ultimo && ultimo !== item.previousElementSibling) {
          container.appendChild(item);
        }
      }
    }

    function soltar() {
      if (!item) return;
      var it = item; item = null;
      // Restaura estilos
      Object.keys(savedStyles).forEach(function (k) { it.style[k] = savedStyles[k]; });
      it.classList.remove('sortable-dragging');

      if (moveu && opts.onSort) {
        var ids = [];
        for (var i = 0; i < container.children.length; i++) {
          var c = container.children[i];
          if (c.dataset && c.dataset.id) ids.push(c.dataset.id);
        }
        opts.onSort(ids);
      }
    }

    container.addEventListener('pointerdown', function (e) {
      var it = emItem(e);
      if (!it) return;
      pegar(it, e);
    });

    document.addEventListener('pointermove', function (e) {
      if (!item) return;
      if (!moveu) {
        if (Math.abs(e.clientY - startY) < 4) return;
        moveu = true;
        iniciarVisual();
      }
      e.preventDefault();
      mover(e);
    });

    document.addEventListener('pointerup', soltar);
    document.addEventListener('pointercancel', soltar);
  }

  window.sortable = sortable;
})();
