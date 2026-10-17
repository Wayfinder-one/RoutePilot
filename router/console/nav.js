/* Shared top bar: brand, 5-page nav, language toggle. */
(function () {
  const page = document.body.dataset.page;
  const bar = document.createElement('header');
  bar.className = 'top';
  bar.innerHTML =
    `<a class="brand" href="index.html"><span class="mark">SR</span><span data-i18n="app.title"></span></a>
     <nav>${['overview', 'pipeline', 'logs', 'signals', 'models'].map(p =>
       `<a href="${p === 'overview' ? 'index' : p}.html"
           class="${p === page ? 'on' : ''}" data-i18n="nav.${p}"></a>`).join('')}</nav>
     <span class="spacer"></span>
     <span class="langtog">
       <button data-lang="zh" onclick="setLang('zh')">中文</button>
       <button data-lang="en" onclick="setLang('en')">EN</button>
     </span>`;
  document.body.prepend(bar);
  setLang(LANG);
})();
