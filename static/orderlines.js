/* Order lines written out in full: each item, then its add-ons, sides and notes underneath. */
(function(){
  function esc(s){ return String(s==null?'':s).replace(/[&<>"]/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c];}); }
  function parts(o){
    if (o && o.line_parts && o.line_parts.length) return o.line_parts;
    var items = (o && o.items) || [];
    return items.map(function(i){
      var subs = (i.options||[]).map(function(p){
        var g = (p.group==='Instructions' ? 'Note' : String(p.group||'').replace(/\?$/,''));
        return (g ? g+': ' : '') + (p.name||'') + (p.delta_cents ? ' (+$'+(p.delta_cents/100).toFixed(2)+')' : '');
      });
      if (i.note) subs.push('Note: '+i.note);
      return {main: (i.qty||1)+' x '+(i.name||'item'), subs: subs};
    });
  }
  window.OrderLines = {
    /* <li> rows for a <ul class="lines"> */
    lis: function(o){
      return parts(o).map(function(p){
        return '<li><b>'+esc(p.main)+'</b>'+(p.subs && p.subs.length ?
          '<ul class="subopts">'+p.subs.map(function(x){ return '<li>'+esc(x)+'</li>'; }).join('')+'</ul>' : '')+'</li>';
      }).join('');
    },
    /* a whole list block, for order cards */
    block: function(o){ var l = this.lis(o); return l ? '<ul class="lines ordlines">'+l+'</ul>' : ''; },
    /* one line per item with its picks in brackets, for tight spots */
    text: function(o){
      return parts(o).map(function(p){ return esc(p.main)+(p.subs && p.subs.length ? ' ('+p.subs.map(esc).join(', ')+')' : ''); }).join('<br>');
    }
  };
})();
