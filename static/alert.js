/* Shared audible alert. Uses the Web Audio API so there is no sound file to ship. */
(function(){
  var ctx = null, armed = false;
  var enabled = localStorage.getItem('ff_sound') !== 'off';

  function unlock(){
    if (ctx) return;
    try { ctx = new (window.AudioContext || window.webkitAudioContext)(); armed = true; } catch(e){}
  }
  ['click','keydown','touchstart'].forEach(function(ev){
    document.addEventListener(ev, unlock, {once:false});
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
      unlock();
      if (!ctx) return;
      if (ctx.state === 'suspended') ctx.resume();
      if (pattern === 'ping'){ beep(880, 0, 0.18, 0.25); return; }
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
    armed: function(){ return armed; }
  };
})();