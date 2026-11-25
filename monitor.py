import difflib
import hashlib
import json
import os
import re
import time
from urllib.parse import urljoin

import requests
from playwright.sync_api import sync_playwright

# ================= 配置区 =================
TARGETS = [
    {
        "name": "KOBROX 官网",
        "url": "https://www.kobrox.net",
        "required_emails": ["manager@kobrox.net"],
        "required_texts": [],
        "check_qr": False,
        "interact_for_wechat": False
    },
    {
        "name": "FAVILLA 官网",
        "url": "https://www.favillacigar.com",
        "required_emails": [],
        "required_texts": ["13587447654"],
        "check_qr": True,
        "interact_for_wechat": True  # 开启自动点击弹窗/购物车流程以检测微信号和二维码
    }
]

STATE_FILE = "last_state.json"
DINGTALK_WEBHOOK = os.environ.get("DINGTALK_WEBHOOK")
QR_KEYWORDS = ("qr", "wechat", "weixin", "wx", "code", "kefu", "kf", "pay", "码")
# ==========================================


def send_dingtalk(title, text):
    if not DINGTALK_WEBHOOK:
        print("错误：未读取到 DINGTALK_WEBHOOK 环境变量")
        return
    payload = {
        "msgtype": "markdown",
        "markdown": {
            "title": title,
            "text": text
        }
    }
    resp = requests.post(DINGTALK_WEBHOOK, json=payload, timeout=15)
    resp.raise_for_status()
    print("钉钉推送响应:", resp.text)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def hash_image_source(src: str) -> str:
    if src.startswith("data:image"):
        return sha256_bytes(src.encode("utf-8"))
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }
    resp = requests.get(src, headers=headers, timeout=15)
    resp.raise_for_status()
    return sha256_bytes(resp.content)


def extract_qr_hashes(page, base_url, extra_text):
    """从当前页面 DOM 和已抓取的文本中提取二维码图片哈希"""
    qr_hashes = {}
    candidate_urls = set()

    # 1) 扫描当前页面所有 <img> 标签（包含 Base64 内嵌图）
    try:
        imgs = page.eval_on_selector_all(
            "img",
            """els => els.map(el => ({
                src: el.getAttribute('src') || '',
                alt: el.getAttribute('alt') || '',
                cls: el.getAttribute('class') || ''
            }))"""
        )
    except Exception:
        imgs = []

    for img in imgs:
        src = img["src"].strip()
        if not src:
            continue
        if src.startswith("data:image"):
            # 过滤掉极小的图标，保留二维码等 Base64 大图
            if len(src) >= 1500:
                candidate_urls.add(src)
        else:
            full_src = urljoin(base_url, src)
            meta = f"{full_src} {img['alt']} {img['cls']}".lower()
            if any(k in meta for k in QR_KEYWORDS):
                candidate_urls.add(full_src)

    # 2) 从 HTML / JS / JSON 文本中扫描 Base64 内嵌图
    b64_matches = re.findall(
        r'data:image/(?:png|jpe?g|webp|gif);base64,[A-Za-z0-9+/=]{1500,}',
        extra_text,
        flags=re.IGNORECASE
    )
    for b64_img in set(b64_matches):
        candidate_urls.add(b64_img)

    # 3) 从 JS / JSON 文本中扫描二维码链接
    js_img_matches = re.findall(
        r'["\']((?:https?://|/|assets/)[^"\'\s]{1,200}\.(?:png|jpe?g|webp|gif))["\']',
        extra_text,
        flags=re.IGNORECASE
    )
    for match in js_img_matches:
        if any(k in match.lower() for k in QR_KEYWORDS):
            candidate_urls.add(urljoin(base_url, match))

    for qurl in sorted(candidate_urls):
        try:
            img_hash = hash_image_source(qurl)
            key = f"base64:{img_hash[:16]}" if qurl.startswith("data:image") else qurl
            qr_hashes[key] = img_hash
        except Exception as img_err:
            key = "base64:error" if qurl.startswith("data:image") else qurl
            qr_hashes[key] = f"DOWNLOAD_ERROR:{img_err}"

    return qr_hashes


def try_click_by_text(page, patterns):
    for pat in patterns:
        try:
            loc = page.locator(f"text=/{pat}/i").first
            if loc.count() > 0 and loc.is_visible():
                loc.click(timeout=3000)
                return pat
        except Exception:
            continue
    return None


def fill_visible_inputs(page):
    """给结算表单里的可见输入框填入测试信息，以便进入微信付款步骤"""
    inputs = page.locator("input:visible, textarea:visible")
    count = inputs.count()
    for i in range(count):
        try:
            el = inputs.nth(i)
            itype = (el.get_attribute("type") or "text").lower()
            if itype in ("hidden", "checkbox", "radio", "submit", "button", "file", "search"):
                continue
            hint = f"{el.get_attribute('placeholder') or ''} {el.get_attribute('name') or ''} {el.get_attribute('id') or ''}".lower()
            if any(k in hint for k in ("phone", "tel", "mobile", "手机", "电话", "联系")):
                el.fill("13800138000")
            elif any(k in hint for k in ("mail", "邮箱")):
                el.fill("test@example.com")
            elif any(k in hint for k in ("addr", "地址")):
                el.fill("广东省广州市天河区测试路1号")
            else:
                el.fill("测试用户")
        except Exception:
            continue


