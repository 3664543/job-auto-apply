#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""wb_deliver.py — BOSS 投递主链路（job_detail 独立页方案）。

实测要点：
  1. 搜索结果页右侧详情面板会卡在 .job-detail-loading（间歇性），
     「立即沟通」按钮永远不出现 → 改用独立详情页
     https://www.zhipin.com/job_detail/{jobId}.html
     该页同步渲染完整 JD + 「立即沟通」按钮，不依赖侧栏。
  2. jobId 每次搜索都会变，必须每次重新提取。
  3. 「立即沟通」是点击即发送默认招呼语；随后进消息页再发定制话术。

用法:
    python wb_deliver.py <搜索词> <岗位关键词> <公司关键词> [stage|send]
      stage = 填好话术、停在发送前（默认）
      send  = 直接发送
"""
import json
import os
import random
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.webbridge_client import extract_jobs, evaluate
from scripts.deliver_engine import pick_greeting_for_job
from scripts.wb import navigate, search_url, call

WORK = os.environ.get("BOSS_WORK_DIR", r"E:\Python\Ai_boss_dome\output\boss_work")
CHAT_URL = "https://www.zhipin.com/web/geek/chat"


def body_text():
    return (evaluate(r"""(document.body.innerText||'').replace(/\s+/g,' ')""") or "")


def detail_company():
    """从 job_detail 页提取公司名（用于新会话匹配）。"""
    code = r"""(function(){
        var sels=['.company-name','.company-info .name','.job-detail-company .name',
                  '.company-title .name','.company-card .name','[class*="company"] [class*="name"]',
                  '.company-info','[class*="companyName"]','[class*="company-name"]'];
        for(var i=0;i<sels.length;i++){
            var e=document.querySelector(sels[i]);
            if(!e) continue;
            var t=(e.textContent||'').replace(/\s+/g,' ').trim();
            if(t && t.length<40) return t;
        }
        return '';
    })()"""
    return evaluate(code) or ""


def pick_newest_conversation():
    """点开会话列表最顶部（最新）的那条。

    刚点完「立即沟通」时，新建的会话就在列表最顶，
    且标题里是招聘方姓名 + 公司简称，按公司名匹配反而找不到。
    """
    code = r"""(function(){
        var list=document.querySelectorAll('.friend-content');
        var best=null;
        for(var i=0;i<list.length;i++){
            var e=list[i];
            var r=e.getBoundingClientRect();
            if(r.width<=0||r.height<=0) continue;
            if(!best || r.top < best.top) best={e:e, top:r.top, t:(e.textContent||'').replace(/\s+/g,'')};
        }
        if(!best) return 'no_conv';
        best.e.scrollIntoView({block:'center'});
        best.e.click();
        return 'newest_clicked:'+best.t.slice(0,30);
    })()"""
    r = evaluate(code)
    time.sleep(1.5)
    sel = evaluate(r"""(function(){
        var s=document.querySelector('.friend-content.selected');
        if(!s) return 'none';
        var j=document.querySelector('.position-name');
        return ((s.textContent||'').replace(/\s+/g,' ').trim().slice(0,40))+' @ '+(j?(j.textContent||'').trim():'?');
    })()""")
    return f"{r} | now={sel}"


def wait_text(needles, timeout=25):
    t0 = time.time()
    while time.time() - t0 < timeout:
        t = body_text()
        if any(n in t for n in needles):
            return True
        time.sleep(1.2)
    return False


def click_text(texts, exact=True):
    """按可见文本点击最内层元素。"""
    code = r"""(function(){
        var WANT = __WANT__, EXACT = __EXACT__;
        var all = document.querySelectorAll('a,button,div,span,i,em');
        var hits = [];
        for (var i=0;i<all.length;i++){
            var e = all[i];
            var t = (e.textContent||'').replace(/\s+/g,'').trim();
            if(!t || t.length>16) continue;
            var ok = EXACT ? (WANT.indexOf(t)>=0) : (t.indexOf(WANT)>=0);
            if(!ok) continue;
            var r = e.getBoundingClientRect();
            if(r.width<=0||r.height<=0) continue;
            if(e.children.length>3) continue;
            var st = window.getComputedStyle(e);
            if(st.display==='none'||st.visibility==='hidden') continue;
            hits.push(e);
        }
        if(!hits.length) return 'not_found';
        hits.sort(function(a,b){
            var la=(a.textContent||'').length, lb=(b.textContent||'').length;
            if(la!==lb) return la-lb;
            var ra=a.getBoundingClientRect(), rb=b.getBoundingClientRect();
            return (ra.width*ra.height)-(rb.width*rb.height);
        });
        var e = hits[0];
        e.scrollIntoView({block:'center'});
        e.click();
        return 'clicked:'+e.tagName+':'+(e.className||'').toString().slice(0,44)+':'+(e.textContent||'').replace(/\s+/g,'').trim().slice(0,24);
    })()""".replace("__WANT__", json.dumps(texts, ensure_ascii=False)).replace("__EXACT__", "true")
    return evaluate(code)


def scan_pages(query, max_pages=6, city="广州"):
    """翻页扫描搜索结果，累积所有岗位（BOSS 排序会变，目标可能在第 2/3 页）。"""
    seen = {}
    order = []
    for p in range(1, max_pages + 1):
        url = search_url(query, "101280600") + f"&page={p}"
        navigate(url)
        time.sleep(3.5)
        # 触发懒加载
        evaluate(r"""(function(){window.scrollTo(0,document.body.scrollHeight);return 1;})()""")
        time.sleep(1.5)
        jobs = extract_jobs()
        new = 0
        for j in jobs:
            jid = j.get("jobId")
            if jid and jid not in seen:
                seen[jid] = j
                order.append(j)
                new += 1
        print(f"   [第{p}页] 提取 {len(jobs)} 条，新增 {new} 条，累计 {len(order)}")
        if not jobs:
            break
        if new == 0 and p > 1:
            break
    return order


_MIN_SALARY_K = 12  # 薪资下限（K），低于不投
_TITLE_EXCLUDE = ("实习", "应届", "校招", "兼职", "测开", "测试开发")  # 标题含这些词不投（含测开/测试开发岗）
# 外包/人力/猎头公司关键词（命中不投）
_OUTSOURCE = (
    "软通", "中软国际", "中软", "法本", "万宝盛华", "人瑞", "德科", "外企德科",
    "FESCO", "外企服务", "科锐", "伯乐", "博彦", "文思海辉", "东软", "外包",
    "人力资源", "猎头", "劳务", "派遣", "锐仕方达", "猎聘",
)
_SALARY_RE = re.compile(r"(\d+)\s*[-–~]\s*(\d+)\s*K")


_PROFILE_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "user_profile.json")
# 代码内兜底默认值；实际以 user_profile.json 的配置为准（t_exclude / c_exclude / min_salary_k）
_DEFAULT_TITLE_EXCLUDE = ("实习", "应届", "校招", "兼职", "测开", "测试开发")
_DEFAULT_OUTSOURCE = _OUTSOURCE


def _filters() -> dict:
    """从 user_profile.json 读取筛选配置；读取失败用兜底默认值。"""
    try:
        p = json.load(open(_PROFILE_PATH, encoding="utf-8"))
        return {
            "min_salary_k": int(p.get("min_salary_k", 12)),
            "max_salary_k": int(p["max_salary_k"]) if p.get("max_salary_k") else None,
            "title_exclude": tuple(p.get("t_exclude") or _DEFAULT_TITLE_EXCLUDE),
            "outsource": tuple(p.get("c_exclude") or _DEFAULT_OUTSOURCE),
        }
    except Exception:
        return {"min_salary_k": _MIN_SALARY_K, "max_salary_k": None, "title_exclude": _DEFAULT_TITLE_EXCLUDE, "outsource": _DEFAULT_OUTSOURCE}


def job_reject_reason(j) -> str:
    """返回不投的理由；空字符串 = 通过全部筛选。规则来自 user_profile.json。"""
    f = _filters()
    title = str(j.get("title", ""))
    hit = next((w for w in f["title_exclude"] if w and w in title), None)
    if hit:
        return f"标题含「{hit}」不投：{title}"
    m = _SALARY_RE.search(str(j.get("salary", "")))
    if m:
        low, high = int(m.group(1)), int(m.group(2))
        if low < f["min_salary_k"]:
            return f"薪资低于{f['min_salary_k']}K：{j.get('salary')}"
        if f["max_salary_k"] and low > f["max_salary_k"]:
            return f"起始薪资超{f['max_salary_k']}K（超出期望范围）：{j.get('salary')}"
    comp = str(j.get("company", ""))
    if any(w and w in comp for w in f["outsource"]):
        return f"外包/人力公司：{comp}"
    return ""


def find_job(query, role_kw, company_kw, city="深圳"):
    # 公司关键词传 "." 或 "-" 表示不做公司过滤
    company_kw = "" if company_kw in (".", "-", "") else company_kw
    jobs = scan_pages(query, max_pages=6, city=city)
    for j in jobs:
        if city and j.get("city") != city:
            continue
        if role_kw and role_kw.lower() not in j["title"].lower():
            continue
        if company_kw and company_kw not in j["company"]:
            continue
        reason = job_reject_reason(j)
        if reason:
            print(f"   [跳过] {reason}")
            continue
        return j, jobs
    return None, jobs


def open_detail(job):
    url = f"https://www.zhipin.com/job_detail/{job['jobId']}.html"
    navigate(url)
    time.sleep(4)
    ok = wait_text(["立即沟通", "继续沟通", "已沟通"], timeout=20)
    print(f"[详情页] {ok}  {url}")
    return ok


def probe_chat_inputs():
    """列出当前页所有可见输入框，标出是否是聊天输入框。"""
    code = r"""JSON.stringify((function(){
        function vis(e){var r=e.getBoundingClientRect();return r.width>0&&r.height>0;}
        var out=[];
        document.querySelectorAll('textarea,input,[contenteditable="true"]').forEach(function(e){
            if(!vis(e)) return;
            var cls=(e.className||'').toString();
            var r=e.getBoundingClientRect();
            out.push({tag:e.tagName, cls:cls.slice(0,50), ph:e.getAttribute('placeholder')||'',
                      type:e.getAttribute('type')||'', x:Math.round(r.x), y:Math.round(r.y),
                      w:Math.round(r.width), h:Math.round(r.height),
                      ce:!!e.isContentEditable});
        });
        return out;
    })())"""
    raw = evaluate(code)
    try:
        return json.loads(raw) if raw else []
    except Exception:
        return [{"_raw": str(raw)[:600]}]


def wait_chat_ready(timeout=60):
    """等消息页加载完：出现 #chat-input 聊天输入框。"""
    t0 = time.time()
    while time.time() - t0 < timeout:
        n = evaluate(r"""document.querySelectorAll('#chat-input,div.chat-input').length""")
        try:
            if int(n or 0) >= 1:
                time.sleep(1.0)
                return True
        except (TypeError, ValueError):
            pass
        time.sleep(1.5)
    return False


