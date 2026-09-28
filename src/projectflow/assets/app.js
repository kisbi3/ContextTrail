"use strict";
const params = new URLSearchParams(location.hash.slice(1));
const token = params.get("token") || sessionStorage.getItem("projectflow.token") || "";
if (token) sessionStorage.setItem("projectflow.token", token);
let version = params.get("v"), selected = params.get("event"), generation = 0;
const $ = (id) => document.getElementById(id);
const names = {proposed:"제안",adopted:"채택",in_progress:"진행 중",asked:"요청",applied:"변경 적용",reported_complete:"완료 보고 · 미검증",observed_success:"관측 성공",observed_failure:"관측 실패",withdrawn:"철회",unknown:"미확인"};
function el(tag, text, cls) { const node=document.createElement(tag); if(text!==undefined)node.textContent=text;if(cls)node.className=cls;return node; }
async function api(path, options={}) {
  const suffix=version ? (path.includes("?")?"&":"?")+"version="+encodeURIComponent(version):"";
  const response=await fetch(path+suffix,{...options,headers:{Authorization:"Bearer "+token,...(options.headers||{})},cache:"no-store"});
  if(!response.ok)throw new Error(response.status===401?"접근 토큰이 없거나 만료되었습니다. 터미널의 B로 새 주소를 확인하세요.":"요청을 처리하지 못했습니다 ("+response.status+")");
  return response;
}
async function select(id) {
  selected=id; const thisGeneration=++generation;
  const data=await(await api("/events/"+encodeURIComponent(id))).json();
  if(thisGeneration!==generation)return;
  const e=data.event, area=$("detail");area.replaceChildren();
  area.append(el("span",e.status_label||names[e.status]||e.status,"badge"),el("span",e.basis,"badge"),el("h3",e.title),el("p",e.summary),el("p","주체: "+e.actor,"muted"),el("small",e.id,"muted"));
  if(e.reference){const copy=el("button","에이전트용 참조 복사","copy"),ref=el("code",e.reference);
    copy.title="Claude Code·Codex 대화에 붙여넣으면 에이전트가 이 사건을 근거와 함께 읽습니다";
    copy.onclick=async()=>{try{await navigator.clipboard.writeText(e.reference);copy.textContent="복사함";}
      catch(_){const range=document.createRange();range.selectNodeContents(ref);getSelection().removeAllRanges();getSelection().addRange(range);copy.textContent="선택함 · 복사하세요";}};
    const row=el("p",undefined,"reference");row.append(copy,ref);area.append(row);}
  for(const relation of (data.relations||[])){const box=el("section",undefined,"evidence");
    box.append(el("h4",relation.from_title+" → "+relation.to_title),el("p",(relation.relation_label||relation.relation)+" · "+relation.basis),el("p",relation.rationale),el("small","관계 근거: "+relation.evidence_ids.join(", ")));area.append(box);}
  for(const item of data.evidence){const box=el("section",undefined,"evidence");box.append(el("h4",item.source.provider+" · "+item.source.role),el("small",item.id),el("small","원문 줄 "+item.start_line+"–"+item.end_line+" · "+(item.source.recorded_at||"작성 시각 미상")));
    if(item.excerpt){const full=el("details");full.append(el("summary","전체 원문 줄 ("+item.quote.length+"자)"),el("pre",item.quote));box.append(el("small","인용 부분"),el("pre",item.excerpt),full);}
    else box.append(el("pre",item.quote));
    const locator=item.source.locator; box.append(el("small",JSON.stringify(locator)));
    if(item.original_state!=="available_at_last_scan")box.append(el("small",item.original_state==="changed"?"원본이 바뀌었습니다. 여기에는 분석 당시 보존한 발췌를 표시합니다.":"현재 원본 부재. 보존한 발췌 밖의 문맥은 확인할 수 없습니다."));
    area.append(box);}
  for(const node of document.querySelectorAll("[data-event-id]"))node.classList.toggle("selected",node.dataset.eventId===id);
  const fragment=new URLSearchParams();if(version)fragment.set("v",version);fragment.set("event",id);history.replaceState(null,"","#"+fragment);
}
async function load() {
  const payload=await(await api("/graph")).json(), graph=payload.graph;
  version=String(graph.version||"");$("version").textContent="그래프 v"+graph.version+" · 분석 기준 "+(graph.analyzed_at||"없음");
  $("status").textContent=(graph.analysis_mode==="synthetic_mock"?"합성 데이터 / Mock 분석 · ":"")+"마지막 확인: "+(payload.last_check.at||"없음")+" · "+(payload.last_check.status||graph.analysis_status);
  $("refresh").disabled=!payload.can_refresh||payload.refreshing;
  const text=await(await api("/graph.svg")).text();
  // This endpoint contains host-generated escaped SVG, not LLM HTML. Enforce a second
  // local whitelist before importing any element into the document.
  const documentSVG=new DOMParser().parseFromString(text,"image/svg+xml");
  const root=documentSVG.documentElement, allowed=new Set(["svg","defs","marker","path","g","rect","text"]);
  if(root.localName!=="svg"||[...root.querySelectorAll("*")].some(n=>!allowed.has(n.localName)))throw new Error("지원하지 않는 SVG 요소입니다.");
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
  if(!confirm("이 프로젝트의 새 대화·코드 근거가 선택한 CLI의 클라우드 모델에 전송될 수 있습니다. 변경분 분석을 실행할까요?"))return;
  $("refresh").disabled=true;
  try{await api("/refresh",{method:"POST",headers:{"Content-Type":"application/json","X-Projectflow-Action":"refresh"},body:JSON.stringify({confirm:true})});
    $("status").textContent="변경분 분석 중 · 이전 그림을 유지합니다.";
    const poll=async()=>{try{const result=await(await api("/graph")).json();if(result.refreshing){setTimeout(poll,1200);return;}version=null;await load();}catch(e){showError(e);$("refresh").disabled=false;}};setTimeout(poll,700);
  }catch(e){showError(e);$("refresh").disabled=false;}
});
if(token)load().catch(showError);