def interact_to_reveal_wechat(page, base_url, extra_chunks, required_texts):
    """尝试点开右下角官方微信顾问或走一遍购物车第二步，触发微信号和二维码渲染"""
    def check_now():
        body_txt = page.inner_text("body")
        html_txt = page.content()
        combined = f"{body_txt}\n{html_txt}\n" + "\n".join(extra_chunks)
        missing = [t for t in required_texts if t not in combined]
        qrs = extract_qr_hashes(page, base_url, combined)
        return missing, qrs

    # 途径 1：尝试点击右下角「专属品鉴顾问 / 企业微信官方直连」
    clicked = try_click_by_text(page, ["专属品鉴顾问", "企业微信官方直连", "官方微信", "联系顾问"])
    if clicked:
        print(f"[交互探针] 已点击右下角入口: {clicked}")
        page.wait_for_timeout(2000)
        missing, qrs = check_now()
        if not missing and qrs:
            return missing, qrs

    # 途径 2：走购物车 -> 确认收货信息 -> 添加官方微信完成付款
    try_click_by_text(page, ["加入购物车", "加入清单", "立即购买", "立即选购"])
    page.wait_for_timeout(1200)

    # 如果首页没有直接的加购按钮，尝试先点开第一个商品再加购
    if not try_click_by_text(page, ["购物车"]):
        try:
            page.locator("img").nth(1).click(timeout=2000)
            page.wait_for_timeout(1200)
        except Exception:
            pass
        try_click_by_text(page, ["加入购物车", "加入清单", "立即购买"])
        page.wait_for_timeout(1200)
        try_click_by_text(page, ["购物车"])

    page.wait_for_timeout(1500)
    try_click_by_text(page, ["去结算", "立即结算", "确认下单", "下一步"])
    page.wait_for_timeout(1200)

    # 填写收货信息并进入第二步
    fill_visible_inputs(page)
    page.wait_for_timeout(500)
    step2_btn = try_click_by_text(page, ["下一步", "添加官方微信", "前往付款", "提交订单", "确认收货信息", "确认并继续"])
    if step2_btn:
        print(f"[交互探针] 已点击进入结算第二步: {step2_btn}")
    page.wait_for_timeout(2500)

    return check_now()


def fetch_site_data(browser, target):
    url = target["url"]
    name = target["name"]
    last_err = None

    for attempt in range(2):
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            viewport={"width": 1440, "height": 900}
        )
        page = context.new_page()
        extra_chunks = []

        def handle_response(resp):
            try:
                if resp.status != 200:
                    return
                rurl = resp.url.lower()
                if any(skip in rurl for skip in ("google-analytics", "googletagmanager", "hm.baidu.com", "clarity.ms")):
                    return
                ctype = resp.headers.get("content-type", "").lower()
                path_no_query = rurl.split("?")[0]
                if "javascript" in ctype or "json" in ctype or path_no_query.endswith(".js"):
                    extra_chunks.append(resp.text())
            except Exception:
                pass

        page.on("response", handle_response)

        try:
            response = page.goto(url, wait_until="domcontentloaded", timeout=30000)
            if response is None:
                raise RuntimeError("服务器无响应")
            if response.status >= 400:
                raise RuntimeError(f"HTTP 状态码异常: {response.status}")

            page.wait_for_timeout(4500)

            # 1) 先提取干净的首页正文和价格（用于变动对比，不受后续点击弹窗干扰）
            visible_text = page.inner_text("body")
            raw_html = page.content()
            lines = [line.strip() for line in visible_text.splitlines() if line.strip()]
            normalized_text = "\n".join(lines)
            text_hash = sha256_bytes(normalized_text.encode("utf-8"))

            emails = sorted(set(
                e.lower() for e in re.findall(
                    r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}",
                    normalized_text + "\n" + raw_html
                )
            ))

            # 2) 在首页 + 已捕获的所有 JS/JSON 中初步校验关键文字和二维码
            searchable_source = f"{normalized_text}\n{raw_html}\n" + "\n".join(extra_chunks)
            req_texts = target.get("required_texts", [])
            missing_texts = [req for req in req_texts if req not in searchable_source]
            qr_hashes = extract_qr_hashes(page, url, searchable_source) if target.get("check_qr") else {}

            print(f"[{name}] 首页加载捕获 {len(extra_chunks)} 个 JS/JSON 资源，初步识别二维码 {len(qr_hashes)} 张，缺失关键词: {missing_texts}")

            # 3) 若开启了交互探针，且关键文字或二维码尚未全部找到，则自动点击进入微信付款步骤再抓一次
            if target.get("interact_for_wechat") and (missing_texts or (target.get("check_qr") and not qr_hashes)):
                print(f"[{name}] 启动自动点击探针，尝试打开微信付款/顾问弹窗...")
                missing_texts, qr_hashes = interact_to_reveal_wechat(page, url, extra_chunks, req_texts)
                print(f"[{name}] 交互后识别二维码 {len(qr_hashes)} 张，缺失关键词: {missing_texts}")

            context.close()
            return {
                "text_hash": text_hash,
                "lines": lines,
                "emails": emails,
                "missing_texts": missing_texts,
                "qr_hashes": qr_hashes
            }
        except Exception as e:
            last_err = e
            context.close()
            if attempt == 0:
                time.sleep(5)

    raise last_err


