(function () {
  'use strict';

  var page = document.querySelector('.treino-admin');
  if (!page) return;

  var search = page.querySelector('#treino-conteudo-busca');
  var statusFilter = page.querySelector('#treino-conteudo-status');
  var results = page.querySelector('[data-admin-results]');
  var empty = page.querySelector('[data-admin-no-results]');
  var modules = Array.from(page.querySelectorAll('[data-admin-module]'));
  var previousOpen = null;

  function normalize(value) {
    return (value || '').normalize('NFD').replace(/[\u0300-\u036f]/g, '').toLowerCase().trim();
  }

  function filter() {
    var query = normalize(search.value);
    var status = statusFilter.value;
    var filtering = Boolean(query || status !== 'todos');
    var visibleModules = 0;
    var visibleLessons = 0;

    if (filtering && !previousOpen) previousOpen = modules.map(function (module) { return module.open; });

    modules.forEach(function (module, index) {
      var nameMatches = normalize(module.dataset.moduleName).includes(query);
      var lessons = Array.from(module.querySelectorAll('[data-admin-lesson]'));
      var matches = 0;
      lessons.forEach(function (lesson) {
        var matchesStatus = status === 'todos' || lesson.dataset.lessonStatus === status ||
          (status === 'processando' && lesson.dataset.processing === 'true');
        var matchesName = nameMatches || normalize(lesson.dataset.lessonName).includes(query);
        lesson.hidden = !(matchesStatus && matchesName);
        if (!lesson.hidden) matches += 1;
      });
      module.hidden = filtering && !matches && !(status === 'todos' && nameMatches);
      if (!module.hidden) {
        visibleModules += 1;
        visibleLessons += matches;
        if (filtering) module.open = true;
      }
      if (!filtering && previousOpen) module.open = previousOpen[index];
    });

    if (!filtering) previousOpen = null;
    results.hidden = !filtering;
    results.textContent = visibleModules + (visibleModules === 1 ? ' módulo' : ' módulos') + ' · ' +
      visibleLessons + (visibleLessons === 1 ? ' aula encontrada' : ' aulas encontradas');
    empty.hidden = !filtering || visibleModules > 0;
  }

  search.addEventListener('input', filter);
  statusFilter.addEventListener('change', filter);
  page.querySelector('[data-admin-clear]').addEventListener('click', function () {
    search.value = '';
    statusFilter.value = 'todos';
    filter();
    search.focus();
  });
  page.querySelector('[data-admin-filters]').hidden = false;

  function openHashTarget() {
    var id;
    try { id = decodeURIComponent(window.location.hash.slice(1)); } catch (error) { return; }
    var target = document.getElementById(id);
    if (!target || !page.contains(target) || target.tagName !== 'DETAILS') return;
    target.open = true;
  }
  page.querySelectorAll('[data-open-details]').forEach(function (link) {
    link.addEventListener('click', function () {
      var target = document.getElementById(link.dataset.openDetails);
      if (target) target.open = true;
    });
  });
  window.addEventListener('hashchange', openHashTarget);
  openHashTarget();

  page.querySelectorAll('.cargo-form').forEach(function (form) {
    var message = form.querySelector('.cargo-status');
    function save() {
      message.textContent = 'Salvando cargos…';
      var body = new FormData(form);
      body.append('ajax', '1');
      fetch(form.action, {
        method: 'POST',
        headers: {'X-CSRFToken': form.querySelector('[name=csrf_token]').value},
        body: body
      }).then(function (response) {
        if (!response.ok) throw new Error('Falha ao salvar');
        return response.json();
      }).then(function (result) {
        message.textContent = result.ok ? 'Cargos salvos.' : 'Não foi possível salvar. Use “Salvar cargos” para tentar novamente.';
      }).catch(function () {
        message.textContent = 'Não foi possível salvar. Use “Salvar cargos” para tentar novamente.';
      });
    }
    form.querySelectorAll('input[name=cargo_ids]').forEach(function (checkbox) {
      checkbox.addEventListener('change', save);
    });
  });
})();
