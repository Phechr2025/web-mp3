
const $ = sel => document.querySelector(sel);
const show = el => el.classList.remove('hide');
const hide = el => el.classList.add('hide');

const btn = $("#btnStart");
const confirmBox = $("#confirm");
const yes = $("#confirmYes");
const no = $("#confirmNo");
const pwrap = $("#progressWrap");
const bar = $("#bar");
const label = $("#label");
const doneWrap = $("#doneWrap");
const dlLink = $("#dlLink");

let currentJob = null;
let timer = null;

function poll(){
  fetch(`/api/progress/${currentJob}`)
    .then(r=>r.json())
    .then(({ok,job})=>{
      if(!ok) return;
      bar.style.width = (job.progress||0) + '%';
      label.textContent = (job.progress||0) + '% - ' + (job.status||'');
      if(job.status === 'done'){
        clearInterval(timer);
        hide(pwrap);
        dlLink.href = `/download/${currentJob}`;
        show(doneWrap);
      }else if(job.status === 'error'){
        clearInterval(timer);
        alert("เกิดข้อผิดพลาด: " + (job.error||'unknown'));
        hide(pwrap);
      }
    })
    .catch(()=>{});
}

if(btn){
  btn.addEventListener('click', ()=>{
    show(confirmBox);
  });
}
if(no){ no.addEventListener('click', ()=> hide(confirmBox)); }

if(yes){
  yes.addEventListener('click', ()=>{
    hide(confirmBox);
    const url = $("#url").value.trim();
    const format = $("#format").value;
    const title = $("#title").value.trim();
    hide(doneWrap);
    show(pwrap);
    label.textContent = "เริ่ม...";
    fetch('/api/create', {
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({url, format, title})
    })
    .then(r=>r.json())
    .then(({ok, job_id, error})=>{
      if(!ok){ hide(pwrap); alert("เริ่มงานไม่สำเร็จ: "+error); return; }
      currentJob = job_id;
      timer = setInterval(poll, 1000);
    })
    .catch(e=>{ hide(pwrap); alert("มีข้อผิดพลาดเครือข่าย"); });
  });
}
