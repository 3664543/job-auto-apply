# -*- coding: utf-8 -*-
"""auto_reply.py — 自动回复 HR 消息。

流程：
  1. 打开消息页，列出全部会话（含未读标记、最新消息时间）
  2. 对有未读/最新回复的会话逐个点开，读取 HR 最新消息
  3. AI 分类意图（邀面试/问薪资/了解意向/要简历/拒绝/其他）并生成回复
  4. draft 模式（默认）：回复只展示不发送；send 模式：确认后自动发送

用法:
    python -m scripts.auto_reply           # 只读消息 + 生成回复草稿
    python -m scripts.auto_reply send 1    # 对第 N 个会话发送草稿回复
"""
import io
import json
import os
import random
import sys
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.wb import navigate, evaluate, call  # noqa: E402

CHAT_URL = "https://www.zhipin.com/web/geek/chat"
_PROFILE_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "user_profile.json")


def list_convs():
    """列出消息页全部会话：名字、公司、未读标记、最新消息预览。"""
    code = r"""JSON.stringify((function(){
        var out=[];
        document.querySelectorAll('.friend-content').forEach(function(e){
            var r=e.getBoundingClientRect();
            if(r.width<=0||r.height<=0) return;
            var name=(e.querySelector('.name-text')||{}).textContent||'';
            var full=((e.querySelector('.title-box')||{}).textContent||'').trim();
            var msg=(e.querySelector('.last-msg-text')||{}).textContent||'';
            var time=(e.querySelector('.time')||{}).textContent||'';
            var unread=e.querySelector('[class*=unread],[class*=badge],[class*=dot]');
            out.push({name:name.trim().slice(0,20), full:full.slice(0,40),
                      msg:msg.trim().slice(0,50), time:time.trim(), unread:!!unread});
        });
        return out;
    })())"""
    raw = evaluate(code)
    try:
        return json.loads(raw) if raw else []
    except Exception:
        return []


def read_current_conv(max_items=8):
    """读取当前选中会话的最近聊天记录（区分自己/HR）。"""
    code = r"""JSON.stringify((function(){
        var box=document.querySelector('.chat-record');
        if(!box) return 'no_box';
        var out=[];
        var nodes=box.querySelectorAll('[class*=item], .message-item, .chat-item');
        var n=Math.min(nodes.length, __MAX__);
        for(var i=nodes.length-n;i<nodes.length;i++){
            var e=nodes[i];
            var t=(e.textContent||'').replace(/\s+/g,' ').trim();
            if(!t) continue;
            var mine=/content-me|item-me|self/.test((e.className||'').toString());
            out.push({who: mine?'我':'HR', text:t.slice(0,120)});
        }
        return out;
    })())""".replace("__MAX__", str(max_items))
    raw = evaluate(code)
    try:
        return json.loads(raw) if raw else []
    except Exception:
        return []


def select_conv(index):
    """点开第 index 个会话（从 0 开始）。"""
    code = r"""(function(){
        var list=document.querySelectorAll('.friend-content');
        var visible=[];
        for(var i=0;i<list.length;i++){
            var r=list[i].getBoundingClientRect();
            if(r.width>0&&r.height>0) visible.push(list[i]);
        }
        if(!visible[__IDX__]) return 'not_found';
        visible[__IDX__].scrollIntoView({block:'center'});
        visible[__IDX__].click();
        return 'clicked';
    })()""".replace("__IDX__", str(index))
    return evaluate(code)


def send_text(text):
    """在当前会话输入框发送消息。"""
    # 填充 contenteditable 聊天框
    ok = evaluate(
        r"""(function(){
            var box=document.querySelector('#chat-input, .chat-input[contenteditable=true]');
            if(!box) return 'no_input';
            box.focus();
            return 'focused';
        })()"""
    )
    if ok != "focused":
        return f"fill_fail:{ok}"
    r = call("fill", {"selector": "#chat-input, .chat-input[contenteditable=true]", "value": text, "mode": "contenteditable"})
    d = (r.get("data") or {})
    if not d.get("success"):
        return f"fill_fail:{str(r)[:120]}"
    time.sleep(1)
    btn = evaluate(r"""(function(){
        var b=document.querySelector('button.btn-send');
        if(!b||b.disabled) return 'no_btn';
        b.click();
        return 'clicked';
    })()""")
    return btn


def draft_reply(messages, conv_name):
    """根据 HR 最新消息生成回复草稿（规则版，个人内容从 user_profile.json 读取）。"""
    hr_msgs = [m["text"] for m in messages if m.get("who") == "HR"]
    last = hr_msgs[-1] if hr_msgs else ""
    try:
        p = json.load(open(_PROFILE_PATH, encoding="utf-8"))
    except Exception:
        p = {}
    exp = (p.get("experience") or "").strip() or "（请在 user_profile.json 的 experience 字段配置背景）"
    kw = [
        ("简历", f"您好，稍后我把简历发您。我的相关背景：{exp[:120]}"),
        ("薪资", "您好，期望薪资可沟通，目前在职，两周内可到岗。"),
        ("面试", "您好，我这边时间方便，具体时间听您安排。"),
        ("到岗", "您好，我目前在职，预计两周内可以到岗，具体可商议。"),
        ("经验", f"您好，我的相关经验：{exp[:150]}"),
    ]
    for k, reply in kw:
        if k in last:
            return f"【规则·命中关键词「{k}」】{reply}"
    if last:
        return f"【规则·通用】您好，感谢您的回复。我对这个岗位很感兴趣，方便说下接下来流程或需要我补充什么材料吗？（待人工确认，或接入 AI 生成更贴合的回复）"
    return "【无 HR 回复】该会话暂无新消息"


