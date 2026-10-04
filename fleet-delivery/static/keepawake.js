/* Keeps the phone or tablet screen on while the app is open.
   Uses the browser's screen wake lock, and falls back to NoSleep (a tiny looping video)
   on phones that don't support it. The choice is saved on this device. */
(function(){
  var KEY = 'ff_awake', DEF = true, WL = null, NS = null, FORCE = 0;
  function on(){ var v = localStorage.getItem(KEY); return FORCE > 0 || (v === null ? DEF : v === '1'); }
  async function grab(){
    if (!on() || document.visibilityState !== 'visible') return;
    if ('wakeLock' in navigator){
      try {
        if (!WL){ WL = await navigator.wakeLock.request('screen');
                  WL.addEventListener('release', function(){ WL = null; }); }
        return;
      } catch(e){ WL = null; }
    }
    try { if (window.NoSleep){ if (!NS) NS = new NoSleep(); if (!NS.isEnabled) NS.enable(); } } catch(e){}
  }
  function drop(){
    try { if (WL){ WL.release(); WL = null; } } catch(e){}
    try { if (NS && NS.isEnabled) NS.disable(); } catch(e){}
  }
  function paint(){
    var v = localStorage.getItem(KEY), mine = (v === null ? DEF : v === '1');
    document.querySelectorAll('[data-awake-btn]').forEach(function(b){
      b.textContent = 'Keep screen awake: ' + (mine ? 'On' : 'Off');
      b.classList.toggle('primary', mine);
    });
  }
  window.ffAwake = {
    init: function(app, dflt){
      KEY = 'ff_awake_' + app; DEF = (dflt !== false && dflt !== 0 && dflt !== '0');
      document.addEventListener('visibilitychange', function(){
        if (document.visibilityState === 'visible'){ WL = null; grab(); } });
      // the fallback needs a tap before it can start, so any tap re-arms it
      document.addEventListener('click', grab, true);
      document.addEventListener('touchend', grab, true);
      setInterval(grab, 10000);
      paint(); grab();
    },
    toggle: function(){
      var v = localStorage.getItem(KEY), mine = (v === null ? DEF : v === '1');
      localStorage.setItem(KEY, mine ? '0' : '1');
      if (on()) grab(); else drop();
      paint(); return !mine;
    },
    hold: function(yes){ FORCE = Math.max(0, FORCE + (yes ? 1 : -1)); if (on()) grab(); else drop(); },
    check: grab,
    enabled: on
  };
})();
