const $ = (s) => document.querySelector(s);
function esc(x){return String(x??"").replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#039;'}[c]));}
async function loadUsers(){
  const q=$("#userSearch").value.trim();
  const j=await (await fetch("/adm/api/users?q="+encodeURIComponent(q))).json();
  const body=$("#usersBody");
  body.innerHTML=(j.items||[]).map(u=>`
    <tr>
      <td><b>${esc(u.first_name||u.username||"Unknown")}</b><br><span>${esc(u.username?"@"+u.username:"")}</span></td>
      <td>${u.tg_id}</td>
      <td>${esc(u.last_seen)}</td>
      <td>${u.is_premium?"⭐ Premium":"Free"}</td>
      <td>${u.is_blocked?"⛔ Blocked":"✅ Active"}</td>
      <td><div class="mini-actions">
        <button onclick="toggleUser(${u.tg_id},'${u.is_blocked?'unblock':'block'}')">${u.is_blocked?'Unblock':'Block'}</button>
        <button onclick="toggleUser(${u.tg_id},'${u.is_premium?'premium_off':'premium_on'}')">${u.is_premium?'Remove Premium':'Premium'}</button>
        <button onclick="showHistory(${u.tg_id})">History</button>
      </div></td>
    </tr>`).join("");
}
async function toggleUser(id,action){
  await fetch(`/adm/api/user/${id}/toggle`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({action})});
  loadUsers();
}
async function showHistory(id){
  const j=await (await fetch(`/adm/api/user/${id}/history`)).json();
  $("#historyUserLabel").textContent="User "+id;
  const items=(j.lookups||[]).map((x,i)=>`
    <div class="history-item"><b>${i+1}. ${esc(x.query_value)}</b><span>${esc(x.source)} • ${esc(x.created_at)} • ${x.success?"Success":"Failed"}</span></div>`
  );
  $("#userHistory").innerHTML=items.join("") || `<div class="history-item">No lookup history.</div>`;
}
$("#userSearch").addEventListener("input",()=>{clearTimeout(window.ut);window.ut=setTimeout(loadUsers,250)});
$("#saveSettings").addEventListener("click",async()=>{
  const body={
    bot_enabled:$("#botEnabled").checked,
    popup_enabled:$("#popupEnabled").checked,
    popup_notice:$("#popupNotice").value
  };
  const j=await (await fetch("/adm/api/settings",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)})).json();
  alert(j.ok?"Saved":"Failed");
});
$("#broadcastBtn").addEventListener("click",async()=>{
  const message=$("#broadcastText").value.trim();
  if(!message)return alert("Write a message");
  if(!confirm("Send this message to all unblocked users?"))return;
  const j=await (await fetch("/adm/api/broadcast",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({message})})).json();
  $("#broadcastOut").textContent=JSON.stringify(j,null,2);
});
loadUsers();