def ai_reply(conv_name, hr_text):
    """AI 分析 HR 消息：分类 + 生成回复。返回 dict 或 None（AI 失败）。

    API key 从环境变量 ZHIPU_API_KEY 读取；求职者背景从 user_profile.json 读取，
    代码中不硬编码任何个人信息。
    """
    import urllib.request
    key = os.environ.get("ZHIPU_API_KEY", "")
    if not key:
        print("   [AI跳过] 未设置 ZHIPU_API_KEY 环境变量")
        return None
    try:
        _p = json.load(open(_PROFILE_PATH, encoding="utf-8"))
    except Exception:
        _p = {}
    persona = (_p.get("experience") or "").strip()
    wechat = (_p.get("wechat") or "").strip()
    if not persona:
        print("   [AI跳过] user_profile.json 未配置求职者背景（experience 字段）")
        return None
    prompt = (
        f"你是求职者，背景如下：{persona}\n"
        f"HR（{conv_name}）最新消息：{hr_text}\n"
        "注意：消息里混有 BOSS 系统卡片文本（如「你与该职位竞争者PK情况」「附件简历已发送」"
        "「您正在与Boss沟通」），这些不是 HR 说的话，忽略它们，只看 HR 真正打的字。\n"
        "判断类别：\n"
        "- invite: 邀面试/约沟通 → 积极回复，时间只说「时间方便，具体您定」，不许承诺具体时间；"
        + (f"如需交换联系方式可提供微信：{wechat}\n" if wechat else "\n")
        + "- interested: HR 真人明确表示感兴趣/主动跟进 → 礼貌表达意向，请求岗位详情\n"
        "- question: 问具体问题 → 如实简洁回答；不知道的如实说需确认，绝不编造\n"
        "- reject: 婉拒/不合适/已有人选 → 不回复\n"
        "- outsource: 消息中出现以下任一特征 → 一律不回复（求职者明确要求）：外派/外包/OD模式/OA模式/派遣/人力外包/劳务/驻场/代招猎头岗位\n"
        "- none: 仅系统消息/无 HR 实质内容 → 不回复\n"
        '只输出 JSON：{"category":"...","reply":"...","reason":"..."}；不回复时 reply 为空字符串。\n'
        "硬性禁令：回复中不得编造微信号、手机号、具体时间、薪资数字等任何求职者资料中不存在的信息；"
        "涉及约时间只写「时间方便，具体听您安排」。回复口语化自然、80字内、不要过度热情。"
    )
    body = json.dumps({"model": "glm-4-flash",
                       "messages": [{"role": "user", "content": prompt}],
                       "max_tokens": 300, "temperature": 0.4}).encode()
    req = urllib.request.Request(
        "https://open.bigmodel.cn/api/paas/v4/chat/completions", data=body,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"})
    try:
        r = json.loads(urllib.request.urlopen(req, timeout=30).read())
        content = r["choices"][0]["message"]["content"]
        content = content[content.find("{"): content.rfind("}") + 1]
        return json.loads(content)
    except Exception as e:
        print(f"   [AI失败] {e}")
        return None


def is_today(t):
    """会话时间是否为今天（今天显示 HH:MM，含冒号）。"""
    return ":" in (t or "")


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "read"
    idx = int(sys.argv[2]) if len(sys.argv) > 2 else None

    navigate(CHAT_URL, wait=5)
    time.sleep(3)
    convs = list_convs()

    if mode == "read" and idx is None:
        print(f"[会话] 共 {len(convs)} 个")
        for i, c in enumerate(convs):
            mark = "*未读" if c.get("unread") else "    "
            print(f"  {i}: {mark} {c['name']} | {c['time']} | {c['msg']}")
        return 0

    if mode == "send" and idx is not None:
        # 指定会话：AI 分析 + 展示 + 确认发送
        select_conv(idx)
        time.sleep(2)
        msgs = read_current_conv()
        hr_text = " ".join(m["text"] for m in msgs if m["who"] == "HR")[-300:]
        a = ai_reply(convs[idx]["name"], hr_text)
        if not a or not a.get("reply"):
            print(f"[不回复] {convs[idx]['name']} | {a.get('category') if a else 'AI失败'} | {a.get('reason') if a else ''}")
            return 0
        print(f"[{a['category']}] 回复 {convs[idx]['name']}: {a['reply']}")
        print(f"[发送] {send_text(a['reply'])}")
        return 0

    # auto 模式：扫描今天投递的会话，AI 筛选值得回复的自动发送
    today = [(i, c) for i, c in enumerate(convs) if is_today(c.get("time"))]
    print(f"[会话] 共 {len(convs)} 个，其中今天活跃 {len(today)} 个，开始 AI 分析...\n")
    sent = 0
    for i, c in today:
        select_conv(i)
        time.sleep(2)
        msgs = read_current_conv()
        hr_texts = [m["text"] for m in msgs if m["who"] == "HR"]
        if not hr_texts:
            continue
        hr_text = " ".join(hr_texts)[-300:]
        a = ai_reply(c["name"], hr_text)
        if not a:
            continue
        cat, reply, reason = a.get("category"), (a.get("reply") or "").strip(), a.get("reason", "")
        print(f"--- {i}: {c['name']} | {c['time']} | HR: {hr_text[:60]}")
        print(f"    AI: {cat} | {reason[:50]}")
        if cat in ("invite", "interested", "question") and reply:
            r = send_text(reply)
            ok = "clicked" in str(r)
            print(f"    [已发送] {reply[:80]} -> {r}")
            if ok:
                sent += 1
            time.sleep(random.uniform(4, 8))
        else:
            print(f"    [跳过] {cat}")
        time.sleep(random.uniform(2, 5))
    print(f"\n[完成] 分析 {len(today)} 个今日会话，自动回复 {sent} 个")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
