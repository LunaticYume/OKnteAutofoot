function e(){let e=document.getElementById(`cc-quick`);if(!e||e.__quickBound)return;e.__quickBound=!0;let t=document.getElementById(`quick-data`);if(!t)return;let n=JSON.parse(t.dataset.chars||`[]`),r=JSON.parse(t.dataset.upgrade||`{}`),i=JSON.parse(t.dataset.materials||`{}`);function a(e){if(!e)return`/images/placeholder.svg`;let t=e.replace(/^\/Game\//,``);return`/images/items/${t.split(`/`).pop()||t}.webp`}let o=document.getElementById(`quick-calc-area`),s=document.getElementById(`char-configs`),c=document.getElementById(`calc-result`),l=document.getElementById(`calc-status`),u=document.getElementById(`selected-count`),d=new Set;function f(){let e=[...d].map(e=>n.find(t=>t.slug===e)?.name??e);u.textContent=d.size===0?`未选择角色`:`已选：${e.join(`、`)}`,o.style.display=d.size?``:`none`,p(),m()}function p(){s.innerHTML=[...d].map(e=>{let t=n.find(t=>t.slug===e),i=r[e]?.skills||[];return`<div class="rounded-xl border border-base bg-card p-4" data-char="${e}">
          <div class="flex items-center justify-between mb-3">
            <div class="flex items-center gap-2">
              ${t?`<img src="${t.icon}" alt="${t.name}" class="w-7 h-7 rounded-full object-cover" />`:``}
              <span class="text-sm font-bold">${t?.name??e}</span>
              <span class="text-[10px] text-muted">${t?.rarity??``}${t?.element?`·`+t.element:``}</span>
            </div>
            <button class="text-[10px] text-muted hover:text-red-500 transition-colors remove-char" data-slug="${e}">移除</button>
          </div>
          <div class="flex flex-wrap items-center gap-3 mb-3">
            <div class="flex items-center gap-1.5 text-xs">
              <span class="text-muted">突破</span>
              <select class="cfg-asc-from text-xs px-1.5 py-0.5 rounded border border-base bg-card" data-char="${e}">
                <option value="0">Lv 1</option>${[30,40,50,60,70,80].map((e,t)=>`<option value="${t+1}">Lv ${e}</option>`).join(``)}
              </select>
              <span>→</span>
              <select class="cfg-asc-to text-xs px-1.5 py-0.5 rounded border border-base bg-card" data-char="${e}">
                ${[30,40,50,60,70,80].map((e,t)=>`<option value="${t+1}" ${t===5?`selected`:``}>Lv ${e}</option>`).join(``)}
              </select>
            </div>
          </div>
          ${i.length>0?`<div class="grid sm:grid-cols-3 gap-1.5">${i.map((t,n)=>{let r=t.levels[t.levels.length-1]?.level||1;return`<div class="flex items-center gap-1 p-1.5 rounded border border-base bg-subtle/30 text-xs">
              <span class="truncate min-w-0 flex-1">${t.name||`技能`+(n+1)}</span><span class="text-muted">Lv</span>
              <select class="cfg-skill-from text-[11px] px-1 py-0.5 rounded border border-base bg-card" data-char="${e}" data-skill="${n}">
                ${Array.from({length:r},(e,t)=>`<option value="${t+1}">${t+1}</option>`).join(``)}
              </select><span class="text-muted">→</span>
              <select class="cfg-skill-to text-[11px] px-1 py-0.5 rounded border border-base bg-card" data-char="${e}" data-skill="${n}">
                ${Array.from({length:r},(e,t)=>`<option value="${t+1}" ${t===r-1?`selected`:``}>${t+1}</option>`).join(``)}
              </select>
            </div>`}).join(``)}</div>`:``}
        </div>`}).join(``),s.querySelectorAll(`.remove-char`).forEach(e=>{e.addEventListener(`click`,()=>{let t=e.dataset.slug;d.delete(t),document.querySelector(`.char-chip[data-slug="${t}"]`)?.classList.remove(`is-active`),f()})}),s.querySelectorAll(`select`).forEach(e=>{e.__cfgBound||(e.__cfgBound=!0,e.addEventListener(`change`,m))})}function m(){let e={};[...d].forEach(t=>{let n=r[t];n&&s.querySelectorAll(`[data-char="${t}"]`).forEach(t=>{let r=parseInt(t.querySelector(`.cfg-asc-from`)?.value||`0`),i=parseInt(t.querySelector(`.cfg-asc-to`)?.value||`0`);for(let t=Math.max(0,r);t<i;t++)n.ascension?.[t]?.items?.forEach(t=>{e[t.id]=(e[t.id]||0)+t.amount});t.querySelectorAll(`.cfg-skill-from`).forEach(r=>{let i=parseInt(r.dataset.skill),a=parseInt(r.value),o=t.querySelector(`.cfg-skill-to[data-skill="${i}"]`),s=o?parseInt(o.value):a;for(let t=a+1;t<=s;t++)(n.skills?.[i]?.levels?.find(e=>e.level===t))?.items?.forEach(t=>{e[t.id]=(e[t.id]||0)+t.amount})})})});let t=Object.entries(e).filter(([,e])=>e>0);if(l.textContent=`共需 ${t.length} 种材料`,t.length===0){c.innerHTML=`<p class="text-xs text-muted col-span-full text-center py-4">无需额外材料</p>`;return}c.innerHTML=t.map(([e,t])=>{let n=i[e]||{name:e,icon:``};return`<div class="flex items-center gap-2 p-2 rounded-lg border border-base bg-subtle/50">
          <div class="w-7 h-7 shrink-0 rounded bg-subtle flex items-center justify-center p-1"><img src="${a(n.icon)}" alt="${n.name}" class="w-full h-full object-contain" loading="lazy" onerror="this.style.display='none'" /></div>
          <div class="min-w-0 flex-1"><p class="text-[11px] font-medium truncate">${n.name}</p></div>
          <span class="text-xs font-semibold shrink-0">${t.toLocaleString()}</span></div>`}).join(``)}document.querySelectorAll(`.char-chip`).forEach(e=>{e.__chipBound||(e.__chipBound=!0,e.addEventListener(`click`,()=>{let t=e.dataset.slug;d.has(t)?(d.delete(t),e.classList.remove(`is-active`)):(d.add(t),e.classList.add(`is-active`)),f()}))});let h=document.getElementById(`clear-all`);h&&!h.__clearBound&&(h.__clearBound=!0,h.addEventListener(`click`,()=>{d.clear(),document.querySelectorAll(`.char-chip`).forEach(e=>e.classList.remove(`is-active`)),f()}))}e(),document.addEventListener(`astro:after-swap`,e);