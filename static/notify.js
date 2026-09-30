// Chrome/Edge/Safari desktop notifications for the driver and kitchen apps.
// Nothing fires until the user hits Allow.
(function(){
  const OK = ('Notification' in window);
  function state(){ return OK ? Notification.permission : 'unsupported'; }
  function ask(){
    if (!OK) return Promise.resolve('unsupported');
    if (Notification.permission === 'granted') return Promise.resolve('granted');
    return Notification.requestPermission();
  }
  function show(title, body, tag){
    if (!OK || Notification.permission !== 'granted') return null;
    try {
      const n = new Notification(title, {body: body || '', tag: tag || 'ttg',
                                         icon: '/static/icon-192.png', renotify: true});
      n.onclick = function(){ window.focus(); this.close(); };
      setTimeout(() => { try { n.close(); } catch(e){} }, 20000);
      return n;
    } catch(e){ return null; }
  }
  // wire a button + a note to the permission state
  function button(btnId, noteId){
    const b = document.getElementById(btnId), note = document.getElementById(noteId);
    function paint(){
      const s = state();
      if (!b) return;
      if (s === 'granted'){ b.classList.add('hidden'); if (note) note.textContent = 'Pop-up alerts are on.'; }
      else if (s === 'denied'){ b.classList.add('hidden');
        if (note) note.textContent = 'Pop-up alerts are blocked in this browser. Turn them back on in the padlock menu next to the address.'; }
      else { b.classList.remove('hidden'); if (note) note.textContent = ''; }
    }
    if (b) b.onclick = () => ask().then(paint);
    paint();
  }
  window.ffNotify = {ask: ask, show: show, state: state, button: button, supported: OK};
})();
