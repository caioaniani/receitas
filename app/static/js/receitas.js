document.addEventListener('DOMContentLoaded', function () {
    const btnAdd = document.getElementById('btn-add-ingrediente');
    const tbody = document.getElementById('ingredientes-body');
    const template = document.getElementById('ingrediente-template');
    const categoriaSelect = document.getElementById('categoria-select');

    if (!btnAdd || !tbody || !template) return;

    // Adicionar ingrediente
    btnAdd.addEventListener('click', function () {
        const clone = template.content.cloneNode(true);
        const rowCount = tbody.querySelectorAll('.ingrediente-row').length;

        // Atualizar índice do checkbox eh_base
        const checkbox = clone.querySelector('.base-check');
        if (checkbox) {
            checkbox.value = rowCount;
        }

        tbody.appendChild(clone);
        updateBaseIndices();
    });

    // Remover ingrediente (delegação de evento)
    tbody.addEventListener('click', function (e) {
        const btn = e.target.closest('.btn-remove-ingrediente');
        if (btn) {
            const rows = tbody.querySelectorAll('.ingrediente-row');
            if (rows.length > 1) {
                btn.closest('.ingrediente-row').remove();
                updateBaseIndices();
            } else {
                alert('A receita precisa ter pelo menos um ingrediente.');
            }
        }
    });

    // Atualizar índices dos checkboxes eh_base
    function updateBaseIndices() {
        const rows = tbody.querySelectorAll('.ingrediente-row');
        rows.forEach(function (row, index) {
            const checkbox = row.querySelector('.base-check');
            if (checkbox) {
                checkbox.value = index;
            }
        });
    }

    // Mostrar/ocultar coluna "Base" baseado na categoria
    function toggleBaseColumn() {
        const isPao = categoriaSelect && categoriaSelect.value === 'pao';
        document.querySelectorAll('.col-base').forEach(function (el) {
            el.style.display = isPao ? '' : 'none';
        });
    }

    if (categoriaSelect) {
        categoriaSelect.addEventListener('change', toggleBaseColumn);
        toggleBaseColumn();
    }
});