def fill_chat(text):
    """在聊天输入框（#chat-input）填入话术。

    必须用 WebBridge 原生 fill 动作：手工 textContent 赋值 Vue 收不到，
    发送按钮会一直是 disabled。原生 fill 走 ProseMirror/富文本编辑器的
    正确事件序列，能让「发送」按钮激活。
    """
    # 先清空（fill 是 clear-and-insert，但残留草稿要先抹掉）
    evaluate(r"""(function(){
        var e=document.querySelector('#chat-input');
        if(!e) return 'no_input';
        e.focus();
        document.execCommand('selectAll',false,null);
        document.execCommand('delete',false,null);
        e.textContent='';
        e.dispatchEvent(new Event('input',{bubbles:true}));
        return 'cleared';
    })()""")
    time.sleep(0.4)
    r = call("fill", {"selector": "#chat-input", "value": text})
    ok = r.get("ok") and (r.get("data") or {}).get("success")
    time.sleep(1.0)
    n = evaluate(r"""(function(){var e=document.querySelector('#chat-input');return e?((e.textContent||'').length):-1;})()""")
    return f"fill_ok={ok} len={n}"


def enter_chat():
    """从弹窗或直接跳转到消息页，确保进入聊天界面。"""
    if "继续沟通" in body_text():
        r = click_text(["继续沟通"])
        print(f"[弹窗·继续沟通] {r}")
        time.sleep(3)
    url = evaluate("location.href") or ""
    if "chat" not in url:
        navigate(CHAT_URL)
        print("[跳转消息页] ok")
    ok = wait_chat_ready(timeout=45)
    print(f"[消息页就绪] {ok}")
    time.sleep(2)
    return ok


