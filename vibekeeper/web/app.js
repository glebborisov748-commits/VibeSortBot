const $ = id => document.getElementById(id);
const tg = window.Telegram?.WebApp;
tg?.ready(); tg?.expand(); tg?.setHeaderColor?.('#080f22'); tg?.setBackgroundColor?.('#080f22');
const auth = tg?.initData || '';
$('authNote').hidden = !!auth;
const taskKey = 'vibesort.job.' + (tg?.initDataUnsafe?.user?.id || 'guest');
const labels = {phonk:'Фонк',funk:'Фанк',rock:'Рок',pop:'Поп',hiphop:'Хип-хоп',electronic:'Электроника',metal:'Метал',jazz:'Джаз',classical:'Классика',chill:'Спокойный вайб',energy:'Энергичный вайб',melancholy:'Меланхолия',dark:'Мрачный вайб',romantic:'Романтика',unknown:'Без категории'};
let genreOptions={'phonk':'Фонк','funk':'Фанк','rock':'Рок','pop':'Поп','hip hop':'Хип-хоп','electronic':'Электроника','metal':'Метал','jazz':'Джаз','classical':'Классика'};
const moods = new Set(['chill','energy','melancholy','dark','romantic']);
let busy = false, sourceRef = '', currentPage = 'add', current = null, currentId = '', filter = 'all', activeJob = null, timer = null, polling = false;
function notice(message, bad = false) { const el = $(bad ? 'error' : 'info'); el.textContent = message; el.hidden = false; }
function clearNotices() { $('error').hidden = true; $('info').hidden = true; }
function count() { $('count').textContent = $('tracks').value.split('\n').filter(x=>x.trim()).length + ' треков'; }
$('tracks').addEventListener('input', count);
function lock(value) { busy = value; for (const id of ['analyze','photo','sample','loadPlaylist','playlistUrl','tracks','genre','spotifyLikes']) $(id).disabled = value; }
function node(tag, text, cls) { const el=document.createElement(tag); if(text!==undefined)el.textContent=text; if(cls)el.className=cls; return el; }
async function api(path, options = {}) {
  if (!auth) throw Error('Открой приложение кнопкой в Telegram-боте.');
  const controller = new AbortController(); const timeout = setTimeout(()=>controller.abort(), 25000);
  try {
    const response=await fetch(path,{...options,signal:controller.signal,headers:{'X-Telegram-Init-Data':auth,...options.headers}});
    let data; try { data=await response.json(); } catch { throw Error(response.status===401?'Сессия устарела. Закрой и открой приложение.':response.status===413?'Фото слишком большое. Выбери скриншот меньшего размера.':'Сервер временно не отвечает. Попробуй ещё раз.'); }
    if(!response.ok)throw Error(data.error||'Не удалось выполнить запрос.'); return data;
  } catch(error) {
    if(error.name==='AbortError'||error instanceof TypeError)throw Error('Соединение прервалось. Список остался на месте. Если загрузка уже началась, нажми «Продолжить проверку».');
    throw error;
  } finally { clearTimeout(timeout); }
}
function send(path, body, method='POST') { return api(path,{method,headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}); }
async function navigate(page) {
  currentPage=page; $('results').hidden=true;
  for(const name of ['add','library','settings']) $('page-'+name).hidden=name!==page;
  document.querySelectorAll('[data-page]').forEach(el=>el.classList.toggle('active',el.dataset.page===page));
  if(page==='library') await loadLibrary();
  if(page==='settings'&&auth) try {const s=await api('/api/settings'); $('aiToggle').checked=s.ai_fallback;} catch(e){notice(e.message,true);}
}
document.querySelectorAll('[data-page]').forEach(el=>el.onclick=()=>navigate(el.dataset.page));
function mode(name) { $('mode-photo').hidden=name!=='photo'; $('mode-link').hidden=name!=='link'; document.querySelectorAll('[data-mode]').forEach(el=>el.classList.toggle('active',el.dataset.mode===name)); }
document.querySelectorAll('[data-mode]').forEach(el=>el.onclick=()=>{if(!busy)mode(el.dataset.mode);});
function resetInput() { if(busy){notice('Сначала дождись текущей загрузки.');return;} sourceRef=''; $('sourceNote').textContent=''; $('tracks').value=''; $('playlistUrl').value=''; $('genre').value=''; $('preview').hidden=true; clearNotices(); count(); navigate('add'); window.scrollTo({top:0,behavior:'smooth'}); }
$('newCollection').onclick=resetInput;
$('sample').onclick=()=>{sourceRef='';$('sourceNote').textContent='';$('tracks').value='Radiohead — No Surprises\nM83 — Midnight City\nКино — Группа крови';count();};
async function compressImage(file) {
  if(file.size>25000000)throw Error('Фото больше 25 МБ. Выбери скриншот поменьше.');
  const url=URL.createObjectURL(file); const image=new Image();
  try { await new Promise((resolve,reject)=>{image.onload=resolve;image.onerror=()=>reject(Error('Не удалось открыть фото. Выбери скриншот JPG или PNG.'));image.src=url;});
    const scale=Math.min(1,2000/Math.max(image.width,image.height));const canvas=document.createElement('canvas');canvas.width=Math.max(1,Math.round(image.width*scale));canvas.height=Math.max(1,Math.round(image.height*scale));const ctx=canvas.getContext('2d');ctx.fillStyle='#fff';ctx.fillRect(0,0,canvas.width,canvas.height);ctx.drawImage(image,0,0,canvas.width,canvas.height);
    const blob=await new Promise(resolve=>canvas.toBlob(resolve,'image/jpeg',.88));if(!blob||blob.size>6000000)throw Error('Не удалось уменьшить фото. Выбери другой скриншот.');return blob;
  } finally {URL.revokeObjectURL(url);}
}
function rememberJob(job, kind){activeJob={job,kind};try{localStorage.setItem(taskKey,JSON.stringify(activeJob));}catch{} }
function forgetJob(){activeJob=null;try{localStorage.removeItem(taskKey);}catch{} }
function progress(kind){$('progress').hidden=false;$('progressTitle').textContent=kind==='export'?'Создаём плейлист Spotify':kind==='ocr'?'Читаем скриншот':kind==='playlist'?'Загружаем плейлист':'Собираем подборки';$('progressText').textContent='Можно перейти в библиотеку — загрузка продолжится.';$('resume').hidden=true;}
async function pollJob(){
  if(!activeJob||polling)return;polling=true;let failures=0;const job=activeJob;
  const poll=async()=>{
    try {const result=await api('/api/jobs/'+job.job);failures=0;
      if(!activeJob||activeJob.job!==job.job){polling=false;return;}
      if(result.status==='running'){timer=setTimeout(poll,2000);return;}
      polling=false;lock(false);$('progress').hidden=true;forgetJob();
      if(result.status==='error')throw Error(result.error);
      if(job.kind==='export'){notice('Плейлист создан. Открыть: '+result.playlist_url);const open=node('a','Открыть плейлист Spotify →');open.href=result.playlist_url;open.target='_blank';open.rel='noopener noreferrer';$('info').append(document.createElement('br'),open);}
      else if(job.kind==='ocr'){
        // A screenshot starts its own import; never inherit a previous Spotify link's restrictions.
        sourceRef='';$('sourceNote').textContent='Скриншот';$('tracks').value=result.text;count();navigate('add');mode('photo');notice('Фото прочитано. Проверь названия и нажми «Собрать подборки».');
      }else if(job.kind==='playlist'){
        sourceRef=result.source_ref;$('sourceNote').textContent=({spotify:'Spotify · Last.fm',yandex:'Яндекс Музыка',youtube:'YouTube Music'}[result.platform]||'Музыкальный плейлист');$('tracks').value=result.text;count();navigate('add');mode('link');notice(result.title+' · '+result.count+' треков'+(result.cached?' · сохранённая копия за последние сутки':'')+(result.partial?' · получена только доступная часть плейлиста':'')+'. Проверь список.');
      }else{showCollection(result.body,result.body.library_id);notice('Подборки сохранены в библиотеке.');}
    }catch(error){
      if(activeJob&&++failures<=3){timer=setTimeout(poll,2000*failures);return;}
      polling=false;notice(error.message,true);$('resume').hidden=!activeJob;
      if(activeJob){$('progress').hidden=false;$('progressText').textContent='Связь прервалась. Задание может продолжаться на сервере.';}else{lock(false);$('progress').hidden=true;}
    }
  };await poll();
}
$('resume').onclick=()=>{if(activeJob){progress(activeJob.kind);pollJob();}};
async function submitJob(path,body,kind,raw=false){if(busy)return;clearNotices();lock(true);progress(kind);try{const result=raw?await api(path,{method:'POST',body}):await send(path,body);rememberJob(result.job,kind);pollJob();}catch(e){lock(false);$('progress').hidden=true;notice(e.message,true);}}
$('photo').onchange=async event=>{
  const file=event.target.files[0];if(!file||busy)return;
  if(!confirm('Отправить фото в Provod AI для распознавания?')){event.target.value='';return;}
  try{lock(true);const blob=await compressImage(file);const old=$('preview').src;if(old.startsWith('blob:'))URL.revokeObjectURL(old);$('preview').src=URL.createObjectURL(blob);$('preview').hidden=false;lock(false);await submitJob('/api/ocr',blob,'ocr',true);}catch(e){lock(false);notice(e.message,true);}finally{event.target.value='';}
};
$('loadPlaylist').onclick=()=>submitJob('/api/playlist',{url:$('playlistUrl').value.trim()},'playlist');
$('analyze').onclick=()=>{if(!$('tracks').value.trim()){notice('Добавь фото, ссылку или список песен.',true);return;}submitJob('/api/analyze',{text:$('tracks').value,source_ref:sourceRef,genre_hint:$('genre').value},'analyze');};
function save(text,name){const url=URL.createObjectURL(new Blob([text],{type:'text/plain;charset=utf-8'}));const a=document.createElement('a');a.href=url;a.download=name;a.click();setTimeout(()=>URL.revokeObjectURL(url),3000);}
function showCollection(body,id){current=body;currentId=id;currentPage='library';for(const name of ['add','library','settings'])$('page-'+name).hidden=true;$('results').hidden=false;document.querySelectorAll('[data-page]').forEach(x=>x.classList.toggle('active',x.dataset.page==='library'));$('resultTitle').textContent=body.name||'Мои подборки';const unknown=body.groups.unknown?.length||0;$('summary').textContent=body.scanned+' треков'+(unknown?' · '+unknown+' без надёжных тегов':'')+(body.ai_estimated?' · часть оценок AI':'');$('refreshSource').hidden=!body.source_url;$('autoSyncRow').hidden=!body.source_url;$('syncStatus').hidden=true;renderGroups();if(body.source_url&&id)api('/api/library/'+id).then(d=>setSync(d.sync)).catch(()=>{});window.scrollTo({top:0,behavior:'smooth'});}
async function loadLibrary(){const root=$('libraryList');root.replaceChildren(node('p','Загружаем…','muted'));try{const data=await api('/api/library');root.replaceChildren();if(!data.items.length){const box=node('div',undefined,'empty');box.append(node('strong','Здесь будет твоя музыка'),node('span','Добавь песни — подборки сохранятся автоматически.'));root.append(box);return;}for(const item of data.items){const card=node('button',undefined,'collection');card.append(node('span',item.name,'collection-title'),node('small',item.scanned+' треков · '+new Date(item.created*1000).toLocaleDateString('ru')));const chips=node('div',undefined,'chips');for(const key of Object.keys(item.categories).slice(0,5))chips.append(node('span',labels[key]||key,'chip'));if(item.sync?.enabled)chips.append(node('span','Авто ↻','chip'));card.append(chips);card.onclick=async()=>{try{const detail=await api('/api/library/'+item.id);showCollection(detail.body,detail.id);setSync(detail.sync);}catch(e){notice(e.message,true);}};root.append(card);}}catch(e){root.replaceChildren(node('p',e.message,'muted'));}}
function validSpotify(track){return /^https:\/\/open\.spotify\.com\/track\/[A-Za-z0-9]{22}$/.test(track.url||'');}
function renderGroups(){const root=$('groups');root.replaceChildren();if(!current)return;for(const[key,tracks]of Object.entries(current.groups)){if(filter==='mood'&&!moods.has(key)||filter==='genre'&&(moods.has(key)||key==='unknown'))continue;const card=node('article',undefined,'group');const head=node('div',undefined,'group-head');head.append(node('h3',labels[key]||key),node('small',tracks.length+' треков'));card.append(head);let expanded=false;const list=node('div');const draw=()=>{list.replaceChildren();for(const[at,track]of tracks.slice(0,expanded?tracks.length:5).entries()){const row=node('div',undefined,'track');row.append(node('span',String(at+1),'track-index'));const text=node('div');text.append(node('span',track.title,'track-title'),node('span',track.artists.join(', '),'track-artist'));if(track.tag_source==='ai')text.append(node('p','Оценка AI'));if(track.tag_source==='user')text.append(node('p','Твой жанр'));row.append(text);if(validSpotify(track)){const play=node('button','▶','play');play.setAttribute('aria-label','Плеер Spotify: '+track.title);play.onclick=()=>{const old=card.querySelector('.player');if(old)old.remove();const box=node('div',undefined,'player');const frame=document.createElement('iframe');frame.src='https://open.spotify.com/embed/track/'+track.url.split('/').pop();frame.title='Spotify: '+track.title;frame.allow='autoplay; clipboard-write; encrypted-media; fullscreen; picture-in-picture';frame.loading='lazy';box.append(frame,node('p','Плеер Spotify. Полное воспроизведение зависит от аккаунта и доступности трека.'));card.append(box);};row.append(play);}else if(youtubeId(track)){const play=node('button','▶','play');play.onclick=()=>youtubePlayer(card,[youtubeId(track)]);row.append(play);}else{const yandex=/^https:\/\/music\.yandex\.ru\/(?:album\/\d+\/)?track\/\d+$/.test(track.url||'');const link=node('a',yandex?'Яндекс →':'Найти →');link.href=yandex?track.url:'https://open.spotify.com/search/'+encodeURIComponent(track.artists.join(' ')+' '+track.title);link.target='_blank';link.rel='noopener noreferrer';row.append(link);}list.append(row);}};draw();card.append(list);if(tracks.length>5){const more=node('button','Все '+tracks.length+' треков','more');more.onclick=()=>{expanded=!expanded;draw();more.textContent=expanded?'Свернуть':'Все '+tracks.length+' треков';};card.append(more);}if(key==='unknown'){const tip=node('p','Жанр не подтверждён. Выбери известный жанр для этих песен — сохраним его без AI.','hint');card.append(tip);const repair=node('select');repair.setAttribute('aria-label','Жанр для треков без категории');for(const [value,title] of Object.entries({'':'Выбери жанр',...genreOptions})){const option=node('option',title);option.value=value;repair.append(option);}const apply=node('button','Применить жанр','secondary');apply.onclick=async()=>{if(!repair.value)return;apply.disabled=true;try{const data=await send('/api/library/'+currentId+'/genre',{genre_hint:repair.value});showCollection(data.body,data.id);}catch(e){notice(e.message,true);}finally{apply.disabled=false;}};card.append(repair,apply);}const actions=node('div',undefined,'actions');const videoIds=tracks.map(youtubeId).filter(Boolean);if(videoIds.length){const listen=node('button','▶ Очередь YouTube');listen.onclick=()=>youtubePlayer(card,videoIds);actions.append(listen);}const text=tracks.map(t=>t.artists.join(', ')+' — '+t.title).join('\n');if(tracks.length&&tracks.every(t=>/^spotify:track:[A-Za-z0-9]{22}$/.test(t.uri||''))){const exp=node('button','Создать в Spotify');exp.onclick=()=>{if(confirm('Создать новый приватный Spotify-плейлист «'+(labels[key]||key)+'» с '+tracks.length+' треками?'))submitJob('/api/library/'+currentId+'/spotify',{category:key},'export');};actions.append(exp);}const copy=node('button','Копировать');copy.onclick=async()=>{try{await navigator.clipboard.writeText(text);copy.textContent='Скопировано ✓';}catch{notice('Копирование недоступно. Скачай TXT.',true);}};const download=node('button','TXT');download.onclick=()=>save(text,'VibeSort-'+key+'.txt');actions.append(copy,download);card.append(actions);root.append(card);}if(!root.children.length)root.append(node('p','В этой категории пока нет подборок.','muted'));}
document.querySelectorAll('[data-filter]').forEach(button=>button.onclick=()=>{filter=button.dataset.filter;document.querySelectorAll('[data-filter]').forEach(x=>x.classList.toggle('active',x===button));renderGroups();});
$('backLibrary').onclick=()=>navigate('library');
$('rename').onclick=async()=>{if(!currentId)return;const name=prompt('Название подборки',current.name||'Мои подборки');if(!name?.trim())return;try{const data=await send('/api/library/'+currentId,{name},'PATCH');showCollection(data.body,data.id);}catch(e){notice(e.message,true);}};
$('deleteCollection').onclick=async()=>{if(!currentId||!confirm('Удалить эту подборку из библиотеки?'))return;try{await api('/api/library/'+currentId,{method:'DELETE'});current=null;currentId='';navigate('library');}catch(e){notice(e.message,true);}};
$('aiToggle').onchange=async()=>{try{await send('/api/settings',{ai_fallback:$('aiToggle').checked},'PUT');notice('Настройки сохранены.');}catch(e){$('aiToggle').checked=!$('aiToggle').checked;notice(e.message,true);}};
$('deleteData').onclick=async()=>{if(!confirm('Удалить библиотеку, настройки и подключение Spotify? Это действие нельзя отменить.'))return;try{await api('/api/data',{method:'DELETE'});forgetJob();current=null;currentId='';resetInput();notice('Данные удалены.');}catch(e){notice(e.message,true);}};
try{const saved=JSON.parse(localStorage.getItem(taskKey)||'null');if(saved&&typeof saved.job==='string'&&['ocr','analyze','playlist','export'].includes(saved.kind)){activeJob=saved;lock(true);progress(saved.kind);pollJob();}}catch{}

