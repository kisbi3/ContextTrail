"use strict";
const params = new URLSearchParams(location.hash.slice(1));
const token = params.get("token") || sessionStorage.getItem("contexttrail.token") || "";
if (token) sessionStorage.setItem("contexttrail.token", token);
let version = params.get("v"), selected = params.get("event"), generation = 0, loading = 0, shown = null, refreshWith = "";
// A link opened at a version stays at it; otherwise a reload shows the latest graph (an analysis may have run since).
const pinned = params.get("v") !== null;
// The story view (requests, decisions, findings; the work a line in its box) or every step; the story by default.
let view = params.get("view") || sessionStorage.getItem("contexttrail.view") || "story";
const $ = (id) => document.getElementById(id);
// Screen texts follow the page language the server chose (the html lang attribute).
const L = document.documentElement.lang === "ko" ? {
  names: {proposed:"제안",adopted:"채택",in_progress:"진행 중",asked:"요청",applied:"변경 적용",reported_complete:"완료 보고 · 미검증",observed_success:"관측 성공",observed_failure:"관측 실패",withdrawn:"철회",unknown:"미확인"},
  tokenMissing:"접근 토큰이 없거나 만료되었습니다. 터미널의 B로 새 주소를 확인하세요.",
  requestFailed:(status)=>"요청을 처리하지 못했습니다 ("+status+")",
  actor:"주체: ", agents:"에이전트: ", analysis:"분석: ", noPerson:"사람 없음", copyReference:"에이전트용 참조 복사",
  copyHint:"Claude Code·Codex 대화에 붙여넣으면 에이전트가 이 사건을 근거와 함께 읽습니다",
  copied:"복사함", selectedCopy:"선택함 · 복사하세요", relationEvidence:"관계 근거: ",
  sourceLines:(a,b)=>"원문 줄 "+a+"–"+b, unknownTime:"작성 시각 미상",
  fullSource:(n)=>"전체 원문 줄 ("+n+"자)", citedPart:"인용 부분",
  sourceChanged:"원본이 바뀌었습니다. 여기에는 분석 당시 보존한 발췌를 표시합니다.",
  sourceMissing:"현재 원본 부재. 보존한 발췌 밖의 문맥은 확인할 수 없습니다.",
  graphVersion:(v,at)=>"그래프 v"+v+" · 분석 기준 "+(at||"없음"),
  synthetic:"합성 데이터 / Mock 분석 · ", lastCheck:(at,status)=>"마지막 확인: "+(at||"없음")+" · "+status,
  badSVG:"지원하지 않는 SVG 요소입니다.",
  confirmRefresh:(runner)=>`이 프로젝트의 새 대화·코드 근거가 ${runner}의 클라우드 모델에 전송될 수 있습니다. 변경분 분석을 실행할까요?`,
  refreshFailed:"변경분 분석 실패: ",
  newer:(v)=>`더 새 그래프 v${v}가 있습니다(주소의 v= 를 지우고 새로 고침)`,
  refreshing:"변경분 분석 중 · 이전 그림을 유지합니다.",
  viewStory:"핵심 보기 · 모든 단계로", viewAll:"모든 단계 · 핵심 보기로",
  viewHint:"핵심 보기는 요청·결정·결론만 상자로 그리고, 작업과 하위 에이전트는 상자 안 한 줄로 접습니다"
} : {
  names: {proposed:"proposed",adopted:"adopted",in_progress:"in progress",asked:"asked",applied:"applied",reported_complete:"reported done · unverified",observed_success:"observed success",observed_failure:"observed failure",withdrawn:"withdrawn",unknown:"unknown"},
  tokenMissing:"The access token is missing or expired. Press B in the terminal for a new link.",
  requestFailed:(status)=>"The request could not be handled ("+status+")",
  actor:"actor: ", agents:"agent: ", analysis:"analysed by: ", noPerson:"no person", copyReference:"Copy reference for an agent",
  copyHint:"Paste it into a Claude Code or Codex conversation and the agent reads this event with its evidence",
  copied:"copied", selectedCopy:"selected · copy it", relationEvidence:"relation evidence: ",
  sourceLines:(a,b)=>"source lines "+a+"–"+b, unknownTime:"time unknown",
  fullSource:(n)=>"whole source line ("+n+" characters)", citedPart:"cited part",
  sourceChanged:"The source has changed. Shown here is the excerpt preserved at analysis time.",
  sourceMissing:"The source is gone. Context beyond the preserved excerpt cannot be checked.",
  graphVersion:(v,at)=>"graph v"+v+" · analyzed as of "+(at||"none"),
  synthetic:"synthetic data / mock analysis · ", lastCheck:(at,status)=>"last check: "+(at||"none")+" · "+status,
  badSVG:"Unsupported SVG element.",
  confirmRefresh:(runner)=>`New conversation and code evidence of this project may be sent to the cloud model of ${runner}. Analyze the changes?`,
  refreshFailed:"Analysis failed: ",
  newer:(v)=>`a newer graph v${v} exists (remove v= from the address and reload)`,
  refreshing:"Analyzing changes · the previous picture stays.",
  viewStory:"Story view · show every step", viewAll:"Every step · show the story",
  viewHint:"The story view draws requests, decisions and findings as boxes and folds the work and sub-agents into a line in their box"
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
  area.append(el("span",e.status_label||L.names[e.status]||e.status,"badge"),el("span",e.basis,"badge"),el("h3",e.title),el("p",e.summary),el("p",L.actor+e.actor,"muted"));
  // Who did the work (the tool, and for the agent's own work its model and effort) and which analysis model wrote the event.
  const agents=(e.authoring||[]).map(a=>[a.agent?(a.agent.startsWith(a.tool+" ")?a.agent:a.tool+" › "+a.agent):a.tool,a.role==="unattended"?L.noPerson:"",e.actor==="user"?"":(a.model||"").replace(a.tool+"-",""),e.actor==="user"?"":a.effort].filter(Boolean).join(" · ")).filter((v,i,s)=>v&&s.indexOf(v)===i);
  if(agents.length)area.append(el("p",L.agents+agents.join(", "),"muted"));
  if(e.analysis)area.append(el("p",L.analysis+[e.analysis.runner,e.analysis.model,e.analysis.effort].filter(Boolean).join(" "),"muted"));
  area.append(el("small",e.id,"muted"));
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
  // In the story view a folded step has no box of its own: the box it folded into is lit.
  for(const node of document.querySelectorAll("[data-event-id]"))node.classList.toggle("selected",node.dataset.eventId===id||(node.dataset.folds||"").split(" ").includes(id));
  const fragment=new URLSearchParams();if(pinned&&version)fragment.set("v",version);fragment.set("event",id);fragment.set("view",view);history.replaceState(null,"","#"+fragment);
}
// Resolves true when this load drew its picture, false when a later load overtook it (a second click).
async function load() {
  const thisLoad=++loading;
  const payload=await(await api("/graph")).json(), graph=payload.graph;
  if(thisLoad!==loading)return false;
  version=String(graph.version||"");$("version").textContent=L.graphVersion(graph.version,graph.analyzed_at);
  $("status").textContent=(graph.analysis_mode==="synthetic_mock"?L.synthetic:"")+L.lastCheck(payload.last_check.at,payload.last_check.status||graph.analysis_status)+(payload.freshness?" · "+payload.freshness:"");
  if(pinned&&payload.latest_version>graph.version)$("status").textContent+=" · "+L.newer(payload.latest_version);
  $("refresh").disabled=!payload.can_refresh||payload.refreshing;
  $("refresh").title=payload.refresh_blocked||"";
  refreshWith=[payload.refresh_with.runner,payload.refresh_with.model].filter(Boolean).join(" · ");
  if(payload.refresh_result&&payload.refresh_result.status==="failed"&&payload.refresh_result.error)$("status").textContent+=" · "+L.refreshFailed+payload.refresh_result.error;
  viewLabel();
  const drawing=view, text=await(await api(drawing==="story"?"/graph.svg?view=story":"/graph.svg")).text();
  if(thisLoad!==loading)return false;
  // This endpoint contains host-generated escaped SVG, not LLM HTML. Enforce a second
  // local whitelist before importing any element into the document.
  const documentSVG=new DOMParser().parseFromString(text,"image/svg+xml");
  const root=documentSVG.documentElement, allowed=new Set(["svg","defs","marker","path","g","rect","text"]);
  if(root.localName!=="svg"||[...root.querySelectorAll("*")].some(n=>!allowed.has(n.localName)))throw new Error(L.badSVG);
  for(const node of [root,...root.querySelectorAll("*")])for(const attribute of [...node.attributes])if(/^on/i.test(attribute.name)||["href","xlink:href","style"].includes(attribute.name))node.removeAttribute(attribute.name);
  $("graph").replaceChildren(document.importNode(root,true));
  shown=drawing;  // the view of the picture now on the page
  for(const node of $("graph").querySelectorAll("[data-event-id]")){node.addEventListener("click",()=>select(node.dataset.eventId).catch(showError));node.addEventListener("keydown",e=>{if(e.key==="Enter")select(node.dataset.eventId).catch(showError);});}
  $("limits").replaceChildren();for(const message of [...new Set([...(graph.limitations||[]),...(graph.input_limitations||[]),...(payload.last_check.limitations||[])])])$("limits").append(el("p",message));
  for(const item of (graph.open_items||[]))$("limits").append(el("p","["+item.status+"] "+item.text));
  if(payload.last_check.error)$("limits").append(el("p",payload.last_check.error));
  if(selected&&graph.events.some(e=>e.id===selected))await select(selected);else if(graph.events.length)await select(graph.events[0].id);
  return true;
}
function showError(error){$("status").textContent=error.message;}
function viewLabel(){$("view").textContent=view==="story"?L.viewStory:L.viewAll;$("view").title=L.viewHint;}
viewLabel();
$("view").addEventListener("click",()=>{
  view=view==="story"?"all":"story";
  viewLabel();
  const thisClick=loading+1;  // the load this click starts
  // The choice is kept once the newest load drew it; if the newest load fails, the button goes back to the picture
  // on the page. An overtaken load changes neither.
  load().then(drew=>{if(drew){try{sessionStorage.setItem("contexttrail.view",view);}catch(_){}}})
    .catch(error=>{if(thisClick===loading&&shown){view=shown;viewLabel();}showError(error);});
});
$("refresh").addEventListener("click",async()=>{
  if(!confirm(L.confirmRefresh(refreshWith||"?")))return;
  $("refresh").disabled=true;
  try{await api("/refresh",{method:"POST",headers:{"Content-Type":"application/json","X-Projectflow-Action":"refresh"},body:JSON.stringify({confirm:true})});
    $("status").textContent=L.refreshing;
    const poll=async()=>{try{const result=await(await api("/graph")).json();if(result.refreshing){setTimeout(poll,1200);return;}version=null;await load();}catch(e){showError(e);$("refresh").disabled=false;}};setTimeout(poll,700);
  }catch(e){showError(e);$("refresh").disabled=false;}
});
if(token)load().catch(showError);
