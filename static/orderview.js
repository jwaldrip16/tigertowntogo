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
  function build(o){
    var num = (o.primary_no || o.code || '');
    var sub = (o.primary_no && o.code && o.primary_no!==o.code) ? o.code : '';
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
      row('Scheduled for', o.scheduled_label ? '<b>'+esc(o.scheduled_label)+'</b>' : '')+
      row('Items'+(o.item_count ? ' ('+o.item_count+')' : ''), '<ul class="lines ovitems">'+lines(o)+'</ul>')+
      row('Customer', '<b>'+esc(o.customer||'')+'</b>'+(ph ? '<br>'+ph : ''))+
      row('Deliver to', esc(o.address||'')+(o.note ? '<div class="ovnote">'+esc(o.note)+'</div>' : ''))+
      row('Restaurant', '<b>'+esc(o.restaurant||'')+'</b>'+(o.restaurant_address ? '<br>'+esc(o.restaurant_address) : '')+(rph ? '<br>'+rph : ''))+
      row('Driver', o.driver ? esc(o.driver) : '')+
      row('Dispatch note', o.dispatch_note ? '<div class="ovnote">'+esc(o.dispatch_note)+'</div>' : '')+
      row('Problem', o.issue ? esc(o.issue)+(o.issue_note ? ' - '+esc(o.issue_note) : '') : '')+
      row('Payment', pay)+
      row('Times', times)+
      '</div>';
  }
  window.OrderView = {
    btn: function(o, label){
      if (!o || o.id==null) return '';
      KEEP[o.id] = o;
      return '<button type="button" class="btn tiny ovbtn" onclick="OrderView.open('+Number(o.id)+')">'+(label||'View order')+'</button>';
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
    close: close
  };
})();
