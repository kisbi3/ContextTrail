"use strict";
const params = new URLSearchParams(location.hash.slice(1));
const token = params.get("token") || sessionStorage.getItem("contexttrail.token") || "";
if (token) sessionStorage.setItem("contexttrail.token", token);
let version = params.get("v"), selected = params.get("event"), generation = 0, loading = 0, drawing = 0, shown = null, refreshWith = "";
// A link opened at a version stays at it; otherwise a reload shows the latest graph (an analysis may have run since).
const pinned = params.get("v") !== null;
// The story view (requests, decisions, findings; the work a line in its box) or every step; the story by default.
let view = params.get("view") || sessionStorage.getItem("contexttrail.view") || "story";
// What the last load brought: the events by id with their numbers, the requests and the request each event's turn
// belongs to; and the boxes now drawn (an event's own box, and the box a folded event lights).
let events = new Map(), requests = [], turnOf = {}, boxes = new Map(), folds = new Map(), haystack = [];
const $ = (id) => document.getElementById(id);
// Screen texts follow the page language the server chose (the html lang attribute).
const L = document.documentElement.lang === "ko" ? {
  tokenMissing:"접근 토큰이 없거나 만료되었습니다. 터미널의 B로 새 주소를 확인하세요.",
  requestFailed:(status)=>"요청을 처리하지 못했습니다 ("+status+")",
  agents:"에이전트: ", analysis:"분석: ", copyReference:"에이전트용 참조 복사",
  copyHint:"Claude Code·Codex 대화에 붙여넣으면 에이전트가 이 사건을 근거와 함께 읽습니다",
  copied:"복사함", selectedCopy:"선택함 · 복사하세요",
  sourceLines:(a,b)=>a===b?a+"번째 줄":a+"–"+b+"번째 줄", unknownTime:"작성 시각 미상",
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
  viewHint:"핵심 보기는 요청·결정·결론만 상자로 그리고, 작업과 하위 에이전트는 상자 안 한 줄로 접습니다",
  prev:"◀ 이전 요청", next:"다음 요청 ▶", latest:"최근 요청", navHint:"[ ] 키로도 요청 사이를 옮깁니다",
  turn:(k,n,at)=>`요청 ${k} / ${n}`+(at?" · "+at:""), beforeRequests:(n)=>`첫 요청 전 · 요청 ${n}개`,
  search:"제목·요약에서 찾기 ( / )", matches:(n)=>n+"건", noMatch:"찾은 사건 없음",
  moreMatches:(n)=>`그 밖에 ${n}건 · 낱말을 더 적어 좁혀 보세요`,
  switched:"모든 단계로 바꿨습니다: 이 단계는 핵심 보기에서 상자 안에 접혀 있습니다",
  whatHappened:"무슨 일이 있었나", evidence:(n)=>`원문 근거 ${n}개`, noEvidence:"보존된 근거 없음",
  checksOfChange:"검증한 결과", checksOfResult:"이 결과가 확인한 변경", links:"연결된 사건",
  structure:(n)=>`대화 순서·위임 연결 ${n}개`, relationEvidence:(n)=>`관계 근거 ${n}개`,
  openItems:"아직 확인되지 않은 일", received:"호출한 쪽이 받은 보고: ", inferred:"추정",
  openHead:(n)=>`열린 항목 ${n}개`, resolved:"해결됨", more:(n)=>`나머지 ${n}개 더 보기`,
  limitsHead:(n)=>`분석 한계 ${n}개`, moreGroups:(n,m)=>`그 밖의 묶음 ${n}개 · 항목 ${m}개`, notes:"분석 메모"
} : {
  tokenMissing:"The access token is missing or expired. Press B in the terminal for a new link.",
  requestFailed:(status)=>"The request could not be handled ("+status+")",
  agents:"agent: ", analysis:"analysed by: ", copyReference:"Copy reference for an agent",
  copyHint:"Paste it into a Claude Code or Codex conversation and the agent reads this event with its evidence",
  copied:"copied", selectedCopy:"selected · copy it",
  sourceLines:(a,b)=>a===b?"line "+a:"lines "+a+"–"+b, unknownTime:"time unknown",
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
  viewHint:"The story view draws requests, decisions and findings as boxes and folds the work and sub-agents into a line in their box",
  prev:"◀ previous request", next:"next request ▶", latest:"latest", navHint:"The [ and ] keys move between requests too",
  turn:(k,n,at)=>`request ${k} / ${n}`+(at?" · "+at:""), beforeRequests:(n)=>`before the first request · ${n} requests`,
  search:"Find in titles and summaries ( / )", matches:(n)=>n+(n===1?" match":" matches"), noMatch:"no event found",
  moreMatches:(n)=>`${n} more · add a word to narrow it`,
  switched:"Switched to every step: this step is folded into a box in the story view",
  whatHappened:"What happened", evidence:(n)=>`Source evidence: ${n}`, noEvidence:"no preserved evidence",
  checksOfChange:"Results that checked it", checksOfResult:"Changes this result checked", links:"Linked events",
  structure:(n)=>`Dialog order and delegation links: ${n}`, relationEvidence:(n)=>`Relation evidence: ${n}`,
  openItems:"Open items", received:"report as its caller received it: ", inferred:"inferred",
  openHead:(n)=>`Open items: ${n}`, resolved:"resolved", more:(n)=>`Show ${n} more`,
  limitsHead:(n)=>`Limitations: ${n}`, moreGroups:(n,m)=>`${n} more groups · ${m} items`, notes:"Analysis notes"
};
const SHOWN_MATCHES = 50, SHOWN_OPEN = 5, SHOWN_GROUPS = 5;
function el(tag, text, cls) { const node=document.createElement(tag); if(text!==undefined&&text!==null)node.textContent=text;if(cls)node.className=cls;return node; }
const pad = (n) => String(n).padStart(2, "0");
// A stored time (ISO, Z or an offset) in the browser's local time, as the terminal shows it: MM-DD HH:MM.
function when(iso) { if(!iso)return ""; const d=new Date(iso); if(isNaN(d))return ""; return `${pad(d.getMonth()+1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`; }
function number(n) { return "["+pad(n)+"]"; }
async function api(path, options={}) {
  const suffix=version ? (path.includes("?")?"&":"?")+"version="+encodeURIComponent(version):"";
  const response=await fetch(path+suffix,{...options,headers:{Authorization:"Bearer "+token,...(options.headers||{})},cache:"no-store"});
  if(!response.ok)throw new Error(response.status===401?L.tokenMissing:L.requestFailed(response.status));
  return response;
}
// A button that selects an event (a relation's other end, an open item, a search match).
function eventLink(id, text) { const link=el("button",text,"link");link.type="button";link.onclick=()=>jump(id).catch(showError);return link; }
function notice(text) { $("notice").textContent=text||"";$("notice").hidden=!text; }

