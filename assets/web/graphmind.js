const cookie=n=>document.cookie.split('; ').find(x=>x.startsWith(n+'='))?.split('=').slice(1).join('=');
const csrf=()=>decodeURIComponent(cookie('graphmind_csrf')||'');
async function post(url,body){
  const send=()=>fetch(url,{method:'POST',headers:{'Content-Type':'application/json','X-GraphMind-CSRF':csrf()},body:JSON.stringify(body)});
  let r=await send();
  if(r.status===401&&url!='/auth/refresh'){
    const q=await fetch('/auth/refresh',{method:'POST',headers:{'X-GraphMind-CSRF':csrf()}});
    if(q.ok)r=await send();else return q;
  }
  return r;
}
async function resumeSession(){
  const resume=document.getElementById('resume');if(!resume)return;
  const status=document.getElementById('status');
  try{
    const r=await post('/auth/refresh',{});
    if(r.ok){location.replace(resume.dataset.returnTo);return;}
    status.textContent=r.status===401?'Your session expired. Sign in again.':'Session renewal is unavailable. Retry or sign in.';
  }catch{status.textContent='Session renewal failed. Retry or sign in.';}
}
document.getElementById('ask-form')?.addEventListener('submit',async e=>{
  e.preventDefault();
  const status=document.getElementById('status'),answer=document.getElementById('answer'),list=document.getElementById('citations');
  const button=e.currentTarget.querySelector('button[type="submit"]');button.disabled=true;
  status.textContent='Working…';answer.textContent='';list.replaceChildren();
  try{
    const r=await post('/api/ask',{question:document.getElementById('question').value});
    const p=await r.json();
    if(!r.ok){status.textContent=p.message||'Request failed';return;}
    if(!['answer','insufficient_evidence'].includes(p.outcome))throw new Error('Invalid response');
    status.textContent=p.outcome==='answer'?'Answered':'No supported answer';answer.textContent=p.answer||'';
    (p.citations||[]).forEach(c=>{
      const li=document.createElement('li'),a=document.createElement('a'),x=document.createElement('div');
      a.href='/documents/'+encodeURIComponent(c.document_id)+'?version_id='+encodeURIComponent(c.version_id);
      a.textContent=(c.display_name||'Source')+' — '+(c.locator||'');x.className='excerpt';x.textContent=c.excerpt||'';
      li.append(a,x);list.append(li);
    });
  }catch{status.textContent='Request failed. Please retry.';}
  finally{button.disabled=false;}
});
document.getElementById('logout')?.addEventListener('click',async()=>{
  try{const r=await post('/logout',{});if(!r.ok)throw new Error('Sign-out failed');location.href='/';}
  catch{document.getElementById('status').textContent='Sign-out could not be confirmed. Please retry.';}
});
void resumeSession();
