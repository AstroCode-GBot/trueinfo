const $ = (s) => document.querySelector(s);

function esc(x){
  return String(x ?? "").replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#039;'}[c]));
}
function pretty(data){ try{return JSON.stringify(data,null,2)}catch{return String(data)} }

(function initTheme(){
  const saved = localStorage.getItem("trueinfo-theme");
  const dark = saved ? saved === "dark" : matchMedia("(prefers-color-scheme:dark)").matches;
  document.body.classList.toggle("dark", dark);
})();
$("#themeBtn")?.addEventListener("click",()=>{
  document.body.classList.toggle("dark");
  localStorage.setItem("trueinfo-theme", document.body.classList.contains("dark")?"dark":"light");
});



$("#openLogin")?.addEventListener("click",()=>{
  $("#loginModal")?.classList.remove("hidden");
});
$("#closeLogin")?.addEventListener("click",()=>{
  $("#loginModal")?.classList.add("hidden");
});

$("#logoutBtn")?.addEventListener("click", async ()=>{
  await fetch("/logout",{method:"POST"});
  location.reload();
});

async function lookup(){
  const val = ($("#number").value || "").replace(/\D/g,"");
  if(!val){ alert("Enter a phone number"); return; }
  const btn=$("#searchBtn");
  btn.classList.add("loading");
  btn.querySelector(".spinner").classList.remove("hidden");
  try{
    const r=await fetch("/api/lookup",{
      method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({number: "0"+val})
    });
    const j=await r.json();
    if(!j.ok){ alert(j.error || "Lookup failed"); return; }
    const grid=$("#resultGrid");
    const data=j.data;
    if(typeof data !== "object"){ grid.innerHTML=`<div class="result-card"><div class="v">${esc(data)}</div></div>`; return; }
    const entries=Object.entries(data);
    grid.innerHTML=entries.map(([k,v])=>{
      const text=typeof v==="object"?pretty(v):String(v??"");
      return `<div class="result-card"><div class="k">${esc(k)}</div><div class="v">${esc(text)}</div></div>`;
    }).join("");
  }catch(e){ alert("Network error"); }
  finally{
    btn.classList.remove("loading");
    btn.querySelector(".spinner").classList.add("hidden");
  }
}
$("#searchBtn")?.addEventListener("click", lookup);
$("#number")?.addEventListener("keydown",e=>{if(e.key==="Enter")lookup()});

async function premiumPost(url, body, out){
  const r=await fetch(url,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
  const j=await r.json();
  out.textContent=pretty(j);
}
$("#tgToPhoneBtn")?.addEventListener("click",()=>premiumPost(
  "/api/premium/tg-to-phone",
  {userid:$("#tgUserId").value.trim()},
  $("#tgToPhoneOut")
));
$("#smsBtn")?.addEventListener("click",()=>premiumPost(
  "/api/premium/sms",
  {number:$("#smsNumber").value.trim(),message:$("#smsMessage").value},
  $("#smsOut")
));

$("#historyBtn")?.addEventListener("click", async ()=>{
  const r=await fetch("/api/history");
  const j=await r.json();
  if(!j.ok){$("#historyOut").textContent=j.error||"Failed";return}
  $("#historyOut").innerHTML=j.items.map((x,i)=>{
    return `<div class="history-item"><b>${i+1}. ${esc(x.query_value)}</b><span>${esc(x.source)} • ${esc(x.created_at)}</span></div>`;
  }).join("") || `<div class="history-item">No history yet.</div>`;
});