function evidenceBox(item) {
  const box=el("section",undefined,"evidence"), source=item.source||{};
  box.append(el("h4",[item.role_label,item.provider_label,when(source.recorded_at)||L.unknownTime,L.sourceLines(item.start_line,item.end_line)].filter(Boolean).join(" · ")));
  if(item.excerpt){const full=el("details");full.append(el("summary",L.fullSource(item.quote.length)),el("pre",item.quote));box.append(el("small",L.citedPart),el("pre",item.excerpt),full);}
  else box.append(el("pre",item.quote));
  if(item.original_state!=="available_at_last_scan")box.append(el("small",item.original_state==="changed"?L.sourceChanged:L.sourceMissing));
  // Where the record is: its file and line (a database's records name the file only).
  const at=source.locator||{}, where=typeof at.path==="string"?at.path+(at.line?":"+at.line:""):"";
  box.append(el("small",[where,item.id].filter(Boolean).join(" · "),"muted"));
  return box;
}
function relationRow(relation, evidence) {
  const row=el("div",undefined,"relation");
  const how=[(relation.direction==="out"?"→ ":"← ")+relation.relation_label,relation.origin_label,relation.basis==="inferred"?L.inferred:""].filter(Boolean).join(" · ");
  const head=el("div",undefined,"relation-head");
  head.append(el("span",how,"how"),eventLink(relation.other_id,number(relation.other_number)+" "+relation.other_title),el("span",relation.other_status,"tone-"+relation.other_tone));
  row.append(head);
  if(relation.rationale)row.append(el("p",relation.rationale,"why"));
  const cited=(relation.evidence_ids||[]).map(id=>evidence.get(id)).filter(item=>item&&!item.own);
  if(cited.length){const more=el("details");more.append(el("summary",L.relationEvidence(cited.length)));for(const item of cited)more.append(evidenceBox(item));row.append(more);}
  return row;
}
// The detail: what happened and its own evidence first, then its relations (dialog-order and delegation links folded
// away), each naming the event at the other end as a link.
function renderDetail(data) {
  const e=data.event, area=$("detail");area.replaceChildren();
  area.append(el("span",e.status_label,"badge tone-"+e.status_tone),el("span",e.kind_label,"badge"));
  if(e.basis_label)area.append(el("span",e.basis_label,"badge"));
  area.append(el("h3",number(e.number)+" "+e.title));
  area.append(el("p",[e.actor_label,when(e.occurred_at||e.recorded_at)].filter(Boolean).join(" · "),"muted meta"));
  if(e.agents)area.append(el("p",L.agents+e.agents,"muted meta"));
  if(e.analysis_label)area.append(el("p",L.analysis+e.analysis_label,"muted meta"));
  for(const report of (e.received_reports||[]))area.append(el("p",L.received+report.title,"muted meta"));
  if(e.summary){area.append(el("h4",L.whatHappened,"section"));for(const part of e.summary.split("\n").filter(p=>p.trim()))area.append(el("p",part));}
  const evidence=new Map(data.evidence.map(item=>[item.id,item])), own=data.evidence.filter(item=>item.own);
  if(e.evidence_ids.length)area.append(el("h4",L.evidence(e.evidence_ids.length),"section"));
  for(const item of own)area.append(evidenceBox(item));
  if(own.length<e.evidence_ids.length)area.append(el("p",L.noEvidence+" · "+e.evidence_ids.filter(id=>!evidence.has(id)).join(", "),"muted"));
  const group=(name)=>data.relations.filter(r=>r.group===name);
  const checks=group("checks"), links=group("links"), structure=group("structure");
  if(checks.length){area.append(el("h4",["action","revision"].includes(e.kind)?L.checksOfChange:L.checksOfResult,"section"));for(const r of checks)area.append(relationRow(r,evidence));}
  if(links.length){area.append(el("h4",L.links,"section"));for(const r of links)area.append(relationRow(r,evidence));}
  if(structure.length){const folded=el("details",undefined,"structure");folded.append(el("summary",L.structure(structure.length)));for(const r of structure)folded.append(relationRow(r,evidence));area.append(folded);}
  if(data.open_items.length){area.append(el("h4",L.openItems,"section"));const list=el("ul");for(const item of data.open_items)list.append(el("li",item.text));area.append(list);}
  if(e.reference){const copy=el("button",L.copyReference,"copy"),ref=el("code",e.reference);
    copy.type="button";copy.title=L.copyHint;
    copy.onclick=async()=>{try{await navigator.clipboard.writeText(e.reference);copy.textContent=L.copied;}
      catch(_){const range=document.createRange();range.selectNodeContents(ref);getSelection().removeAllRanges();getSelection().addRange(range);copy.textContent=L.selectedCopy;}};
    const row=el("p",undefined,"reference");row.append(copy,ref);area.append(row);}
  area.append(el("small",e.id,"muted"));
}
// The box an event is drawn as, or in the story view the box it folded into; null when it is in neither.
function boxOf(id) { return boxes.get(id)||folds.get(id)||null; }
function light(id, scroll) {
  for(const node of $("graph").querySelectorAll(".selected"))node.classList.remove("selected");
  const lit=boxOf(id);
  if(lit){lit.classList.add("selected");if(scroll)reveal(lit);}
}
// Scrolls the graph's own box to centre a drawn box (the page itself moves only when the graph is out of sight, as
// on a phone where the detail is under it).
function reveal(node) {
  const frame=$("graph"), box=node.getBoundingClientRect(), view=frame.getBoundingClientRect();
  frame.scrollTop+=box.top-view.top-Math.max(0,(view.height-box.height)/2);
  if(box.left<view.left||box.right>view.right)frame.scrollLeft+=box.left-view.left-Math.max(0,(view.width-box.width)/2);
  if(view.bottom<0||view.top>innerHeight)frame.scrollIntoView({block:"start"});
}
async function select(id, {scroll=true}={}) {
  selected=id; const thisGeneration=++generation;
  light(id, scroll);
  navLabel();
  const fragment=new URLSearchParams();if(pinned&&version)fragment.set("v",version);fragment.set("event",id);fragment.set("view",view);history.replaceState(null,"","#"+fragment);
  const data=await(await api("/events/"+encodeURIComponent(id))).json();
  if(thisGeneration!==generation)return;
  renderDetail(data);
}
// Going to an event from a link, a search or the list: a step the story view folded into a box shows every step,
// as the terminal does. If that drawing fails, the button goes back to the picture still on the page (as the view
// button's does) and the caller reports the error.
async function jump(id) {
  if(view==="story"&&!boxes.has(id)&&events.has(id)){
    const before=view, thisDraw=drawing+1;  // the drawing this jump starts
    view="all";viewLabel();
    let drew;
    try{drew=await draw();}
    catch(error){if(thisDraw===drawing){view=shown||before;viewLabel();}throw error;}
    if(drew){saveView();notice(L.switched);}
  }
  await select(id);
}
function saveView(){try{sessionStorage.setItem("contexttrail.view",view);}catch(_){}}

