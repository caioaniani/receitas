// Command Palette — Cmd+K / Ctrl+K
// Busca global no sistema: receitas, produtos, navegação rápida.

(function() {
    'use strict';

    const ROTAS = [
        // Navegação principal
        { categoria: 'Navegar', titulo: 'Início', url: '/', icon: 'house' },
        { categoria: 'Navegar', titulo: 'Pedidos', url: '/pedidos/', icon: 'cart' },
        { categoria: 'Navegar', titulo: 'Estoque da loja', url: '/pedidos/estoque-loja', icon: 'box' },
        { categoria: 'Navegar', titulo: 'Histórico de estoque', url: '/pedidos/estoque-loja/historico', icon: 'clock-history' },
        { categoria: 'Navegar', titulo: 'Desperdício', url: '/pedidos/desperdicio', icon: 'trash' },
        { categoria: 'Navegar', titulo: 'Produção', url: '/producao/', icon: 'tools' },
        { categoria: 'Navegar', titulo: 'Congelados', url: '/pedidos/congelados', icon: 'snow' },
        { categoria: 'Navegar', titulo: 'Pedidos a separar', url: '/pedidos/separacao', icon: 'list-check' },

        { categoria: 'Catálogo', titulo: 'Matérias-primas', url: '/materias-primas/', icon: 'flower3' },
        { categoria: 'Catálogo', titulo: 'Estoque MP', url: '/materias-primas/estoque', icon: 'boxes' },
        { categoria: 'Catálogo', titulo: 'Fornecedores', url: '/fornecedores/', icon: 'truck' },
        { categoria: 'Catálogo', titulo: 'Produtos / Cestas', url: '/produtos/', icon: 'gift' },
        { categoria: 'Catálogo', titulo: 'Cardápio PDF', url: '/cardapio?tipo=atacado', icon: 'file-pdf' },

        { categoria: 'Vendas', titulo: 'Vendas PDV', url: '/pdv/', icon: 'cash-stack' },
        { categoria: 'Vendas', titulo: 'Itens vendidos', url: '/pdv/itens-vendidos', icon: 'graph-up' },
        { categoria: 'Vendas', titulo: 'Mapeamentos Seru', url: '/pdv/mapeamentos', icon: 'link-45deg' },
        { categoria: 'Vendas', titulo: 'Mapeamentos VNDA', url: '/pdv/vnda/', icon: 'link' },
        { categoria: 'Vendas', titulo: 'Entregas do site', url: '/entregas/', icon: 'geo-alt' },
        { categoria: 'Vendas', titulo: 'Relatório de pedidos', url: '/pedidos/relatorio', icon: 'file-text' },

        { categoria: 'RH', titulo: 'Painel RH', url: '/rh/', icon: 'people' },
        { categoria: 'RH', titulo: 'Funcionários', url: '/rh/funcionarios', icon: 'person-vcard' },
        { categoria: 'RH', titulo: 'Folha de pagamento', url: '/rh/folha', icon: 'cash-coin' },
        { categoria: 'RH', titulo: 'Lojas', url: '/rh/lojas', icon: 'shop' },
        { categoria: 'RH', titulo: 'Escala operacional', url: '/rh/escala', icon: 'calendar3' },
        { categoria: 'RH', titulo: 'Ponto', url: '/rh/ponto', icon: 'fingerprint' },
        { categoria: 'RH', titulo: 'Férias / folgas', url: '/rh/ferias', icon: 'umbrella' },

        { categoria: 'Sistema', titulo: 'Dashboards', url: '/relatorios/dashboards', icon: 'bar-chart-line' },
        { categoria: 'Sistema', titulo: 'Caixa diário', url: '/caixa', icon: 'piggy-bank' },
        { categoria: 'Sistema', titulo: 'Rentabilidade', url: '/rentabilidade', icon: 'currency-dollar' },
        { categoria: 'Sistema', titulo: 'Relatórios de custos', url: '/relatorios/custos', icon: 'bar-chart' },
        { categoria: 'Sistema', titulo: 'Previsão de demanda', url: '/relatorios/previsao', icon: 'crystal-ball' },
        { categoria: 'Sistema', titulo: 'B2B Indústria', url: '/b2b/', icon: 'building' },
        { categoria: 'Sistema', titulo: 'TO-DO', url: '/todo', icon: 'check2-square' },
        { categoria: 'Sistema', titulo: 'Atribuições', url: '/auth/painel', icon: 'diagram-2' },
        { categoria: 'Sistema', titulo: 'Usuários', url: '/auth/usuarios', icon: 'person-badge' },
        { categoria: 'Sistema', titulo: 'Audit log', url: '/audit', icon: 'shield-check' },
        { categoria: 'Sistema', titulo: 'Slack bot', url: '/slack/install', icon: 'slack' },

        // Ações rápidas
        { categoria: 'Ações', titulo: 'Novo pedido', url: '/pedidos/novo', icon: 'plus-circle', acao: true },
        { categoria: 'Ações', titulo: 'Lançar ponto', url: '/rh/ponto', icon: 'plus-circle', acao: true },
        { categoria: 'Ações', titulo: 'Modo padeiro', url: '/receitas/padeiro', icon: 'eyeglasses' },
        { categoria: 'Ações', titulo: 'Exportar JSON', url: '/api/exportar', icon: 'download' },
        { categoria: 'Ações', titulo: 'Sair', url: '/auth/logout', icon: 'box-arrow-right' },
    ];

    // Receitas vêm injetadas via base.html (variável global RECEITA_NOMES)
    const receitasDinamicas = (window.RECEITA_NOMES || []).map((n, idx) => ({
        categoria: 'Receitas',
        titulo: n,
        // Não temos o ID, então pesquisa via lista de receitas (TODO: melhorar com /receitas/buscar)
        url: '/receitas/?busca=' + encodeURIComponent(n),
        icon: 'journal-text',
    }));

    const TODAS = ROTAS.concat(receitasDinamicas);

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
                    <input type="text" id="cmdk-input" placeholder="Buscar… (ex: novo pedido, fornecedores, sourdough)"
                           autocomplete="off" spellcheck="false">
                    <kbd>esc</kbd>
                </div>
                <div id="cmdk-results" role="listbox"></div>
                <div id="cmdk-footer">
                    <span><kbd>↑</kbd><kbd>↓</kbd> navegar</span>
                    <span><kbd>↵</kbd> abrir</span>
                    <span><kbd>esc</kbd> fechar</span>
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
            resultadosVisiveis = TODAS.filter((i) => i.acao || i.categoria === 'Navegar').slice(0, 12);
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
        if (titulo === q) return 100;
        if (titulo.startsWith(q)) return 80;
        if (titulo.includes(q)) return 50;
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
            html += `<div class="cmdk-cat">${cat}</div>`;
            for (const it of agrupado[cat]) {
                const sel = it.idx === selecionadoIdx ? 'sel' : '';
                html += `<a href="${it.url}" class="cmdk-item ${sel}" data-idx="${it.idx}">
                    <i class="bi bi-${it.icon}"></i>
                    <span>${it.titulo}</span>
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