def pick_conversation(company_kw):
    """选中目标公司会话：点最靠前（最新）的一条匹配项。

    总是点最靠前的那条 —— 立即沟通刚建立的会话就在列表顶部。
    """
    code = r"""(function(){
        var KW = __KW__;
        var list = document.querySelectorAll('.friend-content');
        var best=null;
        for(var i=0;i<list.length;i++){
            var e=list[i];
            var t=(e.textContent||'').replace(/\s+/g,'');
            if(t.indexOf(KW)<0) continue;
            var r=e.getBoundingClientRect();
            if(r.width<=0||r.height<=0) continue;
            if(!best || r.top < best.top) best={e:e, top:r.top, t:t};
        }
        if(!best) return 'conv_not_found';
        best.e.scrollIntoView({block:'center'});
        best.e.click();
        return 'conv_clicked:'+best.t.slice(0,30);
    })()""".replace("__KW__", json.dumps(company_kw, ensure_ascii=False))
    r = evaluate(code)
    time.sleep(1.5)
    sel = evaluate(r"""(function(){
        var s=document.querySelector('.friend-content.selected');
        if(!s) return 'none';
        var t=(s.textContent||'').replace(/\s+/g,' ').trim();
        var j=document.querySelector('.position-name');
        return t.slice(0,40)+' @ '+(j?(j.textContent||'').trim():'?');
    })()""")
    # 安全阀：点击后确认选中的会话确实属于目标公司，防止默认选中旧会话发错人
    if "conv_clicked" in r and company_kw.replace(" ", "") not in sel.replace(" ", ""):
        return f"{r} | now={sel} | MISMATCH"
    return f"{r} | now={sel}"


