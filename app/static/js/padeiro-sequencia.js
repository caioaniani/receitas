/* Consulta por produto na TV. Mudar de produto nunca registra produção. */
(() => {
  'use strict';
  const workspace = document.getElementById('sequencia');
  if (!workspace) return;
  const panels = [...workspace.querySelectorAll('.product-panel')];
  const choices = [...workspace.querySelectorAll('.product-choice')];
  const details = workspace.querySelector('.product-details');
  function showSteps(panel, page) {
    const controls = panel.querySelector('.step-pages');
    if (!controls) return;
    const steps = [...panel.querySelectorAll('.steps > .step')];
    const lastPage = Math.ceil(steps.length / 6) - 1;
    page = Math.max(0, Math.min(lastPage, page));
    panel.dataset.stepPage = String(page);
    steps.forEach((step, index) => { step.hidden = index < page * 6 || index >= (page + 1) * 6; });
    controls.hidden = false;
    const first = page * 6 + 1;
    const last = Math.min((page + 1) * 6, steps.length);
    controls.querySelector('.step-range').textContent = first === last
      ? `Etapa ${first} de ${steps.length}`
      : `Etapas ${first}–${last} de ${steps.length}`;
    controls.querySelector('[data-step-page="-1"]').disabled = page === 0;
    controls.querySelector('[data-step-page="1"]').disabled = page === lastPage;
  }
  function showProduct(id) {
    const selected = panels.find(panel => panel.id === id) || panels[0];
    if (!selected) return;
    showSteps(selected, 0);
    panels.forEach(panel => { panel.hidden = panel !== selected; });
    choices.forEach(choice => {
      if (choice.dataset.product === selected.id) {
        choice.setAttribute('aria-current', 'true');
        choice.scrollIntoView({block: 'nearest', inline: 'nearest'});
      }
      else choice.removeAttribute('aria-current');
    });
    details.scrollTop = 0;
  }
  workspace.addEventListener('click', event => {
    const stepButton = event.target.closest('button[data-step-page]');
    if (stepButton) {
      const panel = stepButton.closest('.product-panel');
      showSteps(panel, Number(panel.dataset.stepPage || 0) + Number(stepButton.dataset.stepPage));
      return;
    }
    const link = event.target.closest('a[data-product]');
    if (!link || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    history.pushState(null, '', '#' + link.dataset.product);
    showProduct(link.dataset.product);
    // O link "Ver próximo" some junto do painel: devolve o foco à seleção.
    if (link.classList.contains('next-product')) {
      const choice = choices.find(item => item.dataset.product === link.dataset.product);
      if (choice) {
        choice.focus({preventScroll: true});
      }
    }
  });
  window.addEventListener('hashchange', () => showProduct(location.hash.slice(1)));
  window.addEventListener('popstate', () => showProduct(location.hash.slice(1)));
  document.body.classList.add('seq-ready');
  showProduct(location.hash.slice(1));
})();
