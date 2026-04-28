/* Gestao de Projetos - inline editing + weekly review */
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

    // Status circle das tarefas
    document.body.addEventListener('click', function (e) {
        var btn = e.target.closest('.proj-tarefa-status');
        if (!btn) return;
        var tid = btn.dataset.tarefaId;
        var atual = ['a_fazer','fazendo','feito','cancelado']
            .find(function(c){ return btn.classList.contains(c); }) || 'a_fazer';
        var prox = STATUS_CICLO[atual];
        postEdicao('/projetos/tarefa/' + tid + '/editar', 'status', prox).then(function(r){
            if (r.ok) {
                aplicarStatusVisual(btn, prox);
                // Soft-reload: se mudou pra "feito" no kanban, recarrega pra mover de coluna
                if (document.querySelector('.proj-kanban')) location.reload();
            }
        });
    });

    // Foco 12s star toggle
    document.body.addEventListener('click', function (e) {
        var btn = e.target.closest('.proj-foco-toggle');
        if (!btn) return;
        var pid = btn.dataset.projetoId;
        var ativo = !btn.classList.contains('inactive');
        var novo = ativo ? '0' : '1';
        postEdicao('/projetos/projeto/' + pid + '/editar', 'foco_12s', novo).then(function(r){
            if (r.ok) {
                btn.classList.toggle('inactive');
                var icon = btn.querySelector('i');
                if (icon) {
                    icon.classList.toggle('bi-star-fill');
                    icon.classList.toggle('bi-star');
                }
            }
        });
    });

    // Edicao inline de selects (status / prioridade do projeto)
    document.body.addEventListener('change', function (e) {
        var sel = e.target.closest('.proj-edit');
        if (sel) {
            var pid = sel.dataset.projetoId;
            var campo = sel.dataset.campo;
            postEdicao('/projetos/projeto/' + pid + '/editar', campo, sel.value);
        }
        var prazo = e.target.closest('.tarefa-prazo');
        if (prazo) {
            var tid = prazo.dataset.tarefaId;
            postEdicao('/projetos/tarefa/' + tid + '/editar', 'prazo', prazo.value);
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

    // Modal "nova tarefa" — popula projeto_id
    document.body.addEventListener('click', function (e) {
        var btn = e.target.closest('.btn-add-tarefa');
        if (!btn) return;
        document.getElementById('modal-tarefa-projeto-id').value = btn.dataset.projetoId;
        document.getElementById('modal-tarefa-projeto-nome').value = btn.dataset.projetoNome;
        new bootstrap.Modal(document.getElementById('modal-nova-tarefa')).show();
    });

    // Weekly Review — carrega via fetch ao abrir
    var btnWeekly = document.getElementById('modal-weekly');
    if (btnWeekly) {
        btnWeekly.addEventListener('show.bs.modal', function () {
            var body = document.getElementById('weekly-body');
            body.innerHTML = '<p class="text-muted small">Carregando…</p>';
            fetch('/projetos/weekly').then(function(r){ return r.json(); }).then(function(data){
                body.innerHTML = montarWeekly(data);
            });
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
            return '<li><strong>' + escapeHtml(t.nome) + '</strong> <span class="text-muted">— ' + escapeHtml(t.projeto) + ' (prazo: ' + (t.prazo || '?') + ')</span></li>';
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
        html += '<div class="text-muted small">Recomendado: 3-5 projetos. Excedeu? Tire estrelas que não são mais prioridade.</div>';
        html += listaItens(d.foco, function(p){ return '<li>' + escapeHtml(p.nome) + '</li>'; });
        html += '</div></div>';

        html += '<div class="card mb-0"><div class="card-body py-2"><strong>6. Reflexão (3 linhas)</strong>';
        html += '<textarea class="form-control form-control-sm mt-2" rows="3" placeholder="O que funcionou / o que não / o que ajustar… (apenas pessoal — não é salvo no sistema)"></textarea>';
        html += '</div></div>';

        return html;
    }

    function escapeHtml(s) {
        var d = document.createElement('div');
        d.textContent = s == null ? '' : s;
        return d.innerHTML;
    }
})();
