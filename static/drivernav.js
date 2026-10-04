/* Built-in turn-by-turn navigation for the driver app. */
(function(){
  var MAP=null, LINE=null, ME=null, DEST=null, WATCH=null, RT=null, CUR=1, TARGET=null;
  var FOLLOW=true, OFF=0, LAST_RR=0, SPOKEN={}, VOICE=localStorage.getItem('ff_nav_voice')!=='0', LASTPOS=null, ARRIVED=false;
  function $(id){ return document.getElementById(id); }
  function hav(a,b){ var R=6371000,t=Math.PI/180,dl=(b[0]-a[0])*t,dn=(b[1]-a[1])*t;
    var x=Math.sin(dl/2)*Math.sin(dl/2)+Math.cos(a[0]*t)*Math.cos(b[0]*t)*Math.sin(dn/2)*Math.sin(dn/2);
    return 2*R*Math.asin(Math.sqrt(x)); }
  function segDist(p,a,b){ // metres from p to segment a-b, flat-earth is fine at this scale
    var k=Math.cos(p[0]*Math.PI/180)*111320, m=110540;
    var ax=(a[1]-p[1])*k, ay=(a[0]-p[0])*m, bx=(b[1]-p[1])*k, by=(b[0]-p[0])*m;
    var dx=bx-ax, dy=by-ay, L=dx*dx+dy*dy, u=L?Math.max(0,Math.min(1,-(ax*dx+ay*dy)/L)):0;
    var x=ax+u*dx, y=ay+u*dy; return Math.sqrt(x*x+y*y); }
  function offRoute(p){ if(!RT||!RT.coords.length) return 0; var best=1e9, c=RT.coords;
    for(var i=1;i<c.length;i++){ var d=segDist(p,c[i-1],c[i]); if(d<best) best=d; } return best; }
  function fmt(m){ var mi=m/1609.34; if(mi<0.1) return Math.max(10,Math.round(m*3.28084/10)*10)+' ft';
    return (mi<10?mi.toFixed(1):Math.round(mi))+' mi'; }
  function arrow(s){ if(!s) return '\u2191'; if(s.type==='arrive') return '\u2691';
    var m=s.modifier||''; return m==='uturn'?'\u21B6':m.indexOf('slight left')===0?'\u2196':m.indexOf('slight right')===0?'\u2197':
      m.indexOf('left')>=0?'\u2190':m.indexOf('right')>=0?'\u2192':'\u2191'; }
  function say(t){ if(!VOICE||!('speechSynthesis' in window)) return;
    try{ speechSynthesis.cancel(); var u=new SpeechSynthesisUtterance(t); u.rate=1; speechSynthesis.speak(u);}catch(e){} }
  function sayOnce(key,t){ if(SPOKEN[key]) return; SPOKEN[key]=1; say(t); }
  function paintVoice(){ var b=$('navVoice'); if(b) b.textContent='Voice '+(VOICE?'on':'off'); }
  function draw(){
    if(!MAP){ MAP=L.map('navMap',{zoomControl:false}); 
      ffBaseMap(MAP);
      MAP.on('dragstart',function(){ FOLLOW=false; $('navRecenter').classList.remove('hidden'); }); }
    if(LINE) MAP.removeLayer(LINE);
    LINE=L.polyline(RT.coords,{color:'#1a73e8',weight:7,opacity:.85}).addTo(MAP);
    if(!DEST) DEST=L.marker(TARGET.ll).addTo(MAP); else DEST.setLatLng(TARGET.ll);
    if(LASTPOS) MAP.setView(LASTPOS,16); else MAP.fitBounds(LINE.getBounds(),{padding:[30,30]});
  }
  function remaining(p){ if(!RT) return [0,0]; var d=0, t=0;
    for(var i=CUR;i<RT.steps.length;i++){ d+=RT.steps[i].dist_m||0; t+=RT.steps[i].dur_s||0; }
    var s=RT.steps[Math.min(CUR,RT.steps.length-1)], toNext=p?hav(p,[s.lat,s.lng]):0;
    var prev=RT.steps[CUR-1]||{}; var frac=prev.dist_m?Math.min(1,toNext/prev.dist_m):0;
    return [d+toNext, t+(prev.dur_s||0)*frac]; }
  function show(p){
    if(!RT) return; var s=RT.steps[Math.min(CUR,RT.steps.length-1)];
    var dn=p?hav(p,[s.lat,s.lng]):(RT.steps[CUR-1]||{}).dist_m||0;
    $('navArrow').textContent=arrow(s); $('navNext').textContent=s.text; $('navDist').textContent=ARRIVED?'':fmt(dn);
    var r=remaining(p), mins=Math.max(1,Math.round(r[1]/60)), at=new Date(Date.now()+r[1]*1000);
    $('navEta').textContent=ARRIVED?'Arrived':(mins+' min');
    $('navLeft').textContent=ARRIVED?'':(fmt(r[0])+' \u00b7 arrive '+at.toLocaleTimeString([], {hour:'numeric',minute:'2-digit'}));
    return dn;
  }
  function step(pos){
    var p=[pos.coords.latitude,pos.coords.longitude]; LASTPOS=p;
    if(!ME) ME=L.circleMarker(p,{radius:9,color:'#fff',weight:3,fillColor:'#1a73e8',fillOpacity:1}).addTo(MAP); else ME.setLatLng(p);
    if(FOLLOW&&MAP) MAP.setView(p,Math.max(MAP.getZoom(),16),{animate:true});
    if(!RT||ARRIVED) return;
    if(hav(p,TARGET.ll)<40){ ARRIVED=true; CUR=RT.steps.length-1; show(p); sayOnce('arr','You have arrived at '+TARGET.label); return; }
    var s=RT.steps[CUR]; if(!s) return;
    var d=hav(p,[s.lat,s.lng]);
    if(d<25&&CUR<RT.steps.length-1){ CUR++; s=RT.steps[CUR]; d=hav(p,[s.lat,s.lng]); }
    if(d<160&&d>=25) sayOnce('n'+CUR,s.text);
    else if(d<800&&d>=400) sayOnce('f'+CUR,'In '+fmt(d).replace('mi','miles').replace('ft','feet')+', '+s.text);
    show(p);
    // off the blue line for a few readings in a row: get a new route from here
    if(offRoute(p)>60){ OFF++; if(OFF>=3&&Date.now()-LAST_RR>15000){ OFF=0; fetchRoute(p,true); } } else OFF=0;
  }
  function fetchRoute(p,re){
    LAST_RR=Date.now();
    if(re){ $('navNext').textContent='Rerouting...'; }
    jget('/api/driver/route?from='+p[0]+','+p[1]+'&to='+TARGET.ll[0]+','+TARGET.ll[1]).then(function(r){
      if(!r||!r.ok){ $('navNext').textContent=(r&&r.error)||'Couldn\'t get directions. Tap Maps app.'; return; }
      RT=r; CUR=Math.min(1,RT.steps.length-1); SPOKEN={}; draw(); show(p);
      if(!re) say((RT.steps[0]||{}).text ? RT.steps[0].text+'. Then '+RT.steps[CUR].text : RT.steps[CUR].text);
      else say('Rerouting. '+RT.steps[CUR].text);
    }).catch(function(){ $('navNext').textContent='No signal. Trying again...'; setTimeout(function(){ fetchRoute(LASTPOS||p,true); }, 8000); });
  }
  window.ffNav={
    open:function(t){
      TARGET=t; ARRIVED=false; RT=null; CUR=1; FOLLOW=true; SPOKEN={}; OFF=0;
      $('navBox').classList.remove('hidden'); document.body.classList.add('navopen');
      $('navDest').textContent=t.label+(t.address?' \u00b7 '+t.address:''); $('navExt').href=t.ext||'#';
      $('navNext').textContent='Getting directions...'; $('navDist').textContent=''; $('navEta').textContent=''; $('navLeft').textContent='';
      $('navRecenter').classList.add('hidden'); paintVoice();
      try{ if(window.ffAwake) ffAwake.hold(true); }catch(e){}
      setTimeout(function(){
        if(MAP) MAP.invalidateSize(); else { MAP=null; }
        var start=function(pos){ LASTPOS=[pos.coords.latitude,pos.coords.longitude];
          if(!MAP){ RT={coords:[LASTPOS,t.ll],steps:[]}; draw(); RT=null; }
          step(pos); fetchRoute(LASTPOS,false); };
        if(!navigator.geolocation){ $('navNext').textContent='This phone is not sharing its location.'; return; }
        navigator.geolocation.getCurrentPosition(start,function(){ $('navNext').textContent='Turn on location for this app to navigate.'; },
          {enableHighAccuracy:true,timeout:15000,maximumAge:5000});
        WATCH=navigator.geolocation.watchPosition(step,function(){}, {enableHighAccuracy:true,maximumAge:0,timeout:20000});
      },50);
    },
    close:function(){
      $('navBox').classList.add('hidden'); document.body.classList.remove('navopen');
      if(WATCH!==null){ navigator.geolocation.clearWatch(WATCH); WATCH=null; }
      try{ speechSynthesis.cancel(); }catch(e){} try{ if(window.ffAwake) ffAwake.hold(false); }catch(e){}
    },
    recenter:function(){ FOLLOW=true; $('navRecenter').classList.add('hidden'); if(LASTPOS&&MAP) MAP.setView(LASTPOS,16); },
    voice:function(){ VOICE=!VOICE; localStorage.setItem('ff_nav_voice',VOICE?'1':'0'); paintVoice(); if(!VOICE){ try{speechSynthesis.cancel();}catch(e){} } },
    overview:function(){ if(LINE&&MAP){ FOLLOW=false; $('navRecenter').classList.remove('hidden'); MAP.fitBounds(LINE.getBounds(),{padding:[30,30]}); } }
  };
})();