// The request the selection belongs to: its turn's, else the last request before it.
function currentTurn() {
  if(selected in turnOf)return turnOf[selected];
  const n=(events.get(selected)||{}).number||0;let k=-1;
  requests.forEach((r,i)=>{if(r.number<=n)k=i;});return k;
}
function navLabel() {
  $("nav").hidden=!requests.length;if(!requests.length)return;
  const k=currentTurn();
  $("turn").textContent=k<0?L.beforeRequests(requests.length):L.turn(k+1,requests.length,when(requests[k].at));
  $("prev").disabled=k<=0;$("next").disabled=k>=requests.length-1;$("latest").disabled=selected===requests[requests.length-1].id;
}
function goTurn(k) { if(!requests.length)return; k=Math.max(0,Math.min(requests.length-1,k)); notice("");select(requests[k].id).catch(showError); }

// Search: every word in the title or the summary, case-insensitive; the matches as a list to click.
function search() {
  const words=$("search").value.toLowerCase().split(/\s+/).filter(Boolean), list=$("matches");
  list.replaceChildren();
  if(!words.length){list.hidden=true;$("count").textContent="";return;}
  const found=haystack.filter(([_,text])=>words.every(w=>text.includes(w))).map(([id])=>events.get(id));
  $("count").textContent=found.length?L.matches(found.length):L.noMatch;
  for(const e of found.slice(0,SHOWN_MATCHES)){const item=el("li"),link=eventLink(e.id,number(e.number)+" "+e.title);
    link.append(el("span",when(e.at),"when"));item.append(link);list.append(item);}
  if(found.length>SHOWN_MATCHES)list.append(el("li",L.moreMatches(found.length-SHOWN_MATCHES),"muted"));
  list.hidden=false;
}