function setSync(state){if(!state)return;$('autoSync').checked=!!state.enabled;$('syncStatus').hidden=!state.enabled;$('syncStatus').textContent=state.error||'Новые песни будут попадать в эту подборку автоматически.';}
function youtubeId(track){const match=/^youtube:video:([A-Za-z0-9_-]{11})$/.exec(track.uri||'');return match?match[1]:null;}
function youtubePlayer(card,ids){card.querySelector('.player')?.remove();const box=node('div',undefined,'player');const frame=document.createElement('iframe');const queue=ids.slice(0,50);frame.src='https://www.youtube-nocookie.com/embed/'+queue[0]+'?playsinline=1&rel=0'+(queue.length>1?'&playlist='+queue.slice(1).join(','):'');frame.className='youtube-player';frame.title='Очередь YouTube';frame.allow='autoplay; encrypted-media; fullscreen; picture-in-picture';box.append(frame,node('p',queue.length+' видео в очереди. Доступность определяется YouTube.'));card.append(box);}
$('reanalyze').onclick=()=>{if(currentId)submitJob('/api/library/'+currentId+'/reanalyze',{},'analyze');};
$('refreshSource').onclick=()=>{if(currentId)submitJob('/api/library/'+currentId+'/refresh',{},'analyze');};
$('autoSync').onchange=async()=>{try{const state=await send('/api/library/'+currentId+'/sync',{enabled:$('autoSync').checked},'PUT');setSync(state);}catch(e){$('autoSync').checked=!$('autoSync').checked;notice(e.message,true);}};
(async()=>{try{const c=await fetch('/api/catalog').then(r=>{if(!r.ok)throw Error('catalog');return r.json();});if(!c.genres||!c.labels)throw Error('catalog');Object.assign(labels,c.labels);genreOptions=c.genres;const select=$('genre');select.replaceChildren();for(const [value,title] of Object.entries({'':'Определить автоматически',...genreOptions})){const o=node('option',title);o.value=value;select.append(o);}if(c.sources)$('platformStatus').textContent='Spotify: открытые плейлисты · Яндекс: импорт зависит от региона · YouTube: '+(c.sources.youtube?'ключ подключён':'нужен YOUTUBE_API_KEY');}catch{}})();

$('spotifyLikes').onclick=()=>submitJob('/api/spotify/import',{},'analyze');
