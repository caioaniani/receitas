/* ═══════════════════════════════════════════
   Padaria — JavaScript Principal
   ═══════════════════════════════════════════ */

document.addEventListener('DOMContentLoaded', function () {

    // ═══ FICHA TÉCNICA ═══
    const fichaBody = document.getElementById('ficha-body');
    const pesoBaseInput = document.getElementById('peso-base');
    const rendimentoInput = document.getElementById('rendimento-qtd');
    const pesoUnitarioInput = document.getElementById('peso-unitario');
    const modoSelect = document.getElementById('modo-lancamento');
    const ingTemplate = document.getElementById('ing-row-template');
    const btnAddIng = document.getElementById('btn-add-ing');

    if (fichaBody && pesoBaseInput) {
        // Eventos de input
        pesoBaseInput.addEventListener('input', recalcularTudo);
        rendimentoInput.addEventListener('input', recalcularTudo);
        if (pesoUnitarioInput) pesoUnitarioInput.addEventListener('input', recalcularTudo);

        // Trocar modo
        if (modoSelect) {
            modoSelect.addEventListener('change', function () {
                aplicarModo();
                recalcularTudo();
            });
            aplicarModo();
        }

        // Delegação de eventos para inputs dinâmicos
        fichaBody.addEventListener('input', function (e) {
            if (e.target.classList.contains('pct-input') || e.target.classList.contains('nome-input')) {
                recalcularTudo();
            }
        });

        // Remover ingrediente
        fichaBody.addEventListener('click', function (e) {
            var btn = e.target.closest('.btn-remove-ing');
            if (btn) {
                btn.closest('.ingrediente-row').remove();
                recalcularTudo();
            }
        });

        // Adicionar ingrediente
        if (btnAddIng && ingTemplate) {
            btnAddIng.addEventListener('click', function () {
                var clone = ingTemplate.content.cloneNode(true);
                // Respeitar estado do cadeado
                var pctInput = clone.querySelector('.pct-input');
                if (pctInput && !pctTravado) {
                    pctInput.readOnly = false;
                    pctInput.classList.remove('pct-locked');
                    pctInput.classList.add('pct-unlocked');
                }
                fichaBody.appendChild(clone);
            });
        }

        // Cadeado de % Padeiro
        var pctTravado = true;
        var btnLock = document.getElementById('btn-lock-pct');
        if (btnLock) {
            btnLock.addEventListener('click', function () {
                pctTravado = !pctTravado;
                var icon = btnLock.querySelector('i');
                if (pctTravado) {
                    icon.className = 'bi bi-lock-fill';
                    btnLock.classList.remove('unlocked');
                    btnLock.title = 'Destravar para editar receita';
                } else {
                    icon.className = 'bi bi-unlock-fill';
                    btnLock.classList.add('unlocked');
                    btnLock.title = 'Travar receita';
                }
                document.querySelectorAll('.pct-input').forEach(function (input) {
                    input.readOnly = pctTravado;
                    if (pctTravado) {
                        input.classList.add('pct-locked');
                        input.classList.remove('pct-unlocked');
                    } else {
                        input.classList.remove('pct-locked');
                        input.classList.add('pct-unlocked');
                    }
                });
            });
        }

        // Calcular ao carregar
        recalcularTudo();
    }

    function aplicarModo() {
        var modo = modoSelect ? modoSelect.value : 'farinha';
        var boxFarinha = document.getElementById('box-farinha');
        var boxQtd = document.getElementById('box-quantidade');

        if (modo === 'farinha') {
            // Farinha editável, quantidade calculada
            pesoBaseInput.readOnly = false;
            pesoBaseInput.classList.remove('calc-readonly');
            rendimentoInput.readOnly = true;
            rendimentoInput.classList.add('calc-readonly');
            if (boxFarinha) boxFarinha.style.order = '1';
            if (boxQtd) boxQtd.style.order = '2';
        } else {
            // Quantidade editável, farinha calculada
            pesoBaseInput.readOnly = true;
            pesoBaseInput.classList.add('calc-readonly');
            rendimentoInput.readOnly = false;
            rendimentoInput.classList.remove('calc-readonly');
            if (boxFarinha) boxFarinha.style.order = '2';
            if (boxQtd) boxQtd.style.order = '1';
        }
    }

    function recalcularTudo() {
        var modo = modoSelect ? modoSelect.value : 'farinha';
        var pesoUnit = pesoUnitarioInput ? (parseFloat(pesoUnitarioInput.value) || 0) : 0;

        // Passo 1: Soma das porcentagens (não depende de peso_base)
        var sumPct = 0;
        document.querySelectorAll('.ingrediente-row').forEach(function (row) {
            var pctInput = row.querySelector('.pct-input');
            sumPct += parseFloat(pctInput ? pctInput.value : 0) || 0;
        });

        // Passo 2: Determinar peso_base e rendimento conforme o modo
        var pesoBase, rendimento;

        if (modo === 'quantidade' && pesoUnit > 0 && sumPct > 0) {
            // Modo quantidade: rendimento é input, calcula peso_base
            rendimento = parseFloat(rendimentoInput.value) || 0;
            pesoBase = rendimento * pesoUnit * 100 / sumPct;
            pesoBaseInput.value = Math.round(pesoBase);
        } else {
            // Modo farinha (padrão): peso_base é input
            pesoBase = parseFloat(pesoBaseInput.value) || 0;
            rendimento = parseFloat(rendimentoInput.value) || 1;
        }

        // Passo 3: Calcular ingredientes
        var totalPct = 0;
        var totalQtd = 0;
        var totalCusto = 0;

        document.querySelectorAll('.ingrediente-row').forEach(function (row) {
            var nomeInput = row.querySelector('.nome-input');
            var pctInput = row.querySelector('.pct-input');
            var qtdCell = row.querySelector('.qtd-calc');
            var custoKgCell = row.querySelector('.custo-kg-calc');
            var custoRsCell = row.querySelector('.custo-rs-calc');

            var nome = nomeInput ? nomeInput.value.trim() : '';
            var pct = parseFloat(pctInput ? pctInput.value : 0) || 0;

            var qtd = pesoBase * pct / 100;
            var mp = MP_DATA[nome];
            var custoKg = mp ? mp.custo_por_kg : 0;
            var custoRs = qtd / 1000 * custoKg;

            qtdCell.textContent = qtd > 0 ? formatNum(qtd, 1) : '-';
            custoKgCell.textContent = mp ? formatBRL(custoKg) : '-';
            custoKgCell.className = mp ? 'custo-kg-calc valor-mp text-end' : 'custo-kg-calc text-end text-muted';
            custoRsCell.textContent = custoRs > 0 ? formatBRL(custoRs) : '-';

            totalPct += pct;
            totalQtd += qtd;
            totalCusto += custoRs;
        });

        // Passo 4: Se modo farinha e peso_unitario preenchido, calcular rendimento
        if (modo === 'farinha' && pesoUnit > 0 && totalQtd > 0) {
            rendimento = Math.floor(totalQtd / pesoUnit);
            rendimentoInput.value = rendimento;
        }

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
    var mpBody = document.getElementById('mp-body');
    var mpTemplate = document.getElementById('mp-row-template');
    var btnAddMp = document.getElementById('btn-add-mp');

    if (mpBody) {
        if (btnAddMp && mpTemplate) {
            btnAddMp.addEventListener('click', function () {
                var clone = mpTemplate.content.cloneNode(true);
                mpBody.appendChild(clone);
            });
        }

        mpBody.addEventListener('click', function (e) {
            var btn = e.target.closest('.btn-del-mp');
            if (btn) {
                var mpId = btn.dataset.id;
                if (confirm('Excluir esta matéria-prima?')) {
                    var form = document.createElement('form');
                    form.method = 'POST';
                    form.action = '/materias-primas/excluir/' + mpId;
                    var csrf = document.createElement('input');
                    csrf.type = 'hidden';
                    csrf.name = 'csrf_token';
                    csrf.value = CSRF_TOKEN;
                    form.appendChild(csrf);
                    document.body.appendChild(form);
                    form.submit();
                }
            }

            var btnNew = e.target.closest('.btn-del-new');
            if (btnNew) {
                btnNew.closest('tr').remove();
            }
        });
    }


    // ═══ IMPORTAR JSON ═══
    var importInput = document.getElementById('import-file');
    if (importInput) {
        importInput.addEventListener('change', function () {
            var file = this.files[0];
            if (!file) return;

            if (!confirm('Isso vai SUBSTITUIR todos os dados atuais pelo conteúdo do arquivo. Continuar?')) {
                this.value = '';
                return;
            }

            var formData = new FormData();
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