// Open items first (each a link to the event it concerns), then the limitations grouped by their head, the largest
// groups shown and the rest folded away.
function renderLimits(payload, graph) {
  const area=$("limits");area.replaceChildren();
  const items=[...(graph.open_items||[])].sort((a,b)=>(a.status==="open"?0:1)-(b.status==="open"?0:1));
  if(items.length){
    area.append(el("h3",L.openHead(items.filter(i=>i.status==="open").length)));
    const list=el("ul",undefined,"open-items"), rest=el("ul",undefined,"open-items");
    items.forEach((item,n)=>{const li=el("li"),target=(item.related_event_ids||[]).find(id=>events.has(id));
      if(item.status!=="open")li.append(el("span",L.resolved,"badge"));
      li.append(target?eventLink(target,item.text):el("span",item.text));(n<SHOWN_OPEN?list:rest).append(li);});
    area.append(list);
    if(items.length>SHOWN_OPEN){const more=el("details");more.append(el("summary",L.more(items.length-SHOWN_OPEN)),rest);area.append(more);}
  }
  const groups=payload.limitation_groups||[];
  if(groups.length){
    area.append(el("h3",L.limitsHead(groups.reduce((sum,g)=>sum+g.count,0))));
    const shownGroups=el("div"),rest=el("div");
    groups.forEach((group,n)=>{const box=el("details",undefined,"group");
      box.append(el("summary",(group.head||L.notes)+" · "+group.count));
      for(const message of group.items)box.append(el("p",message));
      (n<SHOWN_GROUPS?shownGroups:rest).append(box);});
    area.append(shownGroups);
    if(groups.length>SHOWN_GROUPS){const more=el("details");
      more.append(el("summary",L.moreGroups(groups.length-SHOWN_GROUPS,groups.slice(SHOWN_GROUPS).reduce((sum,g)=>sum+g.count,0))),rest);area.append(more);}
  }
  if(payload.last_check.error)area.append(el("p",payload.last_check.error));
}

