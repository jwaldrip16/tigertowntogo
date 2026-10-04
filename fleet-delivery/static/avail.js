// Item availability (days and hours), checked in Opelika time.
var Avail = (function(){
  var DAYN = ['Mon','Tue','Wed','Thu','Fri','Sat','Sun'];
  function hm(s){ var m = String(s||'').match(/^(\d{1,2}):(\d{2})/); return m ? (+m[1])*60 + (+m[2]) : null; }
  function parts(v){
    var m = String(v||'').match(/(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})/);
    if (m){
      var wd = (new Date(Date.UTC(+m[1], +m[2]-1, +m[3])).getUTCDay() + 6) % 7;
      return {wd: wd, min: (+m[4])*60 + (+m[5])};
    }
    var f = {};
    new Intl.DateTimeFormat('en-US', {timeZone:'America/Chicago', weekday:'short', hour:'2-digit',
      minute:'2-digit', hourCycle:'h23'}).formatToParts(new Date()).forEach(function(p){ f[p.type] = p.value; });
    return {wd: DAYN.indexOf(f.weekday), min: ((+f.hour) % 24)*60 + (+f.minute)};
  }
  function ok(it, p){
    if (!it) return true;
    p = p || parts('');
    var days = it.avail_days || [];
    if (days.length && days.indexOf(p.wd) < 0) return false;
    var s = hm(it.avail_start), e = hm(it.avail_end);
    if (s === null || e === null || s === e) return true;
    return e > s ? (p.min >= s && p.min < e) : (p.min >= s || p.min < e);
  }
  return {parts: parts, ok: ok, DAYN: DAYN};
})();
