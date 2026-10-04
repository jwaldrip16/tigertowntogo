/* Shared audible alert. Uses the Web Audio API so there is no sound file to ship. */
(function(){
  var ctx = null, armed = false;
  var enabled = localStorage.getItem('ff_sound') !== 'off';

  var pending = null;   // a sound asked for while the browser still had audio locked
  function running(){ return !!ctx && ctx.state === 'running'; }
  function make(){
    if (ctx) return;
    try { ctx = new (window.AudioContext || window.webkitAudioContext)(); } catch(e){}
  }
  // Browsers keep audio locked until someone clicks or types on the page. Every click
  // re-wakes it, even if an alert tried to play earlier and left it asleep.
  function unlock(){
    make();
    if (!ctx) return;
    var after = function(){
      armed = running();
      if (armed && pending){ var p = pending; pending = null; window.ffSound.play(p); }
      try { document.dispatchEvent(new Event('ffsound')); } catch(e){}
    };
    if (ctx.state !== 'running'){ try { ctx.resume().then(after, after); } catch(e){ after(); } }
    else after();
  }
  ['pointerdown','click','keydown','touchstart'].forEach(function(ev){
    document.addEventListener(ev, unlock, {capture:true});
  });
  document.addEventListener('visibilitychange', function(){
    if (!document.hidden && ctx && ctx.state !== 'running'){ try { ctx.resume().then(function(){ armed = running(); }); } catch(e){} }
  });

  function beep(freq, start, len, vol){
    var o = ctx.createOscillator(), g = ctx.createGain();
    o.type = 'sine'; o.frequency.value = freq;
    g.gain.setValueAtTime(0.0001, ctx.currentTime + start);
    g.gain.exponentialRampToValueAtTime(vol, ctx.currentTime + start + 0.02);
    g.gain.exponentialRampToValueAtTime(0.0001, ctx.currentTime + start + len);
    o.connect(g); g.connect(ctx.destination);
    o.start(ctx.currentTime + start); o.stop(ctx.currentTime + start + len + 0.05);
  }

  var loopTimer = null;

  window.ffSound = {
    // keeps chiming every few seconds until stopLoop() is called
    startLoop: function(everyMs){
      if (loopTimer) return;
      window.ffSound.play('order');
      loopTimer = setInterval(function(){ window.ffSound.play('order'); }, everyMs || 5000);
    },
    stopLoop: function(){
      if (loopTimer){ clearInterval(loopTimer); loopTimer = null; }
    },
    looping: function(){ return loopTimer !== null; },
    // pattern: 'order' = three rising tones, 'ping' = single tone
    play: function(pattern){
      if (!enabled) return;
      make();
      if (!ctx) return;
      if (ctx.state !== 'running'){
        // try to wake it; if the browser still says no, play it on the next click
        try { ctx.resume(); } catch(e){}
        if (ctx.state !== 'running'){ pending = pattern || 'order'; armed = false; return; }
      }
      armed = true;
      if (pattern === 'ping'){ beep(880, 0, 0.18, 0.25); return; }
      if (pattern === 'call'){
        // phone style double ring, loud enough to hear across the room
        beep(988, 0.00, 0.22, 0.45); beep(784, 0.25, 0.22, 0.45);
        beep(988, 0.60, 0.22, 0.45); beep(784, 0.85, 0.22, 0.45);
        if (navigator.vibrate) navigator.vibrate([200, 100, 200]);
        return;
      }
      beep(660, 0.00, 0.16, 0.3);
      beep(880, 0.20, 0.16, 0.3);
      beep(1175, 0.40, 0.30, 0.3);
      if (navigator.vibrate) navigator.vibrate([120, 60, 120]);
    },
    enabled: function(){ return enabled; },
    toggle: function(){
      enabled = !enabled;
      localStorage.setItem('ff_sound', enabled ? 'on' : 'off');
      if (enabled) window.ffSound.play('ping');
      return enabled;
    },
    armed: function(){ return running(); },
    unlock: unlock
  };
})();