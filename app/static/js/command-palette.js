// Command Palette — Cmd+K / Ctrl+K
// Busca global no sistema: receitas, produtos, navegação rápida.

(function() {
    'use strict';

    // O servidor resolve os destinos e filtra permissões antes de expor os dados.
    const dados = document.getElementById('busca-navegacao-dados');
    const TODAS = dados ? JSON.parse(dados.textContent) : [];

    let overlay = null;
    let input = null;
    let resultsEl = null;
    let selecionadoIdx = 0;
    let resultadosVisiveis = [];

    function montarUI() {
        if (overlay) return;
        overlay = document.createElement('div');
        overlay.id = 'cmdk-overlay';
        overlay.innerHTML = `
            <div id="cmdk-panel" role="dialog" aria-label="Busca rápida">
                <div id="cmdk-search">
                    <i class="bi bi-search" aria-hidden="true"></i>
                    <input type="text" id="cmdk-input" placeholder="O que você precisa? Ex.: estoque do site, senha, minis"
                           autocomplete="off" spellcheck="false">
                    <kbd>Esc</kbd>
                </div>
                <div id="cmdk-results" role="listbox"></div>
                <div id="cmdk-footer">
                    <span><kbd>&uarr;</kbd><kbd>&darr;</kbd> navegar</span>
                    <span><kbd>Enter</kbd> abrir</span>
                    <span><kbd>Esc</kbd> fechar</span>
                </div>
            </div>
        `;
        document.body.appendChild(overlay);
        input = overlay.querySelector('#cmdk-input');
        resultsEl = overlay.querySelector('#cmdk-results');

        overlay.addEventListener('click', (e) => { if (e.target === overlay) fechar(); });
        input.addEventListener('input', () => atualizarResultados(input.value));
        input.addEventListener('keydown', (e) => {
            if (e.key === 'ArrowDown') { e.preventDefault(); mover(1); }
            else if (e.key === 'ArrowUp') { e.preventDefault(); mover(-1); }
            else if (e.key === 'Enter') { e.preventDefault(); abrirSelecionado(); }
            else if (e.key === 'Escape') { e.preventDefault(); fechar(); }
        });
    }

    function abrir() {
        montarUI();
        overlay.classList.add('open');
        input.value = '';
        atualizarResultados('');
        setTimeout(() => input.focus(), 50);
    }

    function fechar() {
        overlay.classList.remove('open');
    }

    function mover(delta) {
        selecionadoIdx = Math.max(0, Math.min(resultadosVisiveis.length - 1, selecionadoIdx + delta));
        renderizar();
        const sel = resultsEl.querySelector('.cmdk-item.sel');
        if (sel) sel.scrollIntoView({ block: 'nearest' });
    }

    function abrirSelecionado() {
        const item = resultadosVisiveis[selecionadoIdx];
        if (item) window.location.href = item.url;
    }

    function normalizar(s) {
        return (s || '').toLowerCase().normalize('NFD').replace(/[̀-ͯ]/g, '');
    }

    function atualizarResultados(query) {
        const q = normalizar(query.trim());
        if (!q) {
            // Mostra ações principais quando vazio
            resultadosVisiveis = TODAS.filter((i) => i.principal).slice(0, 12);
        } else {
            resultadosVisiveis = TODAS
                .map((i) => ({ item: i, score: pontuar(q, i) }))
                .filter((r) => r.score > 0)
                .sort((a, b) => b.score - a.score)
                .slice(0, 20)
                .map((r) => r.item);
        }
        selecionadoIdx = 0;
        renderizar();
    }

    function pontuar(q, item) {
        const titulo = normalizar(item.titulo);
        const cat = normalizar(item.categoria);
        const aliases = (item.aliases || []).map(normalizar);
        if (titulo === q) return 100;
        if (aliases.includes(q)) return 90;
        if (titulo.startsWith(q)) return 80;
        if (titulo.includes(q)) return 50;
        if (aliases.some((alias) => alias.includes(q))) return 40;
        if (cat.includes(q)) return 20;
        // Fuzzy: cada caractere de q presente em ordem em titulo
        let ti = 0, hits = 0;
        for (const c of q) {
            const idx = titulo.indexOf(c, ti);
            if (idx === -1) return 0;
            ti = idx + 1;
            hits++;
        }
        return hits >= q.length ? 10 : 0;
    }

    // Nomes vêm do cadastro: renderizar como texto, nunca como HTML executável.
    function escaparHtml(valor) {
        return String(valor).replace(/[&<>"']/g, (c) => ({
            '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
        }[c]));
    }

    function renderizar() {
        if (resultadosVisiveis.length === 0) {
            resultsEl.innerHTML = '<div class="cmdk-empty">Nenhum resultado</div>';
            return;
        }
        const agrupado = {};
        resultadosVisiveis.forEach((it, idx) => {
            if (!agrupado[it.categoria]) agrupado[it.categoria] = [];
            agrupado[it.categoria].push({ ...it, idx });
        });
        let html = '';
        for (const cat in agrupado) {
            html += `<div class="cmdk-cat">${escaparHtml(cat)}</div>`;
            for (const it of agrupado[cat]) {
                const sel = it.idx === selecionadoIdx ? 'sel' : '';
                html += `<a href="${escaparHtml(it.url)}" class="cmdk-item ${sel}" data-idx="${it.idx}">
                    <i class="bi bi-${escaparHtml(it.icon)}"></i>
                    <span>${escaparHtml(it.titulo)}</span>
                </a>`;
            }
        }
        resultsEl.innerHTML = html;
        resultsEl.querySelectorAll('.cmdk-item').forEach((el) => {
            el.addEventListener('mouseenter', () => {
                selecionadoIdx = parseInt(el.dataset.idx);
                resultsEl.querySelectorAll('.cmdk-item.sel').forEach((e) => e.classList.remove('sel'));
                el.classList.add('sel');
            });
        });
    }

    // Atalhos globais
    document.addEventListener('keydown', (e) => {
        const ehCmdOuCtrl = e.metaKey || e.ctrlKey;
        if (ehCmdOuCtrl && e.key === 'k') {
            e.preventDefault();
            abrir();
        }
    });

    // Expõe para outros scripts
    window.abrirCommandPalette = abrir;
})();
