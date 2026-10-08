// Region checkboxes shared by the availability forms. No box checked = the person's usual regions.
let REGIONS = null;
async function loadRegions(){
  if (REGIONS) return REGIONS;
  try { const r = await jget('/api/regions-list'); REGIONS = r.ok ? r : {regions:[], mine:[]}; }
  catch(e){ REGIONS = {regions:[], mine:[]}; }
  return REGIONS;
}
function rgEsc(s){ return String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
function regionBoxes(cls, selected, extra){
  selected = selected || [];
  if (REGIONS && REGIONS.none_assigned)
    return '<span class="muted small">You are not assigned a region yet, so you cannot set availability. Ask dispatch to assign you one.</span>';
  if (!REGIONS || !REGIONS.regions.length) return '';
  // regions combined for drivers (Auburn + Downtown Auburn) show as one chip that covers both
  const groups = [], byLead = {};
  REGIONS.regions.forEach(r=>{
    if (r.drive_lead && byLead[r.drive_lead]) { byLead[r.drive_lead].push(r); return; }
    const gp = [r]; if (r.drive_lead) byLead[r.drive_lead] = gp; groups.push(gp);
  });
  return '<span class="rgpick"><span class="muted small">Region:</span>' + groups.map(gp=>
    '<label class="rgchip"><input type="checkbox" class="'+cls+'" value="'+gp.map(r=>r.id).join(',')+'"'+(extra||'')+
    (gp.some(r=>selected.includes(r.id))?' checked':'')+'> '+rgEsc(gp.map(r=>r.name).join(' + '))+
    (gp.some(r=>r.locked)?' <span class="rglocked">Locked</span>':'')+'</label>').join('') + '</span>';
}
function regionVals(cls, root){
  const out = [];
  Array.from((root||document).querySelectorAll('.'+cls+':checked')).forEach(b=>
    String(b.value).split(',').forEach(v=>{ if (v) out.push(+v); }));
  return out;
}
// Names of regions whose brand is locked (Settings > Regions > Brand sites).
function rgLockedNames(){ return new Set(((REGIONS&&REGIONS.regions)||[]).filter(r=>r.locked).map(r=>r.name)); }
// A region label ("Auburn + Downtown Auburn, Athens") as HTML with each locked region marked.
function rgMark(label){
  if (!label) return '';
  const lk = rgLockedNames();
  return String(label).split(', ').map(part=>{
    const names = part.split(' + ');
    const hit = names.some(n=>lk.has(n.trim()));
    return rgEsc(part) + (hit ? ' <span class="rglocked">Locked</span>' : '');
  }).join(', ');
}
function regionTag(label){
  if (!label) return '';
  const lk = rgLockedNames();
  const hit = String(label).split(/, | \+ /).some(n=>lk.has(n.trim()));
  return ' <span class="pill '+(hit?'red':'blue')+' rgtag">'+rgEsc(label)+(hit?' (locked)':'')+'</span>';
}
(function(){ if (document.getElementById('rglockedcss')) return;
  const s = document.createElement('style'); s.id = 'rglockedcss';
  s.textContent = '.rglocked{display:inline-block;margin-left:4px;padding:1px 7px;border-radius:999px;background:#fde2e1;color:#b42318;font-size:12px;font-weight:600;vertical-align:middle}';
  (document.head||document.documentElement).appendChild(s); })();
