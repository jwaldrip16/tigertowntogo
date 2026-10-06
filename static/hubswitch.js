// Shared Fleet Foot Driver / Kitchen apps: remember which picker sent us here and offer
// "Switch company", which signs out of this company and goes back to the picker.
(function(){
  var app = location.pathname.indexOf('/restaurant') === 0 ? 'kitchen' : 'driver';
  var logout = app === 'kitchen' ? '/restaurant/logout' : '/driver/logout';
  var HUB_KEY = 'ff_hub_' + app;
  try {
    var p = new URLSearchParams(location.search), hub = p.get('hub');
    if (hub && /^https:\/\/[a-z0-9.-]+(:\d+)?$/i.test(hub) || /^http:\/\/(localhost|127\.0\.0\.1)(:\d+)?$/.test(hub||'')) {
      localStorage.setItem(HUB_KEY, hub);
      ['hub','app','co'].forEach(function(k){ p.delete(k); });
      var qs = p.toString(); history.replaceState(null, '', location.pathname + (qs ? '?' + qs : ''));
    }
    hub = localStorage.getItem(HUB_KEY);
    if (!hub) return;
    function go(ev){
      if (ev) ev.preventDefault();
      if (!confirm('Sign out and switch to a different company?')) return;
      fetch(logout, {credentials:'same-origin'}).catch(function(){}).then(function(){
        try{ localStorage.removeItem(HUB_KEY); }catch(e){}
        location.href = hub + '/go/' + app + '?switch=1';
      });
    }
    function add(){
      if (document.getElementById('ffSwitch')) return;
      var a = document.createElement('a'); a.id='ffSwitch'; a.href='#'; a.textContent='Switch company'; a.onclick=go;
      var nav = document.querySelector('header.topbar nav');
      if (nav) { nav.appendChild(a); }
      else { a.style.cssText='display:block;text-align:center;margin:18px auto;color:#2563eb;font-size:15px'; document.body.appendChild(a); }
      var form = document.querySelector('.panel.narrow');
      if (form && !nav) form.appendChild(a);
    }
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', add); else add();
  } catch(e) {}
})();
