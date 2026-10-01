// Card box: type a customer's card, copy it into your own card terminal, then mark
// the order paid. Closing the box keeps the card on the order (encrypted on the
// server once it checks out, otherwise held in this tab) so nothing typed is lost.
// After it is marked paid the security code is dropped; the rest can be viewed later.
(function(){
  let box = null, ORDER = null, idle = null;
  const digits = s => (s || '').replace(/\D/g, '');
  function brand(n){
    if(/^4/.test(n)) return 'Visa';
    if(/^(5[1-5]|2[2-7])/.test(n)) return 'Mastercard';
    if(/^3[47]/.test(n)) return 'Amex';
    if(/^(6011|65|64[4-9])/.test(n)) return 'Discover';
    return '';
  }
  function luhn(n){
    if(n.length < 13) return false;
    let sum = 0, alt = false;
    for(let i = n.length - 1; i >= 0; i--){
      let d = +n[i];
      if(alt){ d *= 2; if(d > 9) d -= 9; }
      sum += d; alt = !alt;
    }
    return sum % 10 === 0;
  }
  // 4-4-4-4 groups (Amex 4-6-5)
  function groups(n){
    if(/^3[47]/.test(n)) return [n.slice(0,4), n.slice(4,10), n.slice(10,15)].filter(Boolean).join(' ');
    return (n.match(/.{1,4}/g) || []).join(' ');
  }
  async function copy(text, btn){
    try{ await navigator.clipboard.writeText(text); }
    catch(e){
      const t = document.createElement('textarea'); t.value = text; document.body.appendChild(t);
      t.select(); try{ document.execCommand('copy'); }catch(_){} t.remove();
    }
    if(btn){ const was = btn.textContent; btn.textContent = 'Copied'; setTimeout(()=>btn.textContent = was, 1200); }
  }
  function val(id){ return (document.getElementById(id) || {}).value || ''; }
  // Checks every field. Returns {ok, errors:{field:message}}.
  function validate(f){
    const e = {};
    const name = (f.name || '').trim().replace(/\s+/g, ' ');
    if(!name) e.name = 'Enter the name on the card.';
    else if(!/^[A-Za-z][A-Za-z .'\-]*$/.test(name)) e.name = 'Letters, spaces, periods, hyphens and apostrophes only.';
    else if(name.split(' ').length < 2) e.name = 'Enter first and last name as printed on the card.';
    const n = digits(f.number);
    const b = brand(n);
    const lens = b === 'Amex' ? [15] : b === 'Visa' ? [13,16,19] : b ? [16] : [13,14,15,16,17,18,19];
    if(!n) e.number = 'Enter the card number.';
    else if(lens.indexOf(n.length) < 0) e.number = (b || 'This card') + ' numbers are ' + lens.join(' or ') + ' digits; this one has ' + n.length + '.';
    else if(!luhn(n)) e.number = 'That number does not check out. Re-read it.';
    const m = /^(\d{2})\/(\d{2})$/.exec((f.exp || '').trim());
    if(!m) e.exp = 'Use MM/YY.';
    else {
      const mm = +m[1], yy = 2000 + +m[2];
      const now = new Date();
      if(mm < 1 || mm > 12) e.exp = 'Month must be 01 to 12.';
      else if(yy < now.getFullYear() || (yy === now.getFullYear() && mm < now.getMonth() + 1)) e.exp = 'This card has expired.';
      else if(yy > now.getFullYear() + 20) e.exp = 'That year is too far out. Re-read it.';
    }
    const c = (f.cvc || '').trim();
    const clen = b === 'Amex' ? 4 : 3;
    if(!/^\d+$/.test(c)) e.cvc = 'Enter the ' + clen + '-digit security code.';
    else if(c.length !== clen) e.cvc = (b === 'Amex' ? 'Amex uses a 4-digit code on the front.' : 'Use the 3-digit code on the back.');
    const z = (f.zip || '').trim();
    if(z && !/^\d{5}(-?\d{4})?$/.test(z)) e.zip = 'ZIP is 5 digits (or ZIP+4).';
    return {ok: Object.keys(e).length === 0, errors: e, brand: b};
  }
  function form(){ return {name: val('cbName'), number: val('cbNum'), exp: val('cbExp'), cvc: val('cbCvc'), zip: val('cbZip')}; }
  function showErrors(touchedOnly){
    const r = validate(form());
    [['name','cbName'],['number','cbNum'],['exp','cbExp'],['cvc','cbCvc'],['zip','cbZip']].forEach(([k,id]) => {
      const el = document.getElementById(id), msg = document.getElementById(id + 'Err');
      if(!el || !msg) return;
      const show = r.errors[k] && (!touchedOnly || el.dataset.touched);
      msg.textContent = show ? r.errors[k] : '';
      el.style.borderColor = show ? '#c0392b' : (el.value && !r.errors[k] ? '#2e8b57' : '');
    });
    const paid = document.getElementById('cbPaid'), all = document.getElementById('cbAll');
    if(paid){ paid.disabled = !r.ok; paid.style.opacity = r.ok ? '' : '.5'; }
    if(all){ all.disabled = !r.ok; all.style.opacity = r.ok ? '' : '.5'; }
    const st = document.getElementById('cbCheck');
    if(st) st.textContent = r.ok ? ((r.brand ? r.brand + ' ' : '') + 'card checks out. Ready to copy.') : (r.brand ? r.brand : '');
    return r;
  }
  function wipe(){
    ['cbName','cbNum','cbExp','cbCvc','cbZip','cbRef'].forEach(id => {
      const el = document.getElementById(id); if(el){ el.value = ''; delete el.dataset.touched; el.style.borderColor = ''; }
      const er = document.getElementById(id + 'Err'); if(er) er.textContent = '';
    });
    const m = document.getElementById('cbCheck'); if(m) m.textContent = '';
  }
  const DRAFTS = {};
  function snapshot(){
    return {name: val('cbName'), number: val('cbNum'), exp: val('cbExp'), cvc: val('cbCvc'),
            zip: val('cbZip'), ref: val('cbRef')};
  }
  async function keep(){
    // Save what was typed so closing never loses the card.
    if(!ORDER || !ORDER.id || ORDER.viewOnly || ORDER.saved || !box) return;
    const f = snapshot();
    if(!(f.name || f.number || f.exp || f.cvc || f.zip)) { delete DRAFTS[ORDER.id]; return; }
    DRAFTS[ORDER.id] = f;
    if(!validate(f).ok) return;
    try{
      const r = await fetch('/api/order/card-save', {method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({order_id: ORDER.id, card: f})});
      const res = await r.json();
      if(res.ok) delete DRAFTS[ORDER.id];
    }catch(e){}
  }
  async function close(){
    await keep();
    wipe(); clearTimeout(idle);
    if(box){ box.remove(); box = null; }
    const done = ORDER && ORDER.onClose; ORDER = null;
    if(done) done();
  }
  function poke(){ clearTimeout(idle); idle = setTimeout(close, 10 * 60 * 1000); }
  function check(){
    const n = digits(val('cbNum'));
    const el = document.getElementById('cbNum');
    const g = groups(n.slice(0, 19));
    if(el.value !== g) el.value = g;
    const e = digits(val('cbExp')).slice(0, 4);
    const ex = document.getElementById('cbExp');
    const ef = e.length > 2 ? e.slice(0,2) + '/' + e.slice(2) : e;
    if(ex.value !== ef) ex.value = ef;
    // Amex CVC is 4 digits
    const cv = document.getElementById('cbCvc'); if(cv) cv.maxLength = brand(n) === 'Amex' ? 4 : 3;
    showErrors(true);
    poke();
  }
  function allText(){
    const n = digits(val('cbNum'));
    return ['Name: ' + val('cbName'), 'Card: ' + groups(n), 'Exp: ' + val('cbExp'),
            'CVC: ' + val('cbCvc'), 'ZIP: ' + val('cbZip')].join('\n');
  }
  async function markPaid(){
    if(!ORDER || !ORDER.id){ close(); return; }
    document.querySelectorAll('#cardBox input').forEach(el => el.dataset.touched = '1');
    const v = showErrors(false);
    if(!v.ok){ alert('Fix the highlighted card fields first.'); return; }
    const n = digits(val('cbNum'));
    try{
      await fetch('/api/order/card-save', {method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({order_id: ORDER.id, card: snapshot()})});
    }catch(e){}
    const r = await fetch('/api/order/mark-paid', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({order_id: ORDER.id, method:'card_keyed', last4: n.slice(-4), ref: val('cbRef')})});
    let res = {}; try{ res = await r.json(); }catch(e){}
    if(!r.ok || !res.ok){ alert(res.error || 'That did not save.'); return; }
    delete DRAFTS[ORDER.id]; ORDER.saved = true;
    close();
  }
  function field(id, label, attrs, copyLabel){
    return '<label class="small" style="display:block;margin-top:8px">'+label+'</label>'+
      '<div style="display:flex;gap:6px"><input id="'+id+'" '+attrs+' autocomplete="off" style="flex:1">'+
      '<button type="button" class="btn tiny" data-copy="'+id+'">'+(copyLabel || 'Copy')+'</button></div>'+
      '<div id="'+id+'Err" class="small" style="color:#c0392b;min-height:1em"></div>';
  }
  window.ffCard = {
    open: function(order){
      close();
      ORDER = order || {};
      box = document.createElement('div');
      box.id = 'cardBox';
      box.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.55);z-index:95;display:flex;align-items:center;justify-content:center';
      const amt = ORDER.amount || ORDER.total || '';
      box.innerHTML = '<form class="card" style="max-width:440px;width:94%;max-height:92vh;overflow:auto" autocomplete="off" onsubmit="return false">'+
        '<h3 style="margin-top:0">Card for '+(ORDER.code || 'this order')+(amt ? ' ('+amt+')' : '')+'</h3>'+
        '<p class="muted small">Type it in, copy it into your card terminal, run it, then mark paid. '+
        'Nothing here is saved. It clears when you close this box.</p>'+
        field('cbName','Name on card','type="text"')+
        field('cbNum','Card number','type="text" inputmode="numeric" placeholder="xxxx xxxx xxxx xxxx"')+
        '<div id="cbCheck" class="small muted" style="margin-top:4px"></div>'+
        '<div style="display:flex;gap:8px"><div style="flex:1">'+field('cbExp','Exp (MM/YY)','type="text" inputmode="numeric" placeholder="MM/YY"')+'</div>'+
        '<div style="flex:1">'+field('cbCvc','CVC','type="password" inputmode="numeric" maxlength="3"')+'</div></div>'+
        field('cbZip','Billing ZIP (optional)','type="text" inputmode="numeric" maxlength="10"')+
        (amt ? '<div style="display:flex;gap:6px;margin-top:8px"><span class="big" style="flex:1">Amount '+amt+'</span>'+
               '<button type="button" class="btn tiny" id="cbAmt">Copy amount</button></div>' : '')+
        '<div style="display:flex;gap:6px;margin-top:10px"><button type="button" class="btn" id="cbAll">Copy everything</button>'+
        '<button type="button" class="btn" id="cbClear">Clear</button></div>'+
        (ORDER.id ? '<hr><label class="small">Approval or reference number from your terminal (optional)</label>'+
           '<input id="cbRef" type="text" autocomplete="off">'+
           '<button type="button" class="btn go" id="cbPaid" style="margin-top:8px;width:100%">It went through: mark '+(ORDER.code||'order')+' paid</button>' : '')+
        '<button type="button" class="btn" id="cbClose" style="margin-top:8px;width:100%">'+(ORDER.closeLabel || 'Close and clear')+'</button></form>';
      document.body.appendChild(box);
      box.querySelectorAll('input').forEach(el => {
        el.addEventListener('input', check);
        el.addEventListener('blur', function(){ if(this.id !== 'cbRef'){ this.dataset.touched = '1'; showErrors(true); } });
      });
      showErrors(true);
      box.querySelectorAll('[data-copy]').forEach(b => b.onclick = function(){
        const id = this.getAttribute('data-copy');
        copy(id === 'cbNum' ? digits(val(id)) : val(id), this);
      });
      const amtBtn = document.getElementById('cbAmt');
      if(amtBtn) amtBtn.onclick = function(){ copy(String(amt).replace(/[^0-9.]/g,''), this); };
      document.getElementById('cbAll').onclick = function(){
        document.querySelectorAll('#cardBox input').forEach(el => el.dataset.touched = '1');
        if(!showErrors(false).ok) return;
        copy(allText(), this);
      };
      document.getElementById('cbClear').onclick = wipe;
      document.getElementById('cbClose').onclick = close;
      const paid = document.getElementById('cbPaid'); if(paid) paid.onclick = markPaid;
      if(!ORDER.prefill && ORDER.id && DRAFTS[ORDER.id]) ORDER.prefill = DRAFTS[ORDER.id];
      if(ORDER.viewOnly){
        const pb = document.getElementById('cbPaid'); if(pb) pb.style.display = 'none';
        const rf = document.getElementById('cbRef'); if(rf){ rf.closest('div').style.display = 'none'; }
        const st = document.createElement('p'); st.className = 'small muted';
        st.textContent = 'Paid. Security code is not kept after a card is run.';
        box.querySelector('#cbCheck').after(st);
      }
      if(ORDER.prefill){
        const p = ORDER.prefill;
        if(p.ref && document.getElementById('cbRef')) document.getElementById('cbRef').value = p.ref;
        document.getElementById('cbName').value = p.name || '';
        document.getElementById('cbNum').value = p.number || '';
        document.getElementById('cbExp').value = p.exp || '';
        document.getElementById('cbCvc').value = p.cvc || '';
        document.getElementById('cbZip').value = p.zip || '';
        box.querySelectorAll('input').forEach(el => { if(el.id !== 'cbRef') el.dataset.touched = '1'; });
        check();
      }
      document.getElementById('cbName').focus();
      poke();
    },
    close: close,
    validate: validate,
    groups: groups,
    brand: brand,
    _validate: validate
  };
  window.addEventListener('pagehide', wipe);
  window.addEventListener('beforeunload', wipe);
})();
