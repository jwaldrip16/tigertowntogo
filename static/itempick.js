/* Shared item pop-up: required sides and drinks, extra sauces (with up to N of each),
   special instructions and quantity. Used by the customer menu and the dispatch order page.
   ItemPick.open(item, onAdd) -> onAdd({menu_item_id, name, price_cents, qty, options}) */
(function(){
  let CUR = null, CURQ = 1, COUNTS = {}, DONE = null;
  function esc(s){ return String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
  function cash(c){ return typeof money === 'function' ? money(c) : ('$' + (c/100).toFixed(2)); }
  function limitText(g){
    if (g.min > 0 && g.min === g.max && g.max > 1) return 'pick ' + g.max + (g.each > 1 ? (', up to '+g.each+' of each') : '');
    return (g.max > 1 ? ('pick up to '+g.max) : 'pick 1') + (g.each > 1 ? (', up to '+g.each+' of each') : '');
  }
  function groupTotal(g){ return g.options.reduce((s,o)=>s+(COUNTS[o.id]||0),0); }
  function findGroup(gid){ return (CUR.groups||[]).find(g=>g.id===gid); }
  function showErr(t){ const e = document.getElementById('imErr'); if (e) e.textContent = t; }
  function unitPrice(){
    let p = CUR.price_cents;
    (CUR.groups||[]).forEach(g=>g.options.forEach(o=>{ p += (COUNTS[o.id]||0) * o.delta_cents; }));
    return p;
  }
  function updateAdd(){ const b = document.getElementById('imAdd'); if (b) b.textContent = 'Add to bag  ' + cash(unitPrice() * CURQ); }
  function escClose(ev){ if (ev.key === 'Escape') close(); }
  function close(){
    const m = document.getElementById('itemModal'); if (m) m.remove();
    document.removeEventListener('keydown', escClose);
    CUR = null;
  }
  function open(item, onAdd){
    if (!item) return;
    close();
    CUR = item; DONE = onAdd; CURQ = 1; COUNTS = {};
    const groups = (CUR.groups||[]).map(g=>{
      const rows = g.options.map(o=>{
        const price = o.delta_cents ? '<span class="muted">+'+cash(o.delta_cents)+'</span>' : '';
        if (g.each > 1){
          return '<div class="optrow" id="or'+o.id+'"><span>'+esc(o.name)+' '+price+'</span>'+
            '<span class="step"><button type="button" onclick="ItemPick.step('+g.id+','+o.id+',-1)">&minus;</button>'+
            '<b id="oc'+o.id+'">0</b><button type="button" onclick="ItemPick.step('+g.id+','+o.id+',1)">+</button></span></div>';
        }
        return '<label class="optrow" id="or'+o.id+'"><span><input type="'+(g.max>1?'checkbox':'radio')+
          '" name="mg'+g.id+'" value="'+o.id+'" onchange="ItemPick.pick('+g.id+','+o.id+',this)"> '+esc(o.name)+'</span>'+price+'</label>';
      }).join('');
      return '<div class="ogroup" id="og'+g.id+'"><div class="ohead"><span>'+esc(g.name)+'</span>'+
        '<span class="req">'+(g.min>0?'REQUIRED':'OPTIONAL')+'</span></div>'+
        '<div class="muted small ohint">'+limitText(g)+'</div><div class="ogrid">'+rows+'</div></div>';
    }).join('');
    const box = document.createElement('div');
    box.className = 'modalback'; box.id = 'itemModal';
    box.onclick = ev=>{ if (ev.target === box) close(); };
    box.innerHTML = '<div class="itemmodal" role="dialog" aria-label="'+esc(CUR.name)+'">'+
      '<button type="button" class="imclose" onclick="ItemPick.close()" aria-label="Close">&times;</button>'+
      '<h2>'+esc(CUR.name)+'</h2><div class="im-cols"><div>'+
      (groups || '<p class="muted">No choices to make on this one.</p>')+'</div><div>'+
      (CUR.image ? '<img class="imimg" src="'+CUR.image+'" alt="">' : '')+
      (CUR.description ? '<p>'+esc(CUR.description)+'</p>' : '')+
      '<textarea id="imNote" rows="2" maxlength="120" placeholder="Your special instructions..."></textarea>'+
      '<div class="addbag"><button type="button" class="qtyb" onclick="ItemPick.qty(-1)" aria-label="One less">&minus;</button>'+
      '<b id="imQty">1</b><button type="button" class="qtyb" onclick="ItemPick.qty(1)" aria-label="One more">+</button>'+
      '<button type="button" class="btn primary" id="imAdd" onclick="ItemPick.add()"></button></div>'+
      '<div class="small bad" id="imErr"></div></div></div></div>';
    document.body.appendChild(box);
    document.addEventListener('keydown', escClose);
    updateAdd();
  }
  function step(gid, oid, d){
    const g = findGroup(gid); const next = (COUNTS[oid]||0) + d;
    if (next < 0) return;
    if (d > 0 && next > g.each){ showErr('Up to '+g.each+' of each.'); return; }
    if (d > 0 && groupTotal(g) + 1 > g.max){ showErr(g.name+': pick at most '+g.max+'.'); return; }
    COUNTS[oid] = next;
    document.getElementById('oc'+oid).textContent = next;
    document.getElementById('or'+oid).classList.toggle('on', next > 0);
    showErr(''); updateAdd();
  }
  function pick(gid, oid, inp){
    const g = findGroup(gid);
    if (g.max <= 1){ g.options.forEach(o=>{ COUNTS[o.id] = 0; }); }
    if (inp.checked && g.max > 1 && groupTotal(g) + 1 > g.max){
      inp.checked = false; showErr(g.name+': pick at most '+g.max+'.'); return;
    }
    COUNTS[oid] = inp.checked ? 1 : 0;
    showErr(''); updateAdd();
  }
  function qty(d){ CURQ = Math.max(1, Math.min(50, CURQ + d)); document.getElementById('imQty').textContent = CURQ; updateAdd(); }
  function add(){
    const picks = [];
    for (const g of (CUR.groups||[])){
      const n = groupTotal(g);
      if (n < g.min){
        showErr(g.min > 1 ? (g.name+': pick '+g.min+'.') : ('Choose your '+g.name.replace(/^what /i,'').replace(/ would you like\??$/i,'').replace(/\?$/,'').toLowerCase()+' first.'));
        document.getElementById('og'+g.id).scrollIntoView({behavior:'smooth', block:'center'});
        return;
      }
      for (const o of g.options){
        const c = COUNTS[o.id]||0;
        if (c) picks.push({group:g.name, name: c > 1 ? (o.name+' x'+c) : o.name, delta_cents:o.delta_cents * c});
      }
    }
    const note = (document.getElementById('imNote').value||'').trim();
    if (note) picks.push({group:'Instructions', name:note, delta_cents:0});
    const line = {menu_item_id:CUR.id, name:CUR.name, price_cents:unitPrice(), qty:CURQ, options:picks};
    const cb = DONE;
    close();
    if (cb) cb(line);
  }
  function label(c){
    const picks = (c.options||[]).map(o=>(o.group==='Instructions'?'Note':o.group.replace(/\?$/,''))+': '+o.name+(o.delta_cents?(' +'+cash(o.delta_cents)):''));
    return esc(c.name) + (picks.length ? ' <span class="muted small">('+picks.map(esc).join(', ')+')</span>' : '');
  }
  window.ItemPick = {open, close, step, pick, qty, add, label};
})();
