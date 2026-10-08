/* "View order": one big, easy-to-read popup with everything about an order.
   OrderView.btn(o) returns the button (and remembers the order); OrderView.open(id) shows it. */
(function(){
  var KEEP = {};
  function esc(s){ return String(s==null?'':s).replace(/[&<>"']/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];}); }
  function row(label, html){ return html ? '<div class="ovrow"><div class="ovlbl">'+esc(label)+'</div><div class="ovval">'+html+'</div></div>' : ''; }
  function digits(s){ return String(s||'').replace(/\D/g,''); }
  function money(label, v){ return (v && v!=='$0.00') ? '<div class="ovmoney"><span>'+esc(label)+'</span><span>'+esc(v)+'</span></div>' : ''; }
  function close(){ var b=document.getElementById('ovBack'); if(b) b.remove(); document.removeEventListener('keydown', onKey); }
  function onKey(e){ if(e.key==='Escape') close(); }
  function lines(o){
    if (window.OrderLines) return OrderLines.lis(o);
    return (o.lines||[]).map(function(l){ return '<li>'+esc(l)+'</li>'; }).join('');
  }
  var CONFIRM = null, CONFIRM_MINS = 15;
  function kitchenText(o){
    if (o.kitchen_status==='preparing' && o.timer_seconds!=null) return Math.max(0, Math.ceil(o.timer_seconds/60))+' min left on the timer';
    if (o.prep_minutes && o.kitchen_status!=='pending') return esc(o.prep_minutes)+' min timer';
    return '';
  }
  function callIn(o){
    if (o.uses_app || !o.manual_state) return '';
    var ph = o.order_method!=='online';
    if (o.manual_state==='placed') return (ph ? 'Called in' : 'Ordered online')+(o.manual_time ? ' '+esc(o.manual_time) : '');
    if (o.manual_state==='ordering') return ph ? 'Dispatch is calling it in now' : 'Dispatch is ordering online now';
    return '';
  }
  function confirmBox(o){
    if (!CONFIRM || o.kitchen_status!=='pending') return '';
    var m = Number(o.prep_minutes || CONFIRM_MINS) || 15;
    return '<div class="ovconfirm"><div class="ovlbl">Confirm this order</div>'+
      '<div class="small muted">Set how many minutes it will take. Setting the timer confirms the order.</div>'+
      '<div class="ovtimer"><button type="button" class="btn" onclick="OrderView.bump(-5)">-5</button>'+
      '<input id="ovMins" class="mins" type="number" min="1" max="180" value="'+m+'"> min'+
      '<button type="button" class="btn" onclick="OrderView.bump(5)">+5</button></div>'+
      '<button type="button" class="btn primary ovgo" onclick="OrderView.confirm('+Number(o.id)+')">Set timer and confirm order</button></div>';
  }
  function build(o){
    // follow Settings > Order numbers for this screen (dispatch, restaurant or driver app)
    var num = (typeof window.ordMain === 'function') ? window.ordMain(o) : (o.primary_no || o.code || '');
    var sub = (typeof window.ordSub === 'function') ? window.ordSub(o)
              : ((o.primary_no && o.code && o.primary_no!==o.code) ? o.code : '');
    if (sub === num) sub = '';
    var status = [o.kitchen_status ? 'Kitchen: '+o.kitchen_status : '', o.dispatch_status ? 'Delivery: '+String(o.dispatch_status).replace(/_/g,' ') : '']
                   .filter(Boolean).join(' &middot; ');
    var ph = o.phone ? '<a href="tel:'+digits(o.phone)+'">'+esc(o.phone)+'</a>' : '';
    var rph = o.restaurant_phone ? '<a href="tel:'+digits(o.restaurant_phone)+'">'+esc(o.restaurant_phone)+'</a>' : '';
    var times = (o.timeline||[]).map(function(x){ return esc(x.time)+' &nbsp;'+esc(x.label); }).join('<br>');
    var pay = money('Food', o.subtotal) + money('Tax', o.tax) + money('Delivery fee', o.fee) + money('Service fee', o.service) +
              money('Tip', o.tip) + (o.discount_note ? '<div class="ovmoney"><span>Discount</span><span>'+esc(o.discount_note)+'</span></div>' : '') +
              '<div class="ovmoney ovtotal"><span>Total</span><span>'+esc(o.total||'')+'</span></div>';
    return '<div class="ovhead"><div><div class="ovnum">Order '+esc(num)+(o.ref?' <span class="pill grey">#'+esc(o.ref)+'</span>':'')+'</div>'+
        (sub ? '<div class="muted small">'+esc(sub)+'</div>' : '')+
        (status ? '<div class="small">'+status+'</div>' : '')+'</div>'+
        '<button type="button" class="btn" onclick="OrderView.close()" aria-label="Close">Close</button></div>'+
      '<div class="ovbody">'+
      row('Brand', o.site_name ? esc(o.site_name) : '')+
      row('House account', o.house ? '<b>'+esc(o.house_name || 'No business name')+'</b>' : '')+
      row('Scheduled for', o.scheduled_label ? '<b>'+esc(o.scheduled_label)+'</b>' : '')+
      row('Items'+(o.item_count ? ' ('+o.item_count+')' : ''), '<ul class="lines ovitems">'+lines(o)+'</ul>')+
      row('Customer', '<b>'+esc(o.customer||'')+'</b>'+(ph ? '<br>'+ph : ''))+
      row('Deliver to', esc(o.address||'')+(o.note ? '<div class="ovnote">'+esc(o.note)+'</div>' : ''))+
      row('Restaurant', '<b>'+esc(o.restaurant||'')+'</b>'+(o.restaurant_address ? '<br>'+esc(o.restaurant_address) : '')+(rph ? '<br>'+rph : ''))+
      row('Driver', o.driver ? esc(o.driver) : '')+
      row('Dispatch note', o.dispatch_note ? '<div class="ovnote">'+esc(o.dispatch_note)+'</div>' : '')+
      row('Hand-off', o.drop_label ? '<b>'+esc(o.drop_label)+'</b>' : '')+
      row('Problem', o.issue ? esc(o.issue)+(o.issue_note ? ' - '+esc(o.issue_note) : '')+(o.cloned_from ? ' (first order '+esc(o.cloned_from)+')' : '') : '')+
      row('Address check', o.needs_address_approval ? '<div class="ovnote">Address not verified yet, waiting on dispatch approval.</div>' : '')+
      row('Kitchen', kitchenText(o))+
      row('Called in', callIn(o))+
      row('Queue', [o.queue_position ? 'Queue #'+esc(o.queue_position) : '', o.hold_reason ? esc(o.hold_reason) : ''].filter(Boolean).join(' &middot; '))+
      row('Details', [o.miles!=null && o.miles!=='' ? esc(o.miles)+' mi from the restaurant' : '',
                      o.placed_by ? 'Placed by '+esc(o.placed_by) : '', o.source ? 'From '+esc(o.source) : '',
                      o.payment_status ? 'Payment '+esc(String(o.payment_status).replace(/_/g,' ')) : '',
                      o.item_fee && o.item_fee!=='$0.00' ? 'Custom fees '+esc(o.item_fee) : ''].filter(Boolean).join('<br>'))+
      row('Payment', pay)+
      row('Times', times)+
      confirmBox(o)+
      '</div>';
  }
  window.OrderView = {
    btn: function(o, label, primary){
      if (!o || o.id==null) return '';
      KEEP[o.id] = o;
      return '<button type="button" class="btn tiny ovbtn'+(primary?' primary':'')+'" onclick="OrderView.open('+Number(o.id)+')">'+esc(label||'View order')+'</button>';
    },
    remember: function(o){ if (o && o.id!=null) KEEP[o.id] = o; },
    open: function(id){
      var o = (typeof id === 'object') ? id : KEEP[id];
      if (!o) return;
      close();
      var back = document.createElement('div');
      back.id = 'ovBack'; back.className = 'modalback ovback';
      back.innerHTML = '<div class="panel ovpanel" role="dialog" aria-modal="true">'+build(o)+'</div>';
      back.addEventListener('click', function(e){ if (e.target === back) close(); });
      document.body.appendChild(back);
      document.addEventListener('keydown', onKey);
    },
    close: close,
    /* kitchen app: a pending order can only be confirmed from inside the popup by setting a timer */
    setConfirm: function(fn, mins){ CONFIRM = fn; if (mins) CONFIRM_MINS = mins; },
    bump: function(d){ var i=document.getElementById('ovMins'); if(!i) return; i.value = Math.min(180, Math.max(1, (Number(i.value)||0)+d)); },
    confirm: function(id){
      var i = document.getElementById('ovMins'); var m = i ? Number(i.value) : 0;
      if (!m || m < 1){ if (i) i.focus(); return; }
      var b = document.querySelector('#ovBack .ovgo'); if (b){ b.disabled = true; b.textContent = 'Confirming...'; }
      Promise.resolve(CONFIRM && CONFIRM(id, m)).then(close, function(){ if (b){ b.disabled=false; b.textContent='Set timer and confirm order'; } });
    }
  };
})();
