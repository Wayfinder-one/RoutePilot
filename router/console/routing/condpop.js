(function (root) { 'use strict'; root.RTCondPop = {
  open(edgeId) { const el = document.getElementById('cvCondPop'); el.dataset.edgeId = edgeId; el.hidden = false; },
  close() { document.getElementById('cvCondPop').hidden = true; },
}; })(window);
