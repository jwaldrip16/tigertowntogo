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
    (gp.some(r=>selected.includes(r.id))?' checked':'')+'> '+rgEsc(gp.map(r=>r.name).join(' + '))+'</label>').join('') + '</span>';
}
function regionVals(cls, root){
  const out = [];
  Array.from((root||document).querySelectorAll('.'+cls+':checked')).forEach(b=>
    String(b.value).split(',').forEach(v=>{ if (v) out.push(+v); }));
  return out;
}
function regionTag(label){ return label ? ' <span class="pill blue rgtag">'+rgEsc(label)+'</span>' : ''; }
