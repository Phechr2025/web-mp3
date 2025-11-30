
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
const sourceSel = $("#source");
const formatSel = $("#format");
const qualityRow = $("#qualityRow");
const qualitySel = $("#quality");
const mp4ModeRow = $("#mp4ModeRow");
const mp4ModeSel = $("#mp4Mode");
const qualityExactRow = $("#qualityExactRow");
const qualityExactSel = $("#qualityExact");
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
  const updateQualityUI = ()=>{
    const fmt = formatSel.value;
    const src = sourceSel ? sourceSel.value : "youtube";

    if(fmt !== 'mp4'){
      hide(qualityRow);
      hide(mp4ModeRow);
      hide(qualityExactRow);
      return;
    }

    // MP4 เท่านั้น
    if(mp4ModeRow) show(mp4ModeRow);

    if(!qualitySel) return;

    if(src === 'tiktok'){
      // TikTok: ใช้โหมดโดยรวมเท่านั้น (ต่ำ/กลาง/สูง)
      if(mp4ModeSel){ mp4ModeSel.value = 'range'; }
      // แสดงเฉพาะแถวคุณภาพโดยรวม
      show(qualityRow);
      hide(qualityExactRow);
      qualitySel.innerHTML = `
        <option value="low">ต่ำ</option>
        <option value="medium">กลาง</option>
        <option value="high">สูง</option>
      `;
    }else{
      // แพลตฟอร์มทั่วไป (YouTube, Bilibili ฯลฯ)
      if(mp4ModeSel && mp4ModeSel.value === 'exact'){
        // โหมดเจาะจงความละเอียด
        hide(qualityRow);
        if(qualityExactRow) show(qualityExactRow);
      }else{
        // โหมดโดยรวม ต่ำ/กลาง/สูง
        show(qualityRow);
        hide(qualityExactRow);
      }

      // ตั้งข้อความของโหมดโดยรวม (ต่ำ/กลาง/สูง)
      qualitySel.innerHTML = `
        <option value="low">ต่ำ (สูงสุด 480p)</option>
        <option value="medium">กลาง (สูงสุด 720p)</option>
        <option value="high">สูง (สูงสุด 1080p)</option>
      `;
    }
  };

  formatSel.addEventListener('change', updateQualityUI);
  if(sourceSel){ sourceSel.addEventListener('change', updateQualityUI); }
  if(mp4ModeSel){ mp4ModeSel.addEventListener('change', updateQualityUI); }
  // ตั้งค่าเริ่มต้น
  updateQualityUI();
}


if(yes){
  yes.addEventListener('click', ()=>{
    hide(confirmBox);
    const url = $("#url").value.trim();
    const format = $("#format").value;
    const title = $("#title").value.trim();
    const source = sourceSel ? sourceSel.value : "youtube";

    let quality = null;
    if(format === 'mp4'){
      if(source === 'tiktok'){
        // TikTok: ใช้เฉพาะโหมดโดยรวม ต่ำ/กลาง/สูง
        quality = qualitySel ? qualitySel.value : 'high';
      }else{
        const mode = mp4ModeSel ? mp4ModeSel.value : 'range';
        if(mode === 'exact'){
          quality = qualityExactSel ? qualityExactSel.value : '720p';
        }else{
          quality = qualitySel ? qualitySel.value : 'high';
        }
      }
    }
    hide(doneWrap);
    hide(pwrap);
    hide(btn);
    show(pwrap);
    label.textContent = "เริ่ม...";
    fetch('/api/create', {
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({url, format, title, quality, source})
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