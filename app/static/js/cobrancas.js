/* Confirma o destinatário efetivamente digitado, nunca o e-mail antigo. */
(() => {
    const form = document.getElementById('cob-send-form');
    if (!form) return;
    const cc = document.getElementById('cob-cc');
    const copias = cc ? JSON.parse(cc.dataset.copias || '[]') : [];
    const bcc = document.getElementById('cob-bcc');
    const ocultas = bcc ? JSON.parse(bcc.dataset.copias || '[]') : [];
    const copiasEfetivas = () => copias.filter(endereco =>
        endereco.toLowerCase() !== form.elements.email.value.trim().toLowerCase());
    const atualizarCopias = () => {
        const lista = document.getElementById('cob-cc-lista');
        if (lista) lista.textContent = copiasEfetivas().join(', ') || 'Nenhum e-mail adicional.';
        const listaOcultas = document.getElementById('cob-bcc-lista');
        const visiveis = [form.elements.email.value.trim(), ...copiasEfetivas()]
            .map(endereco => endereco.toLowerCase());
        if (listaOcultas) listaOcultas.textContent = ocultas.filter(endereco =>
            !visiveis.includes(endereco.toLowerCase())).join(', ')
            || 'Nenhuma — os endereços internos já estão entre os destinatários.';
    };
    form.elements.email.addEventListener('input', atualizarCopias);
    atualizarCopias();
    let enviando = false;
    form.addEventListener('submit', event => {
        if (enviando) { event.preventDefault(); return; }
        const email = form.elements.email.value.trim();
        const adicionais = copiasEfetivas();
        const copiaTexto = adicionais.length ? `\nEm cópia (CC): ${adicionais.join(', ')}.` : '';
        if (!window.confirm(`Enviar a NF e o boleto, juntos em um único e-mail, para ${email}?${copiaTexto}`)) {
            event.preventDefault(); return;
        }
        enviando = true;
        const button = document.getElementById('cob-send-button');
        button.disabled = true;
        button.textContent = 'Enviando os dois documentos…';
    });
    window.addEventListener('pageshow', event => {
        if (event.persisted) window.location.reload();
    });
})();

/* Download direto da lista. Confirmar registro não altera o status no banco. */
(() => {
    document.querySelectorAll('a[data-cob-confirmar-banco]').forEach(link => {
        link.addEventListener('click', event => {
            event.preventDefault();
            if (!window.confirm('Você conferiu no Sicredi que este boleto já foi registrado? O ERP ainda só registra a geração da remessa.')) return;
            const destino = new URL(link.href, window.location.href);
            destino.searchParams.set('banco_confirmado', '1');
            window.location.assign(destino.href);
        });
    });
})();
