#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""wb.py — Kimi WebBridge HTTP 客户端封装（Windows 安全版）。

Windows 下 PowerShell 会破坏内联 JSON 里的中文字符，
因此所有请求都写成临时文件再用 curl.exe --data-binary 发送。
"""
import json
import os
import subprocess
import urllib.parse

BASE = "http://127.0.0.1:10086/command"
SESSION = "boss-main"

# 本进程的工作标签页（navigate 复用，避免每次新开标签页）
_WORK_TAB = {"id": None}


def call(action, args=None, session=SESSION, timeout=120):
    body = {"action": action, "args": args or {}, "session": session}
    p = os.path.join(os.environ["TEMP"], f"wb-{action}-{os.getpid()}.json")
    with open(p, "w", encoding="utf-8") as f:
        json.dump(body, f, ensure_ascii=False)
    try:
        r = subprocess.run(
            ["curl.exe", "-s", "-X", "POST", BASE,
             "-H", "Content-Type: application/json", "--data-binary", "@" + p],
            capture_output=True, text=True, encoding="utf-8", timeout=timeout)
        try:
            return json.loads(r.stdout)
        except Exception:
            return {"raw": r.stdout}
    finally:
        try:
            os.remove(p)
        except OSError:
            pass


def evaluate(code, session=SESSION):
    """执行 JS，返回字符串结果。"""
    r = call("evaluate", {"code": code}, session)
    if not r.get("ok"):
        return ""
    return r.get("data", {}).get("value", "")


def evaluate_json(code, session=SESSION):
    """执行 JS 并把返回的 JSON 字符串解析成 dict。"""
    v = evaluate(code, session)
    if not v:
        return {}
    try:
        return json.loads(v)
    except Exception:
        return {"_raw": v[:2000]}


def navigate(url, new_tab=False, session=SESSION, wait=0):
    """导航到 URL。

    标签页复用：记住本会话的工作标签页 ID，后续导航带 tabId 复用同一标签页，
    避免每次投递都新开一个标签页（WebBridge 生命周期模式下不带 tabId 会新建页面）。
    标签页失效（被关/被风控清掉）时自动降级新开。
    """
    import time
    args = {"url": url}
    if new_tab or _WORK_TAB["id"] is None:
        args["newTab"] = True
    else:
        args["tabId"] = _WORK_TAB["id"]
    r = call("navigate", args, session)
    d = (r or {}).get("data") or {}
    if d.get("tabId"):
        _WORK_TAB["id"] = d["tabId"]
    elif _WORK_TAB["id"] is not None and not d.get("success", True):
        # 复用失败：清掉缓存的 tabId，下次新开
        _WORK_TAB["id"] = None
    if wait:
        time.sleep(wait)
    return r


def search_url(query, city_code="101280100"):
    q = urllib.parse.quote(query)
    # 注意：路由必须是 /web/geek/jobs（复数）；/web/geek/job 会被 BOSS 重定向到登录页
    return f"https://www.zhipin.com/web/geek/jobs?query={q}&city={city_code}"


def list_cards():
    """列出当前职位列表页的卡片（公司 / 岗位 / 薪资 / 城市 / 聊天按钮状态）。"""
    code = r"""JSON.stringify((function(){
        var cards = document.querySelectorAll('.job-card-wrapper, .job-card-box, .job-list-box li, li.job-card-wrapper');
        var out = [];
        for (var i = 0; i < cards.length; i++) {
            var c = cards[i];
            function txt(sel){
                var e = c.querySelector(sel);
                return e ? e.textContent.trim() : '';
            }
            var btn = c.querySelector('.btn-startchat, .op-btn-chat, a.chat-btn, .btn-chat');
            out.push({
                idx: i,
                title: txt('.job-name, .job-title, .job-area .job-name'),
                company: txt('.company-name, .company-info a, .boss-name'),
                salary: txt('.salary, .job-salary, .salary-area'),
                area: txt('.job-area .workplace, .company-location, .job-area'),
                tag: txt('.tag-list, .job-info .tag'),
                chatBtn: btn ? btn.textContent.trim() : '',
                href: (c.querySelector('a.job-name, a.job-card-left') || {}).href || ''
            });
        }
        return out;
    })())"""
    return evaluate_json(code)


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "cards":
        for c in list_cards():
            print(f"[{c['idx']:>2}] {c['title'][:26]:<28} {c['salary'][:10]:<12} "
                  f"{c['company'][:18]:<20} {c['area'][:16]:<18} {c['chatBtn']}")
    else:
        print(call("list_tabs", {}, SESSION))
