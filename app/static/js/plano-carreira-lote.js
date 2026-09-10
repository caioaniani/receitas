/* Seleção limitada aos enquadramentos exibidos pelo filtro atual. */
(function () {
    'use strict';
    var form = document.getElementById('aprovar-lote-form');
    if (!form) return;
    var todas = document.getElementById('selecionar-visiveis');
    var botao = document.getElementById('aprovar-selecionados');
    var textoBotao = botao.textContent;
    var contagem = document.getElementById('lote-contagem');
    var caixas = Array.from(document.querySelectorAll(
        'input[name="enquadramento_ids"][form="aprovar-lote-form"]:not(:disabled)'));

    function atualizar() {
        var selecionadas = caixas.filter(function (caixa) { return caixa.checked; }).length;
        contagem.textContent = selecionadas + (selecionadas === 1
            ? ' pessoa selecionada nesta lista' : ' pessoas selecionadas nesta lista');
        botao.disabled = selecionadas === 0;
        todas.disabled = caixas.length === 0;
        todas.checked = caixas.length > 0 && selecionadas === caixas.length;
        todas.indeterminate = selecionadas > 0 && selecionadas < caixas.length;
    }
    todas.addEventListener('change', function () {
        caixas.forEach(function (caixa) { caixa.checked = todas.checked; });
        atualizar();
    });
    caixas.forEach(function (caixa) { caixa.addEventListener('change', atualizar); });
    // Recalcula quando o navegador restaura a seleção ao voltar da revisão.
    window.addEventListener('pageshow', function () {
        // O cache de navegação pode restaurar o "Salvando..." do loading global.
        botao.textContent = textoBotao;
        atualizar();
    });
    atualizar();
})();
