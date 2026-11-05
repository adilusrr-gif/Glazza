'use strict';
let selectedSite='';
let platformBusy=false;
function siteOptions(value='',all=false){
  return (all?'<option value="">Все объекты</option>':'')+(state.platform?.sites||[]).map(s=>`<option value="${escapeHTML(s.id)}" ${s.id===value?'selected':''}>${escapeHTML(s.name)} · ${escapeHTML(state.platform.profiles[s.profile].name)}</option>`).join('');
}
function scopeUrl(url){return selectedSite?url+(url.includes('?')?'&':'?')+'site_id='+encodeURIComponent(selectedSite):url;}
function updateSiteSelector(){
  const node=$('#site-selector');if(!node||!state.platform)return;
  if(selectedSite&&!state.platform.sites.some(s=>s.id===selectedSite))selectedSite='';
  if(document.activeElement!==node)node.innerHTML=siteOptions(selectedSite,true);
}
function renderPlatform(){
  const p=state.platform;if(!p)return;
  const names={implemented:'Реализован',experimental:'Экспериментальный',planned:'В разработке: включить нельзя'};
  $('#content').innerHTML=head('Организации и объекты','Единая установка для доверенного администратора. Организационные группы не разделяют права доступа между независимыми клиентами.','<button class="secondary" id="new-organization">+ Организация</button><button class="primary" id="new-site">+ Объект</button>')+
    '<div class="warning-box">Эта версия управляется общим администратором. Раздельные кабинеты клиентов и роли сотрудников пока не реализованы.</div>'+
    `<div class="model-grid">${p.sites.map(s=>`<article class="panel"><h2>${escapeHTML(s.name)}</h2><p>${escapeHTML(p.organizations.find(o=>o.id===s.organization_id)?.name||'')} · ${escapeHTML(p.profiles[s.profile].name)}</p><p>${s.modules.map(m=>escapeHTML(p.modules[m].name)).join(' · ')}</p><button class="secondary" data-site-edit="${s.id}">Настроить</button></article>`).join('')}</div>`+
    `<section class="panel"><h2>Привязка камер</h2><p>Фильтр сверху меняет обзор, карту, журнал и историю анализа. Здесь доступны все камеры установки. Перенос камеры не переносит историю старых событий.</p>${(state.allCameras||[]).map(c=>`<div class="status-row"><strong>${escapeHTML(c.name)}</strong><label>Объект<select data-camera-site="${c.id}" aria-label="Объект камеры ${escapeHTML(c.name)}">${siteOptions(c.site_id)}</select></label><button class="secondary" data-camera-site-save="${c.id}">Сохранить</button></div>`).join('')||empty('Добавьте камеры')}</section>`+
    `<section class="panel"><h2>Модули аналитики</h2>${Object.entries(p.modules).map(([id,m])=>`<div class="status-row"><span>${escapeHTML(m.name)}</span>${pill(names[m.state],m.state==='implemented'?'':'warning')}<small>${m.model?m.availability==='ready'?'Модель загружена':'Нужны файлы модели или запуск анализатора':''}</small></div>`).join('')}<p>Профиль выбирает стартовые модули. Очереди, сокрытие товара, СИЗ, автомобильные события и сверка с кассами пока не выполняются. Детектор возможной кражи экспериментальный и требует VadCLIP.</p></section>`;
  $('#new-organization').onclick=()=>{modal('Добавить организацию','<form id="organization-form"><label>Название<input name="name" required minlength="2" maxlength="120"></label><p class="error" id="organization-error"></p><button class="primary">Создать организацию</button></form>');$('#organization-form').onsubmit=async e=>{e.preventDefault();try{await api('/platform/organizations',{method:'POST',body:JSON.stringify(Object.fromEntries(new FormData(e.target)))});$('#modal').close();refresh();}catch(x){$('#organization-error').textContent=x.message;}};};
  $('#new-site').onclick=()=>siteForm();
  $$('[data-site-edit]').forEach(b=>b.onclick=()=>siteForm(p.sites.find(s=>s.id===b.dataset.siteEdit)));
  $$('[data-camera-site-save]').forEach(b=>b.onclick=async()=>{if(platformBusy)return;platformBusy=true;try{const select=$$('[data-camera-site]').find(n=>n.dataset.cameraSite===b.dataset.cameraSiteSave);await api('/platform/cameras/'+b.dataset.cameraSiteSave+'/site',{method:'PUT',body:JSON.stringify({site_id:select.value})});toast('Камера перенесена. История сохранена на прежнем объекте.');}catch(x){toast(x.message);}finally{platformBusy=false;refresh();}});
}
function siteForm(site){
  const p=state.platform,s=site||{name:'',organization_id:p.organizations[0].id,profile:'general',modules:p.profiles.general.defaults};
  const checkboxes=modules=>Object.entries(p.modules).map(([id,m])=>`<label><input type="checkbox" name="module" value="${id}" ${modules.includes(id)?'checked':''} ${m.state==='planned'||id==='person'?'disabled':''}>${escapeHTML(m.name)}${m.state==='planned'?' · запланировано':''}${m.state==='experimental'?' · экспериментально':''}</label>`).join('');
  modal(site?'Настроить объект':'Создать объект',`<form id="site-form"><label>Название<input name="name" required minlength="2" maxlength="120" value="${escapeHTML(s.name)}"></label><label>Организация<select name="organization_id">${p.organizations.map(o=>`<option value="${o.id}" ${s.organization_id===o.id?'selected':''}>${escapeHTML(o.name)}</option>`).join('')}</select></label><label>Профиль<select name="profile">${Object.entries(p.profiles).map(([id,v])=>`<option value="${id}" ${s.profile===id?'selected':''}>${escapeHTML(v.name)}</option>`).join('')}</select></label><fieldset><legend>Модули объекта</legend><div id="site-modules">${checkboxes(s.modules)}</div></fieldset><p>Изменение профиля подставляет стартовые модули. Проверяйте наличие моделей в разделе «Анализ видео». Отключение модуля останавливает новые сигналы, но не удаляет историю.</p><p class="error" id="site-error"></p><button class="primary">Сохранить объект</button></form>`);
  $('#site-form select[name=profile]').onchange=e=>{$('#site-modules').innerHTML=checkboxes(p.profiles[e.target.value].defaults);};
  $('#site-form').onsubmit=async e=>{e.preventDefault();const f=new FormData(e.target),body={name:f.get('name'),organization_id:f.get('organization_id'),profile:f.get('profile'),modules:[...new Set(['person',...f.getAll('module')])]};try{await api('/platform/sites'+(site?'/'+site.id:''),{method:site?'PUT':'POST',body:JSON.stringify(body)});$('#modal').close();refresh();}catch(x){$('#site-error').textContent=x.message;}};
}
