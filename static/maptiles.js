/* Puts the right map pictures on a Leaflet map: Google when the business has a Google key,
   OpenStreetMap otherwise. If Google tiles stop loading, it switches back by itself. */
(function(){
  var P = null;
  function cfg(){
    if (P) return P;
    P = fetch('/api/map-tiles', {credentials:'same-origin'}).then(function(r){ return r.json(); })
      .catch(function(){ return {provider:'osm'}; });
    return P;
  }
  var OSM = {url:'https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', attribution:'&copy; OpenStreetMap', max_zoom:19, tile_size:256};
  function osm(map){ return L.tileLayer(OSM.url, {maxZoom:OSM.max_zoom, attribution:OSM.attribution}).addTo(map); }
  window.ffBaseMap = function(map){
    var tmp = osm(map);
    cfg().then(function(c){
      if (!c || c.provider !== 'google' || !c.url) return;
      var big = (c.tile_size || 256) > 256;
      var g = L.tileLayer(c.url, {maxZoom:c.max_zoom || 22, maxNativeZoom:21,
        tileSize: big ? 512 : 256, zoomOffset: big ? -1 : 0,
        attribution: c.attribution || 'Google'});
      var bad = 0;
      g.on('tileerror', function(){ bad++; if (bad === 6){ map.removeLayer(g); osm(map); P = Promise.resolve({provider:'osm'}); } });
      g.addTo(map); map.removeLayer(tmp);
      if (map.attributionControl) map.attributionControl.setPrefix('<b style="color:#5f6368">Google</b>');
    });
  };
})();
