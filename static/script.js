
const $ = sel => document.querySelector(sel);
const show = el => {
  if(!el) return;
  el.classList.remove('hide');
  el.style.removeProperty('display');
};
const hide = el => {
  if(!el) return;
  el.classList.add('hide');
  el.style.display = 'none';
};

const btn = $("#btnStart");
const confirmBox = $("#confirm");
const formatSel = $("#format");
const qualityRow = $("#qualityRow");
const qualitySel = $("#quality");
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
        hide(btn);
        show(doneWrap);
      }else if(job.status === 'error'){
        clearInterval(timer);
        alert("เกิดข้อผิดพลาด: " + (job.error||'unknown'));
        hide(pwrap);
        show(btn);
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

if(formatSel && qualityRow){
  // แสดงตัวเลือกคุณภาพเมื่อเลือก MP4 เท่านั้น
  const toggleQuality = ()=>{
    if(formatSel.value === 'mp4'){
      show(qualityRow);
    }else{
      hide(qualityRow);
    }
  };
  formatSel.addEventListener('change', toggleQuality);
  // ตั้งค่าเริ่มต้น
  toggleQuality();
}

if(yes){
  yes.addEventListener('click', ()=>{
    hide(confirmBox);
    const url = $("#url").value.trim();
    const format = $("#format").value;
    const title = $("#title").value.trim();
    const quality = qualitySel ? qualitySel.value : null;
    hide(doneWrap);
    hide(pwrap);
    hide(btn);
    show(pwrap);
    label.textContent = "เริ่ม...";
    fetch('/api/create', {
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({url, format, title, quality})
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

if(dlLink){
  dlLink.addEventListener('click', ()=>{
    // หลังจากกดดาวน์โหลดแล้ว กลับมาแสดงปุ่มเริ่มดาวน์โหลดอีกครั้ง
    hide(doneWrap);
    show(btn);
    // รีเซ็ตช่องลิงก์และชื่อไฟล์ให้ว่าง เพื่อพร้อมสำหรับงานถัดไป
    const urlInput = $("#url");
    const titleInput = $("#title");
    if(urlInput) urlInput.value = "";
    if(titleInput) titleInput.value = "";
  });
}