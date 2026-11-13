/* Vélo'v sky: lights the map like the real sky over Lyon.
   The sun's elevation for the displayed moment drives a cross-fade from the normal map to a
   dark one, plus a tint that goes golden near sunset, pink at sunset, blue at dusk. A small
   dial shows the sun's path over the day, sunrise/sunset and the moon at night.
   Usage: const sky = VelovSky.attach(map, {url, attribution}); sky.update(dateObject); */
(function(){
  const LAT = 45.76, LNG = 4.85, RAD = Math.PI/180;

  // Sun elevation in degrees (low-precision solar ephemeris, good to a fraction of a degree)
  function sunAlt(date){
    const d = date.getTime()/86400000 - 10957.5;                 // days since J2000.0
    const g = (357.529 + 0.98560028*d)*RAD, q = 280.459 + 0.98564736*d;
    const L = (q + 1.915*Math.sin(g) + 0.020*Math.sin(2*g))*RAD, e = (23.439 - 0.00000036*d)*RAD;
    const ra = Math.atan2(Math.cos(e)*Math.sin(L), Math.cos(L)), dec = Math.asin(Math.sin(e)*Math.sin(L));
    const gmst = ((18.697374558 + 24.06570982441908*d) % 24 + 24) % 24;
    const H = (gmst*15 + LNG)*RAD - ra, lat = LAT*RAD;
    return Math.asin(Math.sin(lat)*Math.sin(dec) + Math.cos(lat)*Math.cos(dec)*Math.cos(H))/RAD;
  }
  // Moon: fraction of the synodic month since a known new moon (0 = new, .5 = full)
  function moonAge(date){ const m = 29.530588853; return (((date - Date.UTC(2000,0,6,18,14))/86400000) % m + m) % m / m; }
  const MOONS = ['🌑','🌒','🌓','🌔','🌕','🌖','🌗','🌘'];
  const moonIcon = d => MOONS[Math.round(moonAge(d)*8) % 8];

  // Local midnight in Lyon for the day of `date`
  function lyonMidnight(date){
    const p = new Intl.DateTimeFormat('en-GB',{timeZone:'Europe/Paris',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hourCycle:'h23'})
      .formatToParts(date).reduce((a,x)=>(a[x.type]=x.value,a),{});
    const mins = (+p.hour)*60 + (+p.minute);
    return {start: new Date(date.getTime() - mins*60000 - date.getSeconds()*1000 - date.getMilliseconds()), mins, key: p.year+p.month+p.day};
  }
  const dayCache = new Map();
  function dayInfo(date){
    const {start, key} = lyonMidnight(date);
    if(dayCache.has(key)) return dayCache.get(key);
    const curve = [], ev = {};
    let prev = null;
    for(let m=0; m<=1440; m+=5){
      const a = sunAlt(new Date(start.getTime() + m*60000)); curve.push(a);
      if(prev!=null){
        if(prev < -0.833 && a >= -0.833) ev.rise = m - 5*(a+0.833)/(a-prev);
        if(prev >= -0.833 && a < -0.833) ev.set = m - 5*(a+0.833)/(a-prev);
      }
      prev = a;
    }
    const info = {start, curve, rise: ev.rise, set: ev.set, max: Math.max(...curve)};
    dayCache.set(key, info); return info;
  }

  // How the map should look for a sun elevation
  const TINT = [ [14,'#ffffff',0], [6,'#ffe3b3',.20], [2,'#ffb26e',.36], [-1,'#ef8f86',.40], [-4,'#a07cc4',.38],
                 [-8,'#5266b0',.32], [-13,'#27345f',.24] ];
  const hexRgb = h => [1,3,5].map(i=>parseInt(h.substr(i,2),16));
  function tintAt(alt){
    if(alt >= TINT[0][0]) return {c:TINT[0][1], o:0};
    for(let i=1;i<TINT.length;i++){ const [a1,c1,o1]=TINT[i-1], [a2,c2,o2]=TINT[i];
      if(alt >= a2){ const t=(a1-alt)/(a1-a2), A=hexRgb(c1), B=hexRgb(c2);
        return {c:'#'+A.map((v,k)=>Math.round(v+(B[k]-v)*t).toString(16).padStart(2,'0')).join(''), o:o1+(o2-o1)*t}; } }
    const l=TINT[TINT.length-1]; return {c:l[1], o:l[2]};
  }
  const smooth = t => { t=Math.max(0,Math.min(1,t)); return t*t*(3-2*t); };
  function look(alt){
    const t = tintAt(alt);
    return {alt, dark: smooth((3-alt)/13), tint:t.c, tintOp:t.o};          // fully dark below -10°
  }
  function phase(alt, morning){
    if(alt >= 6) return 'Jour';
    if(alt >= 0) return morning ? 'Lever du soleil' : 'Heure dorée';
    if(alt >= -6) return morning ? 'Aube' : 'Crépuscule';
    if(alt >= -12) return 'Heure bleue';
    return 'Nuit';
  }
  const hm = m => m==null ? '–' : `${Math.floor(m/60)}h${String(Math.round(m%60)).padStart(2,'0')}`;

  const CSS = `
  .sky-night{filter:invert(1) hue-rotate(180deg) brightness(.95) contrast(.92) saturate(.5)}
  .sky-tint{position:absolute;left:0;top:0;pointer-events:none;mix-blend-mode:multiply;transition:background-color .6s, opacity .6s}
  .sky-dial{background:var(--panel,#fff);color:var(--ink,#1d1f22);border-radius:12px;padding:7px 9px 6px;box-shadow:0 2px 10px rgba(0,0,0,.18);font:12px/1.3 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;width:200px;user-select:none;transition:background-color .6s,color .6s}
  .sky-dial svg{display:block;width:182px;height:56px}
  .sky-dark .sky-dial{background:rgba(22,25,32,.88);color:#e9e7e2}
  .sky-dark .sky-dial .tm{color:#a3a7b0}
  .sky-dark .sky-dial button{background:#2b2f37;color:#e9e7e2}
  .sky-dial .row{display:flex;justify-content:space-between;align-items:center;gap:6px}
  .sky-dial .ph{font-weight:650}
  .sky-dial .tm{color:var(--muted,#6b6f76);font-variant-numeric:tabular-nums;font-size:11px}
  .sky-dial button{border:0;background:var(--chip,#f0eeea);color:var(--ink,#1d1f22);border-radius:999px;padding:2px 8px;font:inherit;font-size:11px;cursor:pointer}
  .sky-dial.min{width:auto;padding:5px 8px}
  .sky-dial.min svg,.sky-dial.min .tm,.sky-dial.min .mode{display:none}
  .sky-dial .exp{border:0;background:none;color:inherit;font:inherit;font-weight:650;cursor:pointer;padding:2px 0;display:inline-flex;gap:5px;align-items:center;min-height:24px}
  .sky-dial .exp:focus-visible,.sky-dial button:focus-visible{outline:2px solid currentColor;outline-offset:2px}
  .sky-dial .tm{font-size:12px}
  .sky-dial svg text{font-size:10px}
  .leaflet-container{transition:background-color .6s}`;
  const MODES = ['auto','day','night','off'], MODE_TXT = {auto:'Auto', day:'Jour', night:'Nuit', off:'Off'};
  const store = { get(){ try{ return localStorage.getItem('velovSky')||'auto'; }catch(e){ return 'auto'; } },
                  set(v){ try{ localStorage.setItem('velovSky',v); }catch(e){} },
                  open(){ try{ return localStorage.getItem('velovSkyOpen')==='1'; }catch(e){ return false; } },
                  setOpen(v){ try{ localStorage.setItem('velovSkyOpen',v?'1':'0'); }catch(e){} } };

  function attach(map, opts={}){
    if(!document.getElementById('sky-css')){ const s=document.createElement('style'); s.id='sky-css'; s.textContent=CSS; document.head.appendChild(s); }
    const url = opts.url || 'https://tile.openstreetmap.org/{z}/{x}/{y}.png';
    const night = L.tileLayer(url, {maxZoom:19, className:'sky-night', opacity:0}).addTo(map);
    map.createPane('sky'); const pane = map.getPane('sky'); pane.style.zIndex = 260; pane.style.pointerEvents = 'none';
    const tint = L.DomUtil.create('div','sky-tint',pane);
    const place = () => { const s=map.getSize(); tint.style.width=s.x+'px'; tint.style.height=s.y+'px';
      L.DomUtil.setPosition(tint, map.containerPointToLayerPoint([0,0])); };
    map.on('move zoom resize viewreset', place); place();
    const lightBg = opts.lightBg || getComputedStyle(map.getContainer()).backgroundColor || '#e8e5df';

    let mode = store.get(), when = new Date(), state = null, lastKey = '', open = store.open();
    const dial = L.control({position: 'bottomright'});            // same corner on every map
    let box;
    dial.onAdd = () => { box = L.DomUtil.create('div','sky-dial'); L.DomEvent.disableClickPropagation(box); L.DomEvent.disableScrollPropagation(box); return box; };
    dial.addTo(map);

    function draw(){
      const info = dayInfo(when), {mins} = lyonMidnight(when);
      const real = sunAlt(when);
      const alt = mode==='day' ? 45 : mode==='night' ? -25 : real;
      state = mode==='off' ? {alt:real, dark:0, tint:'#ffffff', tintOp:0} : look(alt);
      night.setOpacity(state.dark);
      tint.style.backgroundColor = state.tint; tint.style.opacity = state.tintOp;
      const nb=[13,16,22], lb=(lightBg.match(/\d+/g)||[232,229,223]).slice(0,3).map(Number);
      map.getContainer().classList.toggle('sky-dark', state.dark > .55);
      map.getContainer().style.backgroundColor = `rgb(${lb.map((v,i)=>Math.round(v+(nb[i]-v)*state.dark)).join(',')})`;
      // dial: sun path over the Lyon day, horizon, current position
      const W=182, H=56, hy=36, k=(hy-4)/Math.max(20,info.max), x=m=>m/1440*W, y=a=>hy - Math.max(-16,Math.min(70,a))*k;
      const pts = info.curve.map((a,i)=>`${x(i*5).toFixed(1)},${y(a).toFixed(1)}`).join(' ');
      const sx=x(mins), sy=y(real), up = real > -0.833;
      const isNight = real < -6;
      box.innerHTML = `<svg viewBox="0 0 ${W} ${H}" aria-hidden="true">
          <defs><clipPath id="skyUp"><rect x="0" y="0" width="${W}" height="${hy}"/></clipPath>
            <linearGradient id="skyG" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#ffcf5a" stop-opacity=".55"/><stop offset="1" stop-color="#ffcf5a" stop-opacity=".05"/></linearGradient></defs>
          <rect x="0" y="${hy}" width="${W}" height="${H-hy}" fill="#27345f" opacity=".14"/>
          <polygon points="0,${hy} ${pts} ${W},${hy}" fill="url(#skyG)" clip-path="url(#skyUp)"/>
          <polyline points="${pts}" fill="none" stroke="currentColor" stroke-opacity=".35" stroke-width="1.2"/>
          <line x1="0" y1="${hy}" x2="${W}" y2="${hy}" stroke="currentColor" stroke-opacity=".35"/>
          ${[6,12,18].map(h=>`<line x1="${x(h*60)}" y1="${hy-2}" x2="${x(h*60)}" y2="${hy+2}" stroke="currentColor" stroke-opacity=".4"/><text x="${x(h*60)}" y="${H-2}" font-size="8.5" text-anchor="middle" fill="currentColor" opacity=".55">${h}h</text>`).join('')}
          <line x1="${sx}" y1="2" x2="${sx}" y2="${H-11}" stroke="currentColor" stroke-opacity=".25" stroke-dasharray="2 2"/>
          ${up ? `<circle cx="${sx}" cy="${sy}" r="6.5" fill="#ffb21f" opacity=".35"/><circle cx="${sx}" cy="${sy}" r="4" fill="#ffb21f"/>`
               : `<circle cx="${sx}" cy="${sy}" r="3.5" fill="#ffb21f" opacity=".45"/><text x="${Math.min(W-8,Math.max(8,sx))}" y="13" font-size="12" text-anchor="middle">${moonIcon(when)}</text>`}
        </svg>
        <div class="row"><button class="exp" aria-expanded="${open}" title="${open?'Réduire':'Afficher la course du soleil'}">${isNight?moonIcon(when):up?'☀':'🌅'} ${phase(real, mins<720)}</button><button class="mode" title="Lumière de la carte : suit le soleil (Auto), toujours jour, toujours nuit ou désactivée">${MODE_TXT[mode]}</button></div>
        <div class="tm">lever ${hm(info.rise)} · coucher ${hm(info.set)}${mode!=='auto'&&mode!=='off'?' · forcé':''}</div>`;
      box.classList.toggle('min', !open);
      box.querySelector('.mode').onclick = () => { mode = MODES[(MODES.indexOf(mode)+1)%MODES.length]; store.set(mode); lastKey=''; update(when, true); };
      box.querySelector('.exp').onclick = () => { open = !open; store.setOpen(open); lastKey=''; update(when, true); };
    }
    function update(date, force){
      when = date instanceof Date ? date : new Date(date);
      if(isNaN(when)) return;
      const key = mode + Math.round(when.getTime()/60000);            // the sky changes slowly: once a minute is plenty
      if(!force && key===lastKey) return; lastKey = key;
      draw();
    }
    update(opts.time || new Date(), true);
    return {update, get state(){ return state; }, sunAlt, look, nightLayer: night};
  }
  window.VelovSky = {attach, sunAlt, look};
})();
