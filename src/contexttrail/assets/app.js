"use strict";
const params = new URLSearchParams(location.hash.slice(1));
const token = params.get("token") || sessionStorage.getItem("contexttrail.token") || "";
if (token) sessionStorage.setItem("contexttrail.token", token);
let version = params.get("v"), selected = params.get("event"), generation = 0;
const $ = (id) => document.getElementById(id);
// Screen texts follow the page language the server chose (the html lang attribute).
const L = document.documentElement.lang === "ko" ? {
  names: {proposed:"제안",adopted:"채택",in_progress:"진행 중",asked:"요청",applied:"변경 적용",reported_complete:"완료 보고 · 미검증",observed_success:"관측 성공",observed_failure:"관측 실패",withdrawn:"철회",unknown:"미확인"},
  tokenMissing:"접근 토큰이 없거나 만료되었습니다. 터미널의 B로 새 주소를 확인하세요.",
  requestFailed:(status)=>"요청을 처리하지 못했습니다 ("+status+")",
  actor:"주체: ", copyReference:"에이전트용 참조 복사",
  copyHint:"Claude Code·Codex 대화에 붙여넣으면 에이전트가 이 사건을 근거와 함께 읽습니다",
  copied:"복사함", selectedCopy:"선택함 · 복사하세요", relationEvidence:"관계 근거: ",
  sourceLines:(a,b)=>"원문 줄 "+a+"–"+b, unknownTime:"작성 시각 미상",
  fullSource:(n)=>"전체 원문 줄 ("+n+"자)", citedPart:"인용 부분",
  sourceChanged:"원본이 바뀌었습니다. 여기에는 분석 당시 보존한 발췌를 표시합니다.",
  sourceMissing:"현재 원본 부재. 보존한 발췌 밖의 문맥은 확인할 수 없습니다.",
  graphVersion:(v,at)=>"그래프 v"+v+" · 분석 기준 "+(at||"없음"),
  synthetic:"합성 데이터 / Mock 분석 · ", lastCheck:(at,status)=>"마지막 확인: "+(at||"없음")+" · "+status,
  badSVG:"지원하지 않는 SVG 요소입니다.",
  confirmRefresh:"이 프로젝트의 새 대화·코드 근거가 선택한 CLI의 클라우드 모델에 전송될 수 있습니다. 변경분 분석을 실행할까요?",
  refreshing:"변경분 분석 중 · 이전 그림을 유지합니다."
} : {
  names: {proposed:"proposed",adopted:"adopted",in_progress:"in progress",asked:"asked",applied:"applied",reported_complete:"reported done · unverified",observed_success:"observed success",observed_failure:"observed failure",withdrawn:"withdrawn",unknown:"unknown"},
  tokenMissing:"The access token is missing or expired. Press B in the terminal for a new link.",
  requestFailed:(status)=>"The request could not be handled ("+status+")",
  actor:"actor: ", copyReference:"Copy reference for an agent",
  copyHint:"Paste it into a Claude Code or Codex conversation and the agent reads this event with its evidence",
  copied:"copied", selectedCopy:"selected · copy it", relationEvidence:"relation evidence: ",
  sourceLines:(a,b)=>"source lines "+a+"–"+b, unknownTime:"time unknown",
  fullSource:(n)=>"whole source line ("+n+" characters)", citedPart:"cited part",
  sourceChanged:"The source has changed. Shown here is the excerpt preserved at analysis time.",
  sourceMissing:"The source is gone. Context beyond the preserved excerpt cannot be checked.",
  graphVersion:(v,at)=>"graph v"+v+" · analyzed as of "+(at||"none"),
  synthetic:"synthetic data / mock analysis · ", lastCheck:(at,status)=>"last check: "+(at||"none")+" · "+status,
  badSVG:"Unsupported SVG element.",
  confirmRefresh:"New conversation and code evidence of this project may be sent to the chosen CLI's cloud model. Analyze the changes?",
  refreshing:"Analyzing changes · the previous picture stays."
};
function el(tag, text, cls) { const node=document.createElement(tag); if(text!==undefined)node.textContent=text;if(cls)node.className=cls;return node; }
async function api(path, options={}) {
  const suffix=version ? (path.includes("?")?"&":"?")+"version="+encodeURIComponent(version):"";
  const response=await fetch(path+suffix,{...options,headers:{Authorization:"Bearer "+token,...(options.headers||{})},cache:"no-store"});
  if(!response.ok)throw new Error(response.status===401?L.tokenMissing:L.requestFailed(response.status));
  return response;
}
async function select(id) {
  selected=id; const thisGeneration=++generation;
  const data=await(await api("/events/"+encodeURIComponent(id))).json();
  if(thisGeneration!==generation)return;
  const e=data.event, area=$("detail");area.replaceChildren();
  area.append(el("span",e.status_label||L.names[e.status]||e.status,"badge"),el("span",e.basis,"badge"),el("h3",e.title),el("p",e.summary),el("p",L.actor+e.actor,"muted"),el("small",e.id,"muted"));
  if(e.reference){const copy=el("button",L.copyReference,"copy"),ref=el("code",e.reference);
    copy.title=L.copyHint;
    copy.onclick=async()=>{try{await navigator.clipboard.writeText(e.reference);copy.textContent=L.copied;}
      catch(_){const range=document.createRange();range.selectNodeContents(ref);getSelection().removeAllRanges();getSelection().addRange(range);copy.textContent=L.selectedCopy;}};
    const row=el("p",undefined,"reference");row.append(copy,ref);area.append(row);}
  for(const relation of (data.relations||[])){const box=el("section",undefined,"evidence");
    box.append(el("h4",relation.from_title+" → "+relation.to_title),el("p",(relation.relation_label||relation.relation)+" · "+relation.basis),el("p",relation.rationale),el("small",L.relationEvidence+relation.evidence_ids.join(", ")));area.append(box);}
  for(const item of data.evidence){const box=el("section",undefined,"evidence");box.append(el("h4",item.source.provider+" · "+item.source.role),el("small",item.id),el("small",L.sourceLines(item.start_line,item.end_line)+" · "+(item.source.recorded_at||L.unknownTime)));
    if(item.excerpt){const full=el("details");full.append(el("summary",L.fullSource(item.quote.length)),el("pre",item.quote));box.append(el("small",L.citedPart),el("pre",item.excerpt),full);}
    else box.append(el("pre",item.quote));
    const locator=item.source.locator; box.append(el("small",JSON.stringify(locator)));
    if(item.original_state!=="available_at_last_scan")box.append(el("small",item.original_state==="changed"?L.sourceChanged:L.sourceMissing));
    area.append(box);}
  for(const node of document.querySelectorAll("[data-event-id]"))node.classList.toggle("selected",node.dataset.eventId===id);
  const fragment=new URLSearchParams();if(version)fragment.set("v",version);fragment.set("event",id);history.replaceState(null,"","#"+fragment);
}
async function load() {
  const payload=await(await api("/graph")).json(), graph=payload.graph;
  version=String(graph.version||"");$("version").textContent=L.graphVersion(graph.version,graph.analyzed_at);
  $("status").textContent=(graph.analysis_mode==="synthetic_mock"?L.synthetic:"")+L.lastCheck(payload.last_check.at,payload.last_check.status||graph.analysis_status)+(payload.freshness?" · "+payload.freshness:"");
  $("refresh").disabled=!payload.can_refresh||payload.refreshing;
  const text=await(await api("/graph.svg")).text();
  // This endpoint contains host-generated escaped SVG, not LLM HTML. Enforce a second
  // local whitelist before importing any element into the document.
  const documentSVG=new DOMParser().parseFromString(text,"image/svg+xml");
  const root=documentSVG.documentElement, allowed=new Set(["svg","defs","marker","path","g","rect","text"]);
  if(root.localName!=="svg"||[...root.querySelectorAll("*")].some(n=>!allowed.has(n.localName)))throw new Error(L.badSVG);
  for(const node of [root,...root.querySelectorAll("*")])for(const attribute of [...node.attributes])if(/^on/i.test(attribute.name)||["href","xlink:href","style"].includes(attribute.name))node.removeAttribute(attribute.name);
  $("graph").replaceChildren(document.importNode(root,true));
  for(const node of $("graph").querySelectorAll("[data-event-id]")){node.addEventListener("click",()=>select(node.dataset.eventId).catch(showError));node.addEventListener("keydown",e=>{if(e.key==="Enter")select(node.dataset.eventId).catch(showError);});}
  $("limits").replaceChildren();for(const message of [...new Set([...(graph.limitations||[]),...(graph.input_limitations||[]),...(payload.last_check.limitations||[])])])$("limits").append(el("p",message));
  for(const item of (graph.open_items||[]))$("limits").append(el("p","["+item.status+"] "+item.text));
  if(payload.last_check.error)$("limits").append(el("p",payload.last_check.error));
  if(selected&&graph.events.some(e=>e.id===selected))await select(selected);else if(graph.events.length)await select(graph.events[0].id);
}
function showError(error){$("status").textContent=error.message;}
$("refresh").addEventListener("click",async()=>{
  if(!confirm(L.confirmRefresh))return;
  $("refresh").disabled=true;
  try{await api("/refresh",{method:"POST",headers:{"Content-Type":"application/json","X-Projectflow-Action":"refresh"},body:JSON.stringify({confirm:true})});
    $("status").textContent=L.refreshing;
    const poll=async()=>{try{const result=await(await api("/graph")).json();if(result.refreshing){setTimeout(poll,1200);return;}version=null;await load();}catch(e){showError(e);$("refresh").disabled=false;}};setTimeout(poll,700);
  }catch(e){showError(e);$("refresh").disabled=false;}
});
if(token)load().catch(showError);
