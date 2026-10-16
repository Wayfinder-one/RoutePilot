/* Shared top bar: brand, page nav, language toggle. */
(function () {
  const page = document.body.dataset.page;
  const bar = document.createElement('header');
  bar.className = 'top';
  bar.innerHTML =
    `<div class="logo"><span class="mark">SR</span><span data-i18n="app.title"></span></div>
     <nav>${['overview', 'pipeline', 'lab', 'models'].map(p =>
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