def click_send():
    """点发送按钮（button.btn-send），并返回发送后的验证状态。"""
    before = evaluate(r"""(function(){
        var b=document.querySelector('button.btn-send');
        if(!b) return 'no_btn';
        return (b.className||'');
    })()""")
    if "no_btn" in str(before):
        return "no_send_btn"
    if "disabled" in str(before):
        return "send_btn_still_disabled:" + str(before)[:60]
    evaluate(r"""(function(){
        var b=document.querySelector('button.btn-send');
        if(!b) return 'no_btn';
        b.click();
        return 'clicked';
    })()""")
    time.sleep(2.0)
    after = evaluate(r"""(function(){
        var e=document.querySelector('#chat-input');
        var b=document.querySelector('button.btn-send');
        return 'input_len='+(e?((e.textContent||'').trim().length):-1)+' btn='+(b?(b.className||''):'gone');
    })()""")
    return f"sent | {before} -> {after}"


def deliver_one(job, text, mode="send"):
    """投递单个岗位。返回状态：sent / chatted(已沟通过，跳过) / no_conv / fail / stage。

    安全阀：
      1. 详情页只有「继续沟通/已沟通」→ 已沟通过，不重复发话术；
      2. 消息页会话必须与岗位公司名（或岗位名兜底）匹配，否则跳过；
      3. 发送后截图留证。
    """
    btn = open_detail(job)
    if not btn:
        print(f"!! 详情页未就绪：{job['company']} | {job['title']}")
        return "fail"
    if btn in ("继续沟通", "已沟通"):
        print(f"[跳过·已沟通] {job['title']} | {job['company']} | {job['salary']}")
        return "chatted"
    r = click_text(["立即沟通"])
    print(f"[点立即沟通] {r}")
    time.sleep(3.5)

    enter_chat()
    conv = pick_conversation(job["company"])
    print(f"[选会话] {conv}")
    ok = "conv_clicked" in conv and "MISMATCH" not in conv
    if not ok:
        # 兜底：代招职位会话显示猎头公司名，按招聘公司名找不到。
        # 此时若默认选中的会话岗位名与目标岗位一致（前 6 字命中），视为正确会话放行
        now_part = conv.split("@")[-1] if "@" in conv else ""
        title_head = str(job.get("title", ""))[:6]
        if "conv_not_found" in conv and title_head and title_head in now_part:
            print("[选会话·兜底] 默认会话岗位名匹配，放行")
            ok = True
    if not ok:
        print("!! 会话未找到或不匹配，跳过（防止发错人）")
        call("screenshot", {"path": os.path.join(WORK, "deliver_no_conv.png"), "format": "png"})
        return "no_conv"
    time.sleep(2)

    fill = fill_chat(text)
    print(f"[填充] {fill}")
    time.sleep(1.5)
    if mode != "send":
        print(">>> stage 模式：停在发送前")
        return "stage"
    s = click_send()
    print(f"[发送] {s}")
    time.sleep(2.5)
    call("screenshot", {"path": os.path.join(WORK, "deliver_sent.png"), "format": "png"})
    return "sent" if "sent" in s else "fail"