// Draws the current view; resolves true when this drawing is on the page, false when a later one overtook it.
async function draw() {
  const thisDraw=++drawing, drawn=view;
  const text=await(await api(drawn==="story"?"/graph.svg?view=story":"/graph.svg?view=all")).text();
  if(thisDraw!==drawing)return false;
  // This endpoint contains host-generated escaped SVG, not LLM HTML. Enforce a second
  // local whitelist before importing any element into the document.
  const documentSVG=new DOMParser().parseFromString(text,"image/svg+xml");
  const root=documentSVG.documentElement, allowed=new Set(["svg","defs","marker","path","g","rect","text"]);
  if(root.localName!=="svg"||[...root.querySelectorAll("*")].some(n=>!allowed.has(n.localName)))throw new Error(L.badSVG);
  for(const node of [root,...root.querySelectorAll("*")])for(const attribute of [...node.attributes])if(/^on/i.test(attribute.name)||["href","xlink:href","style"].includes(attribute.name))node.removeAttribute(attribute.name);
  $("graph").replaceChildren(document.importNode(root,true));
  shown=drawn;  // the view of the picture now on the page
  boxes=new Map();folds=new Map();
  for(const node of $("graph").querySelectorAll("[data-event-id]")){
    boxes.set(node.dataset.eventId,node);
    for(const folded of (node.dataset.folds||"").split(" ").filter(Boolean))if(!folds.has(folded))folds.set(folded,node);
    node.addEventListener("click",()=>{notice("");select(node.dataset.eventId,{scroll:false}).catch(showError);});
    node.addEventListener("keydown",e=>{if(e.key==="Enter")select(node.dataset.eventId,{scroll:false}).catch(showError);});
  }
  if(selected)light(selected,true);
  return true;
}
// Resolves true when this load drew its picture, false when a later load overtook it (a second click).
async function load() {
  const thisLoad=++loading;
  const payload=await(await api("/graph")).json(), graph=payload.graph;
  if(thisLoad!==loading)return false;
  version=String(graph.version||"");$("version").textContent=L.graphVersion(graph.version,when(graph.analyzed_at)||graph.analyzed_at);
  $("status").textContent=(graph.analysis_mode==="synthetic_mock"?L.synthetic:"")+L.lastCheck(when(payload.last_check.at)||payload.last_check.at,payload.run_state)+(payload.freshness?" · "+payload.freshness:"");
  if(pinned&&payload.latest_version>graph.version)$("status").textContent+=" · "+L.newer(payload.latest_version);
  $("refresh").disabled=!payload.can_refresh||payload.refreshing;
  $("refresh").title=payload.refresh_blocked||"";
  refreshWith=[payload.refresh_with.runner,payload.refresh_with.model].filter(Boolean).join(" · ");
  if(payload.refresh_result&&payload.refresh_result.status==="failed"&&payload.refresh_result.error)$("status").textContent+=" · "+L.refreshFailed+payload.refresh_result.error;
  events=new Map(graph.events.map((e,n)=>[e.id,{id:e.id,number:n+1,title:e.title,at:e.occurred_at||e.recorded_at}]));
  haystack=graph.events.map(e=>[e.id,(e.title+" "+(e.summary||"")).toLowerCase()]);
  requests=payload.requests||[];turnOf=payload.turn_of||{};
  renderLimits(payload,graph);
  viewLabel();
  if(!await draw()||thisLoad!==loading)return false;
  search();
  // Opened without a selection, the page starts at the latest request (the graph is in time order).
  const start=requests.length?requests[requests.length-1].id:(graph.events.length?graph.events[graph.events.length-1].id:null);
  if(selected&&events.has(selected))await select(selected);else if(start)await select(start);
  return true;
}
function showError(error){$("status").textContent=error.message;}
function viewLabel(){$("view").textContent=view==="story"?L.viewStory:L.viewAll;$("view").title=L.viewHint;}
viewLabel();
$("prev").textContent=L.prev;$("next").textContent=L.next;$("latest").textContent=L.latest;
for(const id of ["prev","next","latest"])$(id).title=L.navHint;
$("search").placeholder=L.search;$("search").setAttribute("aria-label",L.search);
$("prev").addEventListener("click",()=>goTurn(currentTurn()-1));
$("next").addEventListener("click",()=>goTurn(currentTurn()+1));
$("latest").addEventListener("click",()=>goTurn(requests.length-1));
let typing=null;
$("search").addEventListener("input",()=>{clearTimeout(typing);typing=setTimeout(search,120);});
$("search").addEventListener("keydown",e=>{
  if(e.key==="Enter"){const first=$("matches").querySelector("button");if(first)first.click();}
  if(e.key==="Escape"){$("search").value="";search();}
});
document.addEventListener("keydown",e=>{
  if(e.target instanceof HTMLInputElement||e.metaKey||e.ctrlKey||e.altKey)return;
  if(e.key==="["){e.preventDefault();goTurn(currentTurn()-1);}
  else if(e.key==="]"){e.preventDefault();goTurn(currentTurn()+1);}
  else if(e.key==="/"){e.preventDefault();$("search").focus();}
});
$("view").addEventListener("click",()=>{
  view=view==="story"?"all":"story";
  viewLabel();notice("");
  const thisClick=drawing+1;  // the drawing this click starts
  // The choice is kept once the newest drawing is on the page; if the newest drawing fails, the button goes back to
  // the picture on the page. An overtaken drawing changes neither.
  draw().then(drew=>{if(drew)saveView();})
    .catch(error=>{if(thisClick===drawing&&shown){view=shown;viewLabel();}showError(error);});
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
