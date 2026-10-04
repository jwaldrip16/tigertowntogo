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
  return '<span class="rgpick"><span class="muted small">Region:</span>' + REGIONS.regions.map(r=>
    '<label class="rgchip"><input type="checkbox" class="'+cls+'" value="'+r.id+'"'+(extra||'')+
    (selected.includes(r.id)?' checked':'')+'> '+rgEsc(r.name)+'</label>').join('') + '</span>';
}
function regionVals(cls, root){
  return Array.from((root||document).querySelectorAll('.'+cls+':checked')).map(b=>+b.value);
}
function regionTag(label){ return label ? ' <span class="pill blue rgtag">'+rgEsc(label)+'</span>' : ''; }
