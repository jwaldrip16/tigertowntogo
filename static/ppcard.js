// Dispatch types the customer's card into PayPal's own card boxes.
// The number goes straight to PayPal; this site never sees or saves it.
(function(){
  // One PayPal card form per set of keys: each brand can have its own PayPal account.
  const sdks = {};
  function loadSdk(code){
    return fetch('/api/paypal/client' + (code ? '?code=' + encodeURIComponent(code) : ''))
      .then(function(r){ return r.json(); }).then(function(c){
      if (!c.enabled) throw new Error('PayPal is not set up yet. Add your PayPal keys first.');
      if (sdks[c.client_id]) return sdks[c.client_id];
      const ns = 'ppCF' + Object.keys(sdks).length;
      sdks[c.client_id] = new Promise(function(res, rej){
        const s = document.createElement('script');
        s.src = 'https://www.paypal.com/sdk/js?client-id=' + encodeURIComponent(c.client_id) +
                '&currency=USD&intent=authorize&components=card-fields';
        s.setAttribute('data-namespace', ns);
        s.onload = function(){ res(window[ns]); };
        s.onerror = function(){ delete sdks[c.client_id]; rej(new Error('Could not load the PayPal card form. Check the connection and try again.')); };
        document.head.appendChild(s);
      });
      return sdks[c.client_id];
    });
  }

  function esc(x){ return String(x == null ? '' : x).replace(/[&<>"]/g, function(c){ return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]; }); }
  function open(o){
    o = o || {};
    const old = document.getElementById('ppcfModal'); if (old) old.remove();
    const box = document.createElement('div');
    box.className = 'modalback'; box.id = 'ppcfModal';
    box.innerHTML = '<div class="itemmodal" role="dialog" aria-label="Card payment" style="max-width:460px">' +
      '<h3 style="margin-top:0">Card for ' + esc(o.code) + '</h3>' +
      '<p class="small muted">Amount ' + esc(o.amount) + '. It is held now and charged after delivery. ' +
      'The card goes straight to PayPal and is never saved here.</p>' +
      '<div id="ppcfName"></div><div id="ppcfNum"></div>' +
      '<div class="row" style="gap:8px"><div id="ppcfExp" style="flex:1"></div><div id="ppcfCvv" style="flex:1"></div></div>' +
      '<input id="ppcfZip" placeholder="Billing ZIP" inputmode="numeric" maxlength="10" style="width:100%;margin:6px 0">' +
      '<div id="ppcfMsg" class="small" style="min-height:1.4em"></div>' +
      '<div class="row" style="gap:8px;margin-top:8px">' +
      '<button class="btn primary" id="ppcfGo" disabled>Loading card form...</button>' +
      '<button class="btn" id="ppcfClose">' + esc(o.closeLabel || 'Close') + '</button></div></div>';
    document.body.appendChild(box);
    const msg = box.querySelector('#ppcfMsg'), go = box.querySelector('#ppcfGo');
    function say(t, bad){ msg.textContent = t; msg.style.color = bad ? '#c0392b' : ''; }
    let fields = [];
    function close(){
      // shut PayPal's card boxes down properly so the next card form starts clean
      fields.forEach(function(f){ try { f.close(); } catch(e){} });
      fields = [];
      box.remove(); if (o.onClose) o.onClose();
    }
    box.querySelector('#ppcfClose').onclick = close;
    loadSdk(o.code).then(function(pp){
      if (!document.body.contains(box)) return;
      const cf = pp.CardFields({
        createOrder: async function(){
          const r = await jpost('/api/paypal/create', {code: o.code, card: true});
          if (!r || !r.ok){ say((r && r.error) || 'Could not start the payment.', true); throw new Error('create'); }
          return r.id;
        },
        onApprove: async function(data){
          say('Finishing...');
          const r = await jpost('/api/paypal/approve', {code: o.code, id: data.orderID});
          if (!r || !r.ok){ say((r && r.error) || 'The card did not go through.', true); go.disabled = false; go.textContent = 'Run card'; return; }
          say('Card accepted. ' + r.held + ' held on the card.');
          go.textContent = 'Done';
          if (o.onDone) o.onDone(r);
          setTimeout(close, 1200);
        },
        onError: function(err){
          say('The card did not go through. Check the number, date, code and ZIP, or try another card.', true);
          go.disabled = false; go.textContent = 'Run card';
        }
      });
      if (!cf.isEligible()){
        if (o.onPage){
          say('Card boxes are not turned on for this PayPal account yet. Close this and use the PayPal button.', true);
          go.textContent = 'Unavailable'; return;
        }
        say('Typed card payments are not turned on for this PayPal account yet. Use the pay page instead.', true);
        go.textContent = 'Open pay page'; go.disabled = false;
        go.onclick = function(){ window.open('/pay/' + encodeURIComponent(o.code), '_blank'); };
        return;
      }
      [[cf.NameField({placeholder: 'Name on card'}), '#ppcfName'], [cf.NumberField(), '#ppcfNum'],
       [cf.ExpiryField(), '#ppcfExp'], [cf.CVVField(), '#ppcfCvv']].forEach(function(x){
        fields.push(x[0]); x[0].render(x[1]);
      });
      go.disabled = false; go.textContent = 'Run card';
      go.onclick = function(){
        const zip = box.querySelector('#ppcfZip').value.trim();
        if (zip && !/^\d{5}(-?\d{4})?$/.test(zip)){ say('The billing ZIP is 5 digits.', true); return; }
        go.disabled = true; go.textContent = 'Running...'; say('');
        const args = zip ? {billingAddress: {postalCode: zip, countryCode: 'US'}} : undefined;
        cf.submit(args).catch(function(){
          say('Fix the card details and try again.', true); go.disabled = false; go.textContent = 'Run card';
        });
      };
    }).catch(function(e){ say(e.message || 'Could not load the card form.', true); go.textContent = 'Unavailable'; });
  }
  window.ppCard = {open: open};
})();
