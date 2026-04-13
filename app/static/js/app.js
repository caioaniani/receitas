/* ═══════════════════════════════════════════
   Padaria — JavaScript Principal
   ═══════════════════════════════════════════ */

document.addEventListener('DOMContentLoaded', function () {

    // ═══ FICHA TÉCNICA ═══
    const fichaBody = document.getElementById('ficha-body');
    const pesoBaseInput = document.getElementById('peso-base');
    const rendimentoInput = document.getElementById('rendimento-qtd');
    const ingTemplate = document.getElementById('ing-row-template');
    const btnAddIng = document.getElementById('btn-add-ing');

    if (fichaBody && pesoBaseInput) {
        // Recalcular tudo quando peso base muda
        pesoBaseInput.addEventListener('input', recalcularTudo);
        rendimentoInput.addEventListener('input', recalcularTudo);

        // Delegação de eventos para inputs dinâmicos
        fichaBody.addEventListener('input', function (e) {
            if (e.target.classList.contains('pct-input') || e.target.classList.contains('nome-input')) {
                recalcularTudo();
            }
        });

        // Remover ingrediente
        fichaBody.addEventListener('click', function (e) {
            const btn = e.target.closest('.btn-remove-ing');
            if (btn) {
                btn.closest('.ingrediente-row').remove();
                recalcularTudo();
            }
        });

        // Adicionar ingrediente
        if (btnAddIng && ingTemplate) {
            btnAddIng.addEventListener('click', function () {
                const clone = ingTemplate.content.cloneNode(true);
                fichaBody.appendChild(clone);
            });
        }

        // Calcular ao carregar
        recalcularTudo();
    }

    function recalcularTudo() {
        const pesoBase = parseFloat(pesoBaseInput.value) || 0;
        const rendimento = parseFloat(rendimentoInput.value) || 1;
        let totalPct = 0;
        let totalQtd = 0;
        let totalCusto = 0;

        document.querySelectorAll('.ingrediente-row').forEach(function (row) {
            const nomeInput = row.querySelector('.nome-input');
            const pctInput = row.querySelector('.pct-input');
            const qtdCell = row.querySelector('.qtd-calc');
            const custoKgCell = row.querySelector('.custo-kg-calc');
            const custoRsCell = row.querySelector('.custo-rs-calc');

            const nome = nomeInput ? nomeInput.value.trim() : '';
            const pct = parseFloat(pctInput ? pctInput.value : 0) || 0;

            // Qtd (g) = peso_base × % / 100
            const qtd = pesoBase * pct / 100;

            // Custo/kg do banco de MP
            const mp = MP_DATA[nome];
            const custoKg = mp ? mp.custo_por_kg : 0;

            // Custo R$ = qtd / 1000 × custo/kg
            const custoRs = qtd / 1000 * custoKg;

            // Atualizar células
            qtdCell.textContent = qtd > 0 ? formatNum(qtd, 1) : '-';
            custoKgCell.textContent = mp ? formatBRL(custoKg) : '-';
            custoKgCell.className = mp ? 'custo-kg-calc valor-mp text-end' : 'custo-kg-calc text-end text-muted';
            custoRsCell.textContent = custoRs > 0 ? formatBRL(custoRs) : '-';

            totalPct += pct;
            totalQtd += qtd;
            totalCusto += custoRs;
        });

        // Totais na tabela
        var elPct = document.getElementById('total-pct');
        var elQtd = document.getElementById('total-qtd');
        var elCusto = document.getElementById('total-custo');
        if (elPct) elPct.textContent = formatNum(totalPct, 1) + '%';
        if (elQtd) elQtd.textContent = formatNum(totalQtd, 0) + 'g';
        if (elCusto) elCusto.textContent = formatBRL(totalCusto);

        // Resumo
        var rPeso = document.getElementById('resumo-peso');
        var rCusto = document.getElementById('resumo-custo');
        var rUn = document.getElementById('resumo-unidades');
        var rCustoUn = document.getElementById('resumo-custo-un');
        if (rPeso) rPeso.textContent = formatNum(totalQtd, 0) + 'g';
        if (rCusto) rCusto.textContent = formatBRL(totalCusto);
        if (rUn) rUn.textContent = rendimento;
        if (rCustoUn) rCustoUn.textContent = rendimento > 0 ? formatBRL(totalCusto / rendimento) : '-';
    }


    // ═══ BANCO DE MP ═══
    const mpBody = document.getElementById('mp-body');
    const mpTemplate = document.getElementById('mp-row-template');
    const btnAddMp = document.getElementById('btn-add-mp');

    if (mpBody) {
        // Adicionar linha
        if (btnAddMp && mpTemplate) {
            btnAddMp.addEventListener('click', function () {
                const clone = mpTemplate.content.cloneNode(true);
                mpBody.appendChild(clone);
            });
        }

        // Excluir MP existente (via POST)
        mpBody.addEventListener('click', function (e) {
            const btn = e.target.closest('.btn-del-mp');
            if (btn) {
                const mpId = btn.dataset.id;
                if (confirm('Excluir esta matéria-prima?')) {
                    const form = document.createElement('form');
                    form.method = 'POST';
                    form.action = '/materias-primas/excluir/' + mpId;
                    const csrf = document.createElement('input');
                    csrf.type = 'hidden';
                    csrf.name = 'csrf_token';
                    csrf.value = CSRF_TOKEN;
                    form.appendChild(csrf);
                    document.body.appendChild(form);
                    form.submit();
                }
            }

            // Remover linha nova (ainda não salva)
            const btnNew = e.target.closest('.btn-del-new');
            if (btnNew) {
                btnNew.closest('tr').remove();
            }
        });
    }


    // ═══ IMPORTAR JSON ═══
    const importInput = document.getElementById('import-file');
    if (importInput) {
        importInput.addEventListener('change', function () {
            const file = this.files[0];
            if (!file) return;

            if (!confirm('Isso vai SUBSTITUIR todos os dados atuais pelo conteúdo do arquivo. Continuar?')) {
                this.value = '';
                return;
            }

            const formData = new FormData();
            formData.append('file', file);

            fetch('/api/importar', {
                method: 'POST',
                body: formData,
                headers: { 'X-CSRFToken': CSRF_TOKEN }
            })
            .then(function (resp) { return resp.json(); })
            .then(function (data) {
                if (data.success) {
                    alert('Dados importados com sucesso!');
                    window.location.href = '/';
                } else {
                    alert('Erro: ' + (data.error || 'Falha na importação'));
                }
            })
            .catch(function () {
                alert('Erro de conexão ao importar.');
            });

            this.value = '';
        });
    }


    // ═══ HELPERS ═══
    function formatBRL(value) {
        return 'R$ ' + value.toFixed(2).replace('.', ',');
    }

    function formatNum(value, decimals) {
        return value.toFixed(decimals).replace('.', ',');
    }
});