def deliver_batch(query, role_kw, company_kw, mode, target):
    """批量投递：按筛选条件遍历岗位，跳过已沟通的，投满 target 个即停。"""
    company_kw = "" if company_kw in (".", "-") else company_kw
    jobs = scan_pages(query, max_pages=6, city="深圳")
    cands = []
    for j in jobs:
        if j.get("city") != "深圳":
            continue
        if role_kw and role_kw.lower() not in j["title"].lower():
            continue
        if company_kw and company_kw not in j["company"]:
            continue
        reason = job_reject_reason(j)
        if reason:
            print(f"   [跳过] {reason}")
            continue
        cands.append(j)
    print(f"\n[候选] 符合筛选 {len(cands)} 个，本次目标 {target} 个")
    sent = 0
    for i, job in enumerate(cands):
        if sent >= target:
            break
        print(f"\n===== 候选 {i + 1}/{len(cands)}：{job['title']} | {job['salary']} | {job['company']} =====")
        text, meta = pick_greeting_for_job(job)
        print(f"[话术] {meta.get('source')} / {meta.get('folder') or '(通用)'} / {len(text)} 字")
        st = deliver_one(job, text, mode)
        if st == "sent":
            sent += 1
            print(f"[进度] 本次已投 {sent}/{target}")
        elif st == "stage":
            print(">>> stage 模式停在发送前，批量中止（stage 仅支持单岗位演练）")
            break
        if i + 1 < len(cands) and sent < target:
            gap = random.uniform(8, 15)
            print(f"[间隔] 等待 {gap:.0f} 秒（风控节奏）")
            time.sleep(gap)
    print(f"\n[完成] 扫描 {len(jobs)} 个岗位，符合筛选 {len(cands)} 个，本次投出 {sent} 个")
    return 0


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "stage"
    if mode in ("chat", "chat-send"):
        # 补发模式：跳过搜索/打招呼，直接进消息页给指定公司发话术
        company_kw = sys.argv[2]
        text = sys.argv[3] if len(sys.argv) > 3 else None
        if not text:
            return chat_only(company_kw, mode)
        return chat_only(company_kw, mode, text)

    if mode == "batch":
        # 用法: python -m scripts.wb_deliver batch <数量> <搜索词> <岗位词> <公司词> [send|stage]
        target = int(sys.argv[2])
        query, role_kw, company_kw = sys.argv[3], sys.argv[4], sys.argv[5]
        send_mode = sys.argv[6] if len(sys.argv) > 6 else "send"
        return deliver_batch(query, role_kw, company_kw, send_mode, target)

    query, role_kw, company_kw = sys.argv[2], sys.argv[3], sys.argv[4]
    mode = sys.argv[5] if len(sys.argv) > 5 else "stage"

    job, jobs = find_job(query, role_kw, company_kw)
    if not job:
        print("!! 未找到岗位")
        for j in jobs:
            print(f"   {j['title'][:30]:<32} {j['salary'][:12]:<14} {j['company'][:16]} | {j['city']}")
        return 2

    print(f"[命中] {job['title']} | {job['salary']} | {job['company']} | {job['city']} | {job['degree']}")
    print(f"[jobId] {job['jobId']}")

    text, meta = pick_greeting_for_job(job)
    print(f"[话术] {meta.get('source')} / {meta.get('folder') or '(通用)'} / {len(text)} 字")
    print(f"       {text[:120]}...")

    if not open_detail(job):
        print("!! 详情页未就绪")
        call("screenshot", {"path": os.path.join(WORK, "deliver_fail.png"), "format": "png"})
        return 3

    # 点「立即沟通」——这一步会向 BOSS 发出默认招呼语（不可撤回）
    r = click_text(["立即沟通"])
    if r == "not_found":
        r2 = click_text(["继续沟通"])
        print(f"[已沟通过·跳过打招呼] 继续沟通={r2}")
    else:
        print(f"[点立即沟通] {r}")
    time.sleep(3.5)

    snap = body_text()
    print(f"[当前] {evaluate('location.href')}")
    print(f"[弹窗?] 已向BOSS发送消息={'已向BOSS发送消息' in snap} 继续沟通={'继续沟通' in snap}")
    call("screenshot", {"path": os.path.join(WORK, "deliver_after_greet.png"), "format": "png"})

    enter_chat()
    # 用命中岗位的真实公司名选会话；选不到或匹配错一律硬中止，杜绝发错人
    conv = pick_conversation(job["company"])
    print(f"[选会话] {conv}")
    ok = "conv_clicked" in conv and "MISMATCH" not in conv
    if not ok:
        # 兜底：代招职位会话显示猎头公司名，按招聘公司名找不到。
        # 此时若默认选中的会话岗位名与目标岗位一致（前 6 字命中），视为正确会话放行
        now_part = conv.split("@")[-1] if "@" in conv else ""
        title_head = str(job.get("title", ""))[:6]
        if "conv_not_found" in conv and title_head and title_head in now_part:
            print(f"[选会话·兜底] 默认会话岗位名匹配「{now_part[:30]}」，放行")
            ok = True
    if not ok:
        print("!! 目标公司会话未找到或不匹配，中止发送（防止发错人）")
        call("screenshot", {"path": os.path.join(WORK, "deliver_no_conv.png"), "format": "png"})
        return 4
    time.sleep(2)

    fill = fill_chat(text)
    print(f"[填充] {fill}")
    time.sleep(1.5)
    print(f"[发送键] {evaluate(r'''(function(){var b=document.querySelector('button.btn-send');return b?((b.className||'')+'|'+(b.textContent||'').trim()):'none';})()''')}")

    call("screenshot", {"path": os.path.join(WORK, "deliver_filled.png"), "format": "png"})
    print(f"[截图] {os.path.join(WORK, 'deliver_filled.png')}")

    if mode == "send":
        s = click_send()
        print(f"[发送] {s}")
        time.sleep(2.5)
        call("screenshot", {"path": os.path.join(WORK, "deliver_sent.png"), "format": "png"})
        print(f"[截图] {os.path.join(WORK, 'deliver_sent.png')}")
    else:
        print("\n>>> 已停在发送前。确认后回复「发送」。")
    return 0


