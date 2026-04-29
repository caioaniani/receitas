/* Gestao de Projetos - inline editing, drag-and-drop, weekly review */
(function () {
    var STATUS_CICLO = {
        'a_fazer': 'fazendo',
        'fazendo': 'feito',
        'feito':   'a_fazer',
        'cancelado': 'a_fazer'
    };

    function postEdicao(url, campo, valor) {
        var fd = new FormData();
        fd.append('campo', campo);
        fd.append('valor', valor);
        fd.append('csrf_token', CSRF_TOKEN);
        return fetch(url, { method: 'POST', body: fd })
            .then(function (r) { return r.json(); });
    }

    function aplicarStatusVisual(btn, novoStatus) {
        btn.classList.remove('a_fazer','fazendo','feito','cancelado');
        btn.classList.add(novoStatus);
        var simbolos = { 'a_fazer':'○', 'fazendo':'▶', 'feito':'✓', 'cancelado':'✕' };
        btn.textContent = simbolos[novoStatus] || '?';
    }

    function moverCardEntreColunas(card, novoStatus) {
        // Move o card entre colunas do kanban se houver matching coluna
        var col = document.querySelector('.proj-coluna-cards[data-status="' + novoStatus + '"]');
        if (!col) return;
        col.appendChild(card);
        // Atualiza data-tarefa pra refletir status novo
        try {
            var d = JSON.parse(card.dataset.tarefa || '{}');
            d.status = novoStatus;
            card.dataset.tarefa = JSON.stringify(d);
        } catch (e) {}
    }

    function atualizarContadores() {
        // Recalcula contadores dos headers das colunas do kanban
        document.querySelectorAll('.proj-coluna-cards').forEach(function (col) {
            var n = col.querySelectorAll('[data-tarefa-id]').length;
            var header = col.parentElement.querySelector('.proj-coluna-header');
            if (header) {
                var label = header.textContent.split('(')[0].trim();
                // Mantem qualquer sufixo como "WIP 3"
                var sufixo = '';
                var m = header.textContent.match(/(·.*)$/);
                if (m) sufixo = ' ' + m[1];
                header.firstChild.textContent = label + ' (' + n + ')';
            }
        });
    }

    // Status circle das tarefas — OPTIMISTIC: aplica visual antes da resposta
    document.body.addEventListener('click', function (e) {
        var btn = e.target.closest('.proj-tarefa-status');
        if (!btn) return;
        var tid = btn.dataset.tarefaId;
        var card = btn.closest('[data-tarefa-id]');
        var atual = ['a_fazer','fazendo','feito','cancelado']
            .find(function(c){ return btn.classList.contains(c); }) || 'a_fazer';
        var prox = STATUS_CICLO[atual];

        // 1) Optimistic: aplica visual IMEDIATAMENTE
        aplicarStatusVisual(btn, prox);
        // Se está num kanban, move card para a coluna nova (sem reload)
        if (card && card.classList.contains('proj-kanban-card')) {
            moverCardEntreColunas(card, prox);
            atualizarContadores();
        }

        // 2) Sincroniza em background
        postEdicao('/projetos/tarefa/' + tid + '/editar', 'status', prox).then(function (r) {
            if (!r.ok) {
                // Reverte se falhou
                aplicarStatusVisual(btn, atual);
                alert('Não foi possível atualizar a tarefa. Recarregue.');
            }
        }).catch(function () {
            aplicarStatusVisual(btn, atual);
        });
    });

    // Foco 12s star toggle — OPTIMISTIC
    document.body.addEventListener('click', function (e) {
        var btn = e.target.closest('.proj-foco-toggle');
        if (!btn) return;
        var pid = btn.dataset.projetoId;
        var ativo = !btn.classList.contains('inactive');
        var novo = ativo ? '0' : '1';

        // 1) Visual imediato
        btn.classList.toggle('inactive');
        var icon = btn.querySelector('i');
        if (icon) {
            icon.classList.toggle('bi-star-fill');
            icon.classList.toggle('bi-star');
        }

        // 2) Sync em background
        postEdicao('/projetos/projeto/' + pid + '/editar', 'foco_12s', novo).then(function (r) {
            if (!r.ok) {
                // Reverte
                btn.classList.toggle('inactive');
                if (icon) {
                    icon.classList.toggle('bi-star-fill');
                    icon.classList.toggle('bi-star');
                }
            }
        });
    });

    // Edicao inline de selects (status / prioridade do projeto, prazo da tarefa, DRI)
    document.body.addEventListener('change', function (e) {
        var sel = e.target.closest('.proj-edit');
        if (sel) {
            var pid = sel.dataset.projetoId;
            var campo = sel.dataset.campo;
            postEdicao('/projetos/projeto/' + pid + '/editar', campo, sel.value);
            return;
        }
        var prazo = e.target.closest('.tarefa-prazo');
        if (prazo) {
            var tid = prazo.dataset.tarefaId;
            postEdicao('/projetos/tarefa/' + tid + '/editar', 'prazo', prazo.value);
            return;
        }
        var dri = e.target.closest('.proj-dri');
        if (dri) {
            var url = dri.dataset.tipo === 'projeto'
                ? '/projetos/projeto/' + dri.dataset.id + '/editar'
                : '/projetos/tarefa/' + dri.dataset.id + '/editar';
            postEdicao(url, 'responsavel_id', dri.value);
            return;
        }
    });

    // Filtro por tipo de area (hierarquica)
    var filtros = document.getElementById('filtro-tipo');
    if (filtros) {
        filtros.addEventListener('click', function (e) {
            var btn = e.target.closest('button[data-tipo]');
            if (!btn) return;
            filtros.querySelectorAll('button').forEach(function(b){ b.classList.remove('active'); });
            btn.classList.add('active');
            var tipo = btn.dataset.tipo;
            document.querySelectorAll('.proj-area-block').forEach(function (block) {
                block.style.display = (!tipo || block.dataset.areaTipo === tipo) ? '' : 'none';
            });
        });
    }

    // Busca textual em tarefas
    var busca = document.getElementById('busca-tarefa');
    if (busca) {
        busca.addEventListener('input', function () {
            var termo = busca.value.toLowerCase().trim();
            document.querySelectorAll('.proj-tarefa').forEach(function(row) {
                var nome = (row.dataset.nome || '');
                row.style.display = (!termo || nome.indexOf(termo) !== -1) ? '' : 'none';
            });
        });
    }

    // Expand / collapse de projeto
    document.body.addEventListener('click', function (e) {
        var btn = e.target.closest('.proj-toggle');
        if (!btn) return;
        var pid = btn.dataset.projetoId;
        var wrap = document.querySelector('[data-projeto-tarefas="' + pid + '"]');
        var icon = btn.querySelector('i');
        if (wrap) {
            wrap.style.display = wrap.style.display === 'none' ? '' : 'none';
            if (icon) {
                icon.classList.toggle('bi-chevron-down');
                icon.classList.toggle('bi-chevron-right');
            }
        }
    });

    var btnCollapseAll = document.getElementById('btn-collapse-all');
    if (btnCollapseAll) {
        btnCollapseAll.addEventListener('click', function () {
            document.querySelectorAll('[data-projeto-tarefas]').forEach(function(w){ w.style.display = 'none'; });
            document.querySelectorAll('.proj-toggle i').forEach(function(i){
                i.classList.remove('bi-chevron-down'); i.classList.add('bi-chevron-right');
            });
        });
    }
    var btnExpandAll = document.getElementById('btn-expand-all');
    if (btnExpandAll) {
        btnExpandAll.addEventListener('click', function () {
            document.querySelectorAll('[data-projeto-tarefas]').forEach(function(w){ w.style.display = ''; });
            document.querySelectorAll('.proj-toggle i').forEach(function(i){
                i.classList.add('bi-chevron-down'); i.classList.remove('bi-chevron-right');
            });
        });
    }

    // Modal "nova tarefa" — popula projeto_id
    document.body.addEventListener('click', function (e) {
        var btn = e.target.closest('.btn-add-tarefa');
        if (!btn) return;
        document.getElementById('modal-tarefa-projeto-id').value = btn.dataset.projetoId;
        document.getElementById('modal-tarefa-projeto-nome').value = btn.dataset.projetoNome;
        new bootstrap.Modal(document.getElementById('modal-nova-tarefa')).show();
    });

    // ── Drag-and-drop kanban ──
    if (typeof Sortable !== 'undefined') {
        document.querySelectorAll('.proj-coluna-cards').forEach(function (coluna) {
            new Sortable(coluna, {
                group: 'kanban',
                animation: 150,
                ghostClass: 'proj-card-ghost',
                onEnd: function (evt) {
                    var card = evt.item;
                    var tid = card.dataset.tarefaId;
                    var novoStatus = evt.to.dataset.status;
                    if (!tid || !novoStatus) return;

                    // Optimistic: ja atualiza visual sem esperar resposta
                    var circle = card.querySelector('.proj-tarefa-status');
                    if (circle) aplicarStatusVisual(circle, novoStatus);
                    // Atualiza data-tarefa do card pra refletir status novo
                    try {
                        var d = JSON.parse(card.dataset.tarefa || '{}');
                        d.status = novoStatus;
                        card.dataset.tarefa = JSON.stringify(d);
                    } catch (e) {}
                    atualizarContadores();

                    // Sincroniza em background
                    var ids = Array.from(evt.to.querySelectorAll('[data-tarefa-id]'))
                        .map(function (el) { return el.dataset.tarefaId; });
                    var fd = new FormData();
                    fd.append('csrf_token', CSRF_TOKEN);
                    fd.append('status', novoStatus);
                    ids.forEach(function (id) { fd.append('ids[]', id); });
                    fetch('/projetos/tarefa/' + tid + '/mover', { method: 'POST', body: fd })
                        .then(function (r) { return r.json(); })
                        .then(function (r) {
                            if (!r.ok) {
                                alert('Não foi possível mover a tarefa. Recarregue.');
                            }
                        }).catch(function () {
                            alert('Erro de rede ao mover. Recarregue.');
                        });
                },
            });
        });
    }

    // ── Weekly Review ──
    var modalWeekly = document.getElementById('modal-weekly');
    if (modalWeekly) {
        modalWeekly.addEventListener('show.bs.modal', function () {
            var body = document.getElementById('weekly-body');
            body.innerHTML = '<p class="text-muted small">Carregando…</p>';
            fetch('/projetos/weekly').then(function(r){ return r.json(); }).then(function(data){
                body.innerHTML = montarWeekly(data);
                var btnSalvar = document.getElementById('weekly-salvar');
                if (btnSalvar) btnSalvar.addEventListener('click', salvarReflexao);
            });
        });
    }

    function salvarReflexao() {
        var ta = document.getElementById('weekly-reflexao');
        if (!ta || !ta.value.trim()) {
            alert('Escreva a reflexão antes de salvar.');
            return;
        }
        var fd = new FormData();
        fd.append('reflexao', ta.value.trim());
        fd.append('csrf_token', CSRF_TOKEN);
        fetch('/projetos/weekly/salvar', { method: 'POST', body: fd })
            .then(function(r){ return r.json(); })
            .then(function(r){
                if (r.ok) {
                    var ok = document.getElementById('weekly-ok');
                    if (ok) { ok.style.display = 'block'; setTimeout(function(){ ok.style.display='none'; }, 2500); }
                    ta.value = '';
                }
            });
    }

    function listaItens(items, render) {
        if (!items.length) return '<em class="text-muted small">Nenhum.</em>';
        return '<ul class="mb-0 small">' + items.map(render).join('') + '</ul>';
    }

    function montarWeekly(d) {
        var c = d.contadores;
        var html = '<p class="small text-muted mb-3">Roteiro de ~30 min, toda segunda. Use cada bloco abaixo pra fazer a varredura semanal.</p>';

        html += '<div class="card mb-2"><div class="card-body py-2"><strong>1. Tarefas atrasadas (' + d.atrasadas.length + ')</strong>';
        html += '<div class="text-muted small">Pra cada uma: faz hoje, move o prazo, ou cancela.</div>';
        html += listaItens(d.atrasadas, function(t){
            return '<li><strong>' + escapeHtml(t.nome) + '</strong> <span class="text-muted">— ' + escapeHtml(t.projeto) + ' (' + (t.relativa || t.prazo || '?') + ')</span></li>';
        });
        html += '</div></div>';

        html += '<div class="card mb-2"><div class="card-body py-2"><strong>2. WIP em "Fazendo" (' + d.fazendo.length + ' / limite ' + c.wip_limit + ')</strong>';
        if (c.wip_estourado) html += ' <span class="badge bg-danger">Acima do limite!</span>';
        html += listaItens(d.fazendo, function(t){
            return '<li>' + escapeHtml(t.nome) + ' <span class="text-muted">— ' + escapeHtml(t.projeto) + '</span></li>';
        });
        html += '</div></div>';

        html += '<div class="card mb-2"><div class="card-body py-2"><strong>3. Projetos Ativos sem DRI (' + d.sem_dri.length + ')</strong>';
        html += listaItens(d.sem_dri, function(p){ return '<li>' + escapeHtml(p.nome) + '</li>'; });
        html += '</div></div>';

        html += '<div class="card mb-2"><div class="card-body py-2"><strong>4. Projetos Ativos sem tarefa em A fazer/Fazendo (' + d.sem_tarefa.length + ')</strong>';
        html += '<div class="text-muted small">Crie a próxima ação ou mova pra "Em espera".</div>';
        html += listaItens(d.sem_tarefa, function(p){ return '<li>' + escapeHtml(p.nome) + '</li>'; });
        html += '</div></div>';

        html += '<div class="card mb-2"><div class="card-body py-2"><strong>5. Foco 12s ainda faz sentido? (' + d.foco.length + ')</strong>';
        html += '<div class="text-muted small">Recomendado: 3-5 projetos.' + (d.foco.length > 5 ? ' <span class="text-danger">Acima do recomendado!</span>' : '') + '</div>';
        html += listaItens(d.foco, function(p){ return '<li>' + escapeHtml(p.nome) + '</li>'; });
        html += '</div></div>';

        html += '<div class="card mb-2"><div class="card-body py-2"><strong>6. Reflexão da semana</strong>';
        html += '<textarea class="form-control form-control-sm mt-2" rows="4" id="weekly-reflexao" placeholder="O que funcionou / o que não / o que ajustar… (será salvo no histórico)"></textarea>';
        html += '<div class="d-flex gap-2 align-items-center mt-2">';
        html += '<button type="button" class="btn btn-warning btn-sm" id="weekly-salvar"><i class="bi bi-save"></i> Salvar reflexão</button>';
        html += '<span class="text-success small" id="weekly-ok" style="display:none;"><i class="bi bi-check-circle"></i> Salvo!</span>';
        html += '</div></div></div>';

        if (d.historico && d.historico.length) {
            html += '<div class="card mb-0"><div class="card-body py-2"><strong>Histórico (últimas ' + d.historico.length + ')</strong>';
            d.historico.forEach(function(r){
                html += '<div class="mt-2 pb-2 border-bottom">';
                html += '<div class="small text-muted">' + r.data + ' · ' + (r.autor || '') + ' · ' +
                    'F' + r.snapshot.fazendo + ' / A' + r.snapshot.a_fazer + ' / Atr' + r.snapshot.atrasadas + ' / ⭐' + r.snapshot.foco + '</div>';
                html += '<div class="small">' + escapeHtml(r.reflexao).replace(/\n/g, '<br>') + '</div>';
                html += '</div>';
            });
            html += '</div></div>';
        }

        return html;
    }

    function escapeHtml(s) {
        var d = document.createElement('div');
        d.textContent = s == null ? '' : s;
        return d.innerHTML;
    }

    // ── Pre-fetch ao hover em links de projeto/views ──
    // Chrome/Safari fazem cache automatico se a resposta tiver Cache-Control;
    // mesmo sem isso, a request fica "no aire" e quando o user clicar o
    // browser geralmente reaproveita.
    var prefetched = new Set();
    function prefetchURL(url) {
        if (!url || prefetched.has(url)) return;
        prefetched.add(url);
        fetch(url, { credentials: 'same-origin', method: 'GET' }).catch(function () {});
    }
    document.body.addEventListener('mouseover', function (e) {
        var link = e.target.closest('a[href]');
        if (!link) return;
        var href = link.getAttribute('href') || '';
        // Apenas links internos do modulo de projetos / detalhes
        if (!href.startsWith('/projetos/') && href !== '/projetos') return;
        // Ignora links que abrem em nova aba
        if (link.target === '_blank') return;
        prefetchURL(link.href);
    }, true);

    // ── Edição de tarefa via modal ──
    function preencherEAbrirModalEdicao(d) {
        document.getElementById('edt-id').value = d.id;
        document.getElementById('edt-nome').value = d.nome || '';
        document.getElementById('edt-status').value = d.status || 'a_fazer';
        document.getElementById('edt-tipo').value = d.tipo || '';
        document.getElementById('edt-esforco').value = d.esforco || '';
        document.getElementById('edt-prazo').value = d.prazo || '';
        document.getElementById('edt-recorrencia').value = d.recorrencia || '';
        document.getElementById('edt-responsavel').value = d.responsavel_id || '';
        document.getElementById('edt-observacao').value = d.observacao || '';
        var pr = document.getElementById('edt-projeto');
        if (pr) pr.textContent = d.projeto_nome || '';
        var modalEl = document.getElementById('modal-editar-tarefa');
        if (modalEl) new bootstrap.Modal(modalEl).show();
    }

    function abrirEdicaoTarefaCard(card) {
        // 1) Tenta dados embutidos no DOM (sem rede - instantaneo)
        if (card.dataset.tarefa) {
            try {
                preencherEAbrirModalEdicao(JSON.parse(card.dataset.tarefa));
                return;
            } catch (e) { /* fallback */ }
        }
        // 2) Fallback: fetch — abre modal com spinner enquanto busca
        var tid = card.dataset.tarefaId;
        if (!tid) return;
        var modalEl = document.getElementById('modal-editar-tarefa');
        if (modalEl) {
            // Limpa campos pra evitar dado fantasma do anterior
            ['edt-id','edt-nome','edt-prazo','edt-observacao'].forEach(function(id){
                var el = document.getElementById(id); if (el) el.value = '';
            });
            new bootstrap.Modal(modalEl).show();
        }
        fetch('/projetos/tarefa/' + tid + '/dados')
            .then(function (r) { return r.json(); })
            .then(preencherEAbrirModalEdicao);
    }

    // Click em qualquer nome de tarefa marcado com .tarefa-nome-editavel
    document.body.addEventListener('click', function (e) {
        var alvo = e.target.closest('.tarefa-nome-editavel');
        if (!alvo) return;
        var card = alvo.closest('[data-tarefa-id]');
        if (!card) return;
        e.preventDefault();
        e.stopPropagation();
        abrirEdicaoTarefaCard(card);
    });

    function atualizarCardTarefa(tid, dadosNovos) {
        // Encontra todos os cards/linhas dessa tarefa no DOM e atualiza
        var nodes = document.querySelectorAll('[data-tarefa-id="' + tid + '"]');
        nodes.forEach(function (card) {
            // Atualiza dataset.tarefa
            try {
                var dAtual = JSON.parse(card.dataset.tarefa || '{}');
                Object.assign(dAtual, dadosNovos);
                card.dataset.tarefa = JSON.stringify(dAtual);
            } catch (e) {}

            // Atualiza nome visivel
            var nomeEl = card.querySelector('.tarefa-nome-editavel');
            if (nomeEl && dadosNovos.nome) {
                // Preserva os badges, troca só o text node inicial
                var primeiro = nomeEl.firstChild;
                if (primeiro && primeiro.nodeType === 3) {
                    primeiro.nodeValue = dadosNovos.nome + ' ';
                } else {
                    nomeEl.textContent = dadosNovos.nome;
                }
            }

            // Atualiza status circle
            if (dadosNovos.status) {
                var circle = card.querySelector('.proj-tarefa-status');
                if (circle) aplicarStatusVisual(circle, dadosNovos.status);

                // Se está em kanban, move pra coluna nova
                if (card.classList.contains('proj-kanban-card')) {
                    moverCardEntreColunas(card, dadosNovos.status);
                }
            }
        });
        atualizarContadores();
    }

    // Submit do form de edição
    var formEditar = document.getElementById('form-editar-tarefa');
    if (formEditar) {
        formEditar.addEventListener('submit', function (e) {
            e.preventDefault();
            var tid = document.getElementById('edt-id').value;
            var fd = new FormData(formEditar);
            fd.append('csrf_token', CSRF_TOKEN);

            // Optimistic: fecha modal e atualiza DOM imediatamente
            var dadosNovos = {
                nome: document.getElementById('edt-nome').value,
                status: document.getElementById('edt-status').value,
                tipo: document.getElementById('edt-tipo').value,
                esforco: document.getElementById('edt-esforco').value,
                prazo: document.getElementById('edt-prazo').value,
                recorrencia: document.getElementById('edt-recorrencia').value,
                responsavel_id: document.getElementById('edt-responsavel').value,
                observacao: document.getElementById('edt-observacao').value,
            };
            bootstrap.Modal.getInstance(document.getElementById('modal-editar-tarefa')).hide();
            atualizarCardTarefa(tid, dadosNovos);

            // Sync em background
            fetch('/projetos/tarefa/' + tid + '/atualizar', {
                method: 'POST', body: fd,
            }).then(function (r) { return r.json(); }).then(function (r) {
                if (!r.ok) {
                    alert('Erro: ' + (r.erro || 'desconhecido') + '. Recarregue.');
                }
            });
        });
    }

    // Excluir do modal de edição — optimistic
    var btnExcluirEdt = document.getElementById('edt-excluir');
    if (btnExcluirEdt) {
        btnExcluirEdt.addEventListener('click', function () {
            if (!confirm('Excluir esta tarefa?')) return;
            var tid = document.getElementById('edt-id').value;

            // Remove visualmente antes da resposta
            bootstrap.Modal.getInstance(document.getElementById('modal-editar-tarefa')).hide();
            document.querySelectorAll('[data-tarefa-id="' + tid + '"]').forEach(function (n) {
                n.remove();
            });
            atualizarContadores();

            var fd = new FormData();
            fd.append('csrf_token', CSRF_TOKEN);
            fetch('/projetos/tarefa/' + tid + '/excluir', { method: 'POST', body: fd });
        });
    }

    // ── Recorrência (select inline) ──
    document.body.addEventListener('change', function (e) {
        var sel = e.target.closest('.proj-edit-tarefa');
        if (!sel) return;
        var tid = sel.dataset.tarefaId;
        var campo = sel.dataset.campo;
        postEdicao('/projetos/tarefa/' + tid + '/editar', campo, sel.value);
    });

    // ── Comentário (modal) ──
    document.body.addEventListener('click', function (e) {
        var btn = e.target.closest('.btn-comentario');
        if (!btn) return;
        document.getElementById('comentario-tipo').value = btn.dataset.tipo;
        document.getElementById('comentario-id').value = btn.dataset.id;
        document.getElementById('comentario-titulo').textContent = (btn.dataset.tipo === 'projeto' ? 'Projeto: ' : 'Tarefa: ') + btn.dataset.nome;
        document.getElementById('comentario-texto').value = btn.dataset.texto || '';
        var modalEl = document.getElementById('modal-comentario');
        if (modalEl) new bootstrap.Modal(modalEl).show();
    });
    var btnSalvarCom = document.getElementById('btn-salvar-comentario');
    if (btnSalvarCom) {
        btnSalvarCom.addEventListener('click', function () {
            var tipo = document.getElementById('comentario-tipo').value;
            var id = document.getElementById('comentario-id').value;
            var texto = document.getElementById('comentario-texto').value;
            var url = (tipo === 'projeto')
                ? '/projetos/projeto/' + id + '/editar'
                : '/projetos/tarefa/' + id + '/editar';
            postEdicao(url, 'observacao', texto).then(function (r) {
                if (r.ok) {
                    bootstrap.Modal.getInstance(document.getElementById('modal-comentario')).hide();
                    // Atualiza icone do botao (cheio se tem texto)
                    document.querySelectorAll('.btn-comentario[data-tipo="' + tipo + '"][data-id="' + id + '"]').forEach(function(b){
                        b.dataset.texto = texto;
                        var icon = b.querySelector('i');
                        if (icon) {
                            icon.classList.toggle('bi-chat-text-fill', !!texto.trim());
                            icon.classList.toggle('bi-chat-text', !texto.trim());
                        }
                    });
                }
            });
        });
    }

    // ── Atalhos de teclado ──
    var seqTimeout = null;
    var seq = '';
    document.addEventListener('keydown', function (e) {
        if (e.target.matches('input, textarea, select, [contenteditable]')) return;
        if (e.ctrlKey || e.metaKey || e.altKey) return;

        var key = e.key.toLowerCase();

        // Sequencias g+letra
        if (seq === 'g') {
            seq = '';
            clearTimeout(seqTimeout);
            var rotas = {
                'h': '/projetos/hoje',
                'p': '/projetos/',
                'k': '/projetos/kanban',
                'f': '/projetos/foco',
                'c': '/projetos/calendario',
                'r': '/projetos/relatorio',
                't': '/projetos/templates',
            };
            if (rotas[key]) { e.preventDefault(); location.href = rotas[key]; return; }
        }
        if (key === 'g') {
            seq = 'g';
            clearTimeout(seqTimeout);
            seqTimeout = setTimeout(function () { seq = ''; }, 1500);
            return;
        }

        // /  -> foca a busca
        if (key === '/') {
            var busca = document.getElementById('busca-tarefa');
            if (busca) { e.preventDefault(); busca.focus(); return; }
        }

        // n  -> nova tarefa (no primeiro projeto disponivel)
        if (key === 'n') {
            var primeiroProj = document.querySelector('.proj-card[data-projeto-id]');
            if (primeiroProj) {
                e.preventDefault();
                var pid = primeiroProj.dataset.projetoId;
                var nome = primeiroProj.querySelector('strong').textContent;
                var modal = document.getElementById('modal-nova-tarefa');
                if (modal) {
                    document.getElementById('modal-tarefa-projeto-id').value = pid;
                    document.getElementById('modal-tarefa-projeto-nome').value = nome;
                    new bootstrap.Modal(modal).show();
                }
            }
        }

        // ?  -> mostra ajuda de atalhos
        if (key === '?' || (e.shiftKey && key === '/')) {
            e.preventDefault();
            alert('Atalhos:\n\n/  → busca\nn  → nova tarefa\n\ng h  → Hoje\ng p  → Painel\ng k  → Kanban\ng f  → Foco 12s\ng c  → Calendário\ng r  → Relatório\ng t  → Templates');
        }
    });
})();