def get_text_diff(old_lines, new_lines, max_lines=12):
    diff = list(difflib.unified_diff(old_lines, new_lines, lineterm="", n=0))
    changes = [
        line for line in diff
        if (line.startswith("+") or line.startswith("-"))
        and not line.startswith("+++")
        and not line.startswith("---")
    ]
    if not changes:
        return ""
    preview = changes[:max_lines]
    if len(changes) > max_lines:
        preview.append(f"...（其余 {len(changes) - max_lines} 处变动已省略）")
    return "\n".join(f"> {c}" for c in preview)


def load_state():
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def main():
    saved_state = load_state()
    critical_alerts = []
    change_alerts = []
    init_reports = []
    state_updated = False

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)

        for target in TARGETS:
            name = target["name"]
            url = target["url"]

            try:
                current = fetch_site_data(browser, target)
            except Exception as e:
                print(f"[{name}] 无法打开: {e}")
                critical_alerts.append(
                    f"#### ❌ {name} 无法打开\n"
                    f"- **网址**：{url}\n"
                    f"- **原因**：`{e}`"
                )
                continue

            site_issues = []

            # 1. 校验邮箱
            req_emails = [e.lower() for e in target.get("required_emails", [])]
            if req_emails:
                for req_email in req_emails:
                    if req_email not in current["emails"]:
                        site_issues.append(f"正确邮箱 `{req_email}` **已消失或被篡改**！当前检测到的邮箱：`{current['emails']}`")
                extra_emails = [e for e in current["emails"] if e not in req_emails]
                if extra_emails:
                    site_issues.append(f"发现异常/新增邮箱：`{extra_emails}`")

            # 2. 校验指定文字（如企业微信号）
            for missing in current["missing_texts"]:
                site_issues.append(f"关键信息 `{missing}` **已消失或被篡改**！")

            # 3. 历史状态对比
            if url not in saved_state:
                saved_state[url] = {
                    "text_hash": current["text_hash"],
                    "lines": current["lines"],
                    "emails": current["emails"],
                    "qr_hashes": current["qr_hashes"]
                }
                state_updated = True
                if target.get("check_qr"):
                    qr_count = len(current["qr_hashes"])
                    qr_info = f"识别到 {qr_count} 张二维码/内嵌图片"
                else:
                    qr_info = "未开启"
                init_reports.append(
                    f"- **{name}** ({url})\n"
                    f"  - 检测到邮箱：`{current['emails'] or '无'}`\n"
                    f"  - 二维码监控：{qr_info}"
                )
            else:
                prev = saved_state[url]

                if target.get("check_qr"):
                    prev_qr = prev.get("qr_hashes", {})
                    curr_qr = current["qr_hashes"]
                    if curr_qr != prev_qr:
                        site_issues.append(
                            f"**二维码图片发生变化**！（原数量：{len(prev_qr)}，现数量：{len(curr_qr)}，图片链接或图像内容已被修改）"
                        )
                        saved_state[url]["qr_hashes"] = curr_qr
                        state_updated = True

                if current["text_hash"] != prev.get("text_hash"):
                    diff_preview = get_text_diff(prev.get("lines", []), current["lines"])
                    change_alerts.append(
                        f"#### 📝 {name} 内容/价格变动\n"
                        f"- **网址**：[点击核对]({url})\n"
                        f"- **主要差异**（`-` 为旧内容，`+` 为新内容）：\n\n{diff_preview}"
                    )
                    saved_state[url]["text_hash"] = current["text_hash"]
                    saved_state[url]["lines"] = current["lines"]
                    saved_state[url]["emails"] = current["emails"]
                    state_updated = True
                else:
                    print(f"[{name}] 页面内容无变动。")

            if site_issues:
                issue_list = "\n".join(f"- {item}" for item in site_issues)
                critical_alerts.append(f"#### 🚨 {name} 关键信息异常\n- **网址**：[点击核对]({url})\n{issue_list}")

        browser.close()

    if state_updated:
        save_state(saved_state)

    if init_reports:
        send_dingtalk(
            "网页监控初始化成功",
            "### ✅ 网页监控已启动\n\n已记录初始基准，请核对以下识别结果：\n\n" + "\n".join(init_reports)
        )

    if critical_alerts:
        send_dingtalk(
            "【紧急】网页监控异常告警",
            "### 🚨 网页监控紧急告警\n\n" + "\n\n---\n\n".join(critical_alerts)
        )

    if change_alerts:
        send_dingtalk(
            "网页监控内容/价格变动提醒",
            "### 🔔 检测到网页监控内容变动\n\n" + "\n\n---\n\n".join(change_alerts)
        )


if __name__ == "__main__":
    main()