def chat_only(company_kw, mode, text=None):
    """只进消息页，给指定公司会话发消息（补发用）。"""
    if not text:
        from scripts.recruit_greetings import pick_by_company
        text, src, meta = pick_by_company(company_kw)
        if not text:
            print(f"[补发] 招聘目录里没找到「{company_kw}」的专属话术，回退通用")
            job = {"title": "", "company": company_kw, "jobId": ""}
            text, meta = pick_greeting_for_job(job)
            meta = dict(meta or {})
            meta["source"] = "generic"
        else:
            meta = dict(meta)
            meta["source"] = src
        print(f"[补发目标] {company_kw} | {meta.get('folder') or '(通用)'} | {len(text)} 字")
    print(f"[话术] {text[:120]}...")
    navigate(CHAT_URL)
    ok = wait_chat_ready(timeout=60)
    print(f"[消息页就绪] {ok}")
    time.sleep(2)
    print(f"[选会话] {pick_conversation(company_kw)}")
    time.sleep(2)
    print(f"[填充] {fill_chat(text)}")
    time.sleep(1.5)
    print(f"[发送键] {evaluate(r'''(function(){var b=document.querySelector('button.btn-send');return b?((b.className||'')+'|'+(b.textContent||'').trim()):'none';})()''')}")
    call("screenshot", {"path": os.path.join(WORK, "deliver_filled.png"), "format": "png"})
    if mode == "chat-send":
        print(f"[发送] {click_send()}")
        time.sleep(2.5)
        call("screenshot", {"path": os.path.join(WORK, "deliver_sent.png"), "format": "png"})
    else:
        print("\n>>> 已停在发送前。确认后回复「发送」。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
