/* Puts the right map pictures on a Leaflet map: Google when the business has a Google key,
   otherwise the Esri street map (house numbers and accurate US streets). If those tiles stop
   loading, it falls back to OpenStreetMap by itself. */
(function(){
  var P = null;
  function cfg(){
    if (P) return P;
    P = fetch('/api/map-tiles', {credentials:'same-origin'}).then(function(r){ return r.json(); })
      .catch(function(){ return {provider:'osm'}; });
    return P;
  }
  var OSM = {url:'https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', attribution:'&copy; OpenStreetMap', max_zoom:19, tile_size:256};
  var ESRI = {url:'https://server.arcgisonline.com/ArcGIS/rest/services/World_Street_Map/MapServer/tile/{z}/{y}/{x}',
    attribution:'Tiles &copy; Esri, HERE, Garmin, USGS, OpenStreetMap contributors', max_zoom:20};
  function plainOsm(map){ return L.tileLayer(OSM.url, {maxZoom:OSM.max_zoom, attribution:OSM.attribution}).addTo(map); }
  // Default: Esri street map (house numbers on the blocks). A small switch in the corner flips to
  // OpenStreetMap, which sometimes has newer side streets. The choice is remembered on this device.
  function osm(map){
    var e = L.tileLayer(ESRI.url, {maxZoom:ESRI.max_zoom, maxNativeZoom:19, attribution:ESRI.attribution});
    var o = L.tileLayer(OSM.url, {maxZoom:ESRI.max_zoom, maxNativeZoom:OSM.max_zoom, attribution:OSM.attribution});
    var pick = 'esri';
    try { pick = localStorage.getItem('ffMapPick') || 'esri'; } catch(_){}
    var cur = (pick === 'osm') ? o : e;
    var bad = 0;
    e.on('tileerror', function(){ bad++; if (bad === 6 && map.hasLayer(e)){ map.removeLayer(e); o.addTo(map); } });
    cur.addTo(map);
    if (L.control && L.control.layers && !map._ffSwitch){
      map._ffSwitch = L.control.layers({'Street map (house numbers)': e, 'OpenStreetMap': o}, null,
                                       {position:'topright'}).addTo(map);
      map.on('baselayerchange', function(ev){
        try { localStorage.setItem('ffMapPick', ev.layer === o ? 'osm' : 'esri'); } catch(_){}
      });
    }
    return cur;
  }
  window.ffBaseMap = function(map){
    var tmp = osm(map);
    cfg().then(function(c){
      if (!c || c.provider !== 'google' || !c.url) return;
      var big = (c.tile_size || 256) > 256;
      var g = L.tileLayer(c.url, {maxZoom:c.max_zoom || 22, maxNativeZoom:21,
        tileSize: big ? 512 : 256, zoomOffset: big ? -1 : 0,
        attribution: c.attribution || 'Google'});
      var bad = 0;
      g.on('tileerror', function(){ bad++; if (bad === 6){ map.removeLayer(g); map._ffSwitch = null; osm(map); P = Promise.resolve({provider:'esri'}); } });
      g.addTo(map); map.eachLayer(function(l){ if (l !== g && l instanceof L.TileLayer) map.removeLayer(l); });
      if (map._ffSwitch){ map.removeControl(map._ffSwitch); map._ffSwitch = null; }
      if (map.attributionControl) map.attributionControl.setPrefix('<b style="color:#5f6368">Google</b>');
    });
  };
})();
