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
        "interact_for_wechat": True
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
    """提取当前页面中渲染出的二维码图片哈希（兼容 Base64 内嵌图与图片链接）"""
    qr_hashes = {}
    candidate_urls = set()

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
            if len(src) >= 1500:
                candidate_urls.add(src)
        else:
            full_src = urljoin(base_url, src)
            meta = f"{full_src} {img['alt']} {img['cls']}".lower()
            if any(k in meta for k in QR_KEYWORDS):
                candidate_urls.add(full_src)

    b64_matches = re.findall(
        r'data:image/(?:png|jpe?g|webp|gif);base64,[A-Za-z0-9+/=]{1500,}',
        extra_text,
        flags=re.IGNORECASE
    )
    for b64_img in set(b64_matches):
        candidate_urls.add(b64_img)

    for qurl in sorted(candidate_urls):
        try:
            img_hash = hash_image_source(qurl)
            key = f"base64:{img_hash[:16]}" if qurl.startswith("data:image") else qurl
            qr_hashes[key] = img_hash
        except Exception as img_err:
            key = "base64:error" if qurl.startswith("data:image") else qurl
            qr_hashes[key] = f"DOWNLOAD_ERROR:{img_err}"

    return qr_hashes


def list_visible_buttons(page, limit=25):
    texts = []
    try:
        raw = page.eval_on_selector_all(
            "button, a, [role='button']",
            """els => els
                .filter(el => el.offsetParent !== null)
                .map(el => (el.innerText || '').replace(/\\s+/g, ' ').trim())
                .filter(t => t.length > 0 && t.length <= 35)"""
        )
        for t in raw:
            if t not in texts:
                texts.append(t)
            if len(texts) >= limit:
                break
    except Exception:
        pass
    return texts


def try_click_by_text(page, patterns):
    """按优先级依次尝试点击可见的按钮或文本元素"""
    for pat in patterns:
        for group in (
            page.locator("button, a, [role='button']").filter(has_text=pat),
            page.get_by_text(pat),
        ):
            try:
                count = min(group.count(), 5)
                for idx in range(count):
                    el = group.nth(idx)
                    if el.is_visible():
                        el.click(timeout=3000)
                        return pat
            except Exception:
                continue
    return None


def fill_shipping_form(page):
    """自动填写「确认收货信息」表单里的可见输入框和下拉框"""
    inputs = page.locator("input:visible, textarea:visible")
    count = inputs.count()
    filled = 0
    for i in range(count):
        try:
            el = inputs.nth(i)
            if not el.is_editable():
                continue
            itype = (el.get_attribute("type") or "text").lower()
            if itype in ("hidden", "checkbox", "radio", "submit", "button", "file", "search"):
                continue
            hint = f"{el.get_attribute('placeholder') or ''} {el.get_attribute('name') or ''} {el.get_attribute('id') or ''}".lower()
            # 跳过顶部搜索框
            if any(k in hint for k in ("search", "搜索", "查找")):
                continue
            if any(k in hint for k in ("phone", "tel", "mobile", "手机", "电话", "联系")):
                el.fill("13800138000")
            elif any(k in hint for k in ("mail", "邮箱")):
                el.fill("test@example.com")
            elif any(k in hint for k in ("addr", "地址", "省", "市", "区", "街道")):
                el.fill("广东省广州市天河区测试路1号")
            elif any(k in hint for k in ("remark", "note", "备注", "刻字")):
                el.fill("测试")
            else:
                el.fill("测试用户")
            filled += 1
        except Exception:
            continue

    # 若表单包含省市区下拉框，默认选择第一个有效选项
    selects = page.locator("select:visible")
    for i in range(selects.count()):
        try:
            sel = selects.nth(i)
            if sel.locator("option").count() > 1:
                sel.select_option(index=1)
        except Exception:
            continue

    return filled


def interact_to_reveal_wechat(page, base_url, extra_chunks, required_texts):
    """按固定流程：加购商品 -> 点购物车 -> 点立即结算 -> 确认收货信息进入第二步"""
    def check_now():
        body_txt = page.inner_text("body")
        html_txt = page.content()
        combined = f"{body_txt}\n{html_txt}\n" + "\n".join(extra_chunks)
        missing = [t for t in required_texts if t not in combined]
        qrs = extract_qr_hashes(page, base_url, combined)
        return missing, qrs

    # 1. 先向购物车加入 1 件商品（新浏览器会话默认购物车为空）
    page.mouse.wheel(0, 700)
    page.wait_for_timeout(1500)
    add_btn = try_click_by_text(page, ["加入购物车", "加入购物袋", "加入清单", "加购", "立即购买", "立即选购"])
    if not add_btn:
        # 首页卡片没有直接加购按钮时，点开带“¥”价格的第一个商品卡片再加购
        try:
            price_el = page.get_by_text("¥").first
            if price_el.count() > 0 and price_el.is_visible():
                price_el.click(timeout=3000)
                print("[购物流程] 步骤1a: 已点击商品卡片进入详情")
                page.wait_for_timeout(1500)
                add_btn = try_click_by_text(page, ["加入购物车", "加入购物袋", "加入清单", "立即购买", "立即选购", "确认加入"])
        except Exception as e:
            print(f"[购物流程] 步骤1a 点击商品卡片异常: {e}")

    print(f"[购物流程] 步骤1 加购结果: {add_btn}")
    page.wait_for_timeout(1200)

    # 2. 点击右上角「购物车」
    cart_btn = try_click_by_text(page, ["购物车"])
    print(f"[购物流程] 步骤2 打开购物车: {cart_btn}")
    page.wait_for_timeout(1500)

    # 3. 点击「立即结算 / 添加官方微信付款」
    print(f"[购物流程] 购物车可见按钮: {list_visible_buttons(page)}")
    checkout_btn = try_click_by_text(
        page,
        ["立即结算", "添加官方微信付款", "添加官方微信完成付款", "去结算", "立即下单", "结算"]
    )
    print(f"[购物流程] 步骤3 点击结算: {checkout_btn}")
    page.wait_for_timeout(1500)

    # 4. 填写收货信息并点击「确认收货信息」进入第二步
    filled = fill_shipping_form(page)
    print(f"[购物流程] 步骤4a 已填写 {filled} 个收货信息字段，当前可见按钮: {list_visible_buttons(page)}")
    page.wait_for_timeout(500)

    step2_btn = try_click_by_text(
        page,
        [
            "确认收货信息",
            "添加官方微信完成付款",
            "添加官方微信付款",
            "进入第二步",
            "下一步",
            "提交订单",
            "锁定订单",
            "确认并继续",
            "去付款"
        ]
    )
    print(f"[购物流程] 步骤4b 进入第二步点击结果: {step2_btn}")
    page.wait_for_timeout(2500)

    # 5. 检查是否已经拿到微信号和二维码
    missing, qrs = check_now()
    if missing or not qrs:
        print(f"[购物流程] 未完全识别，当前可见按钮: {list_visible_buttons(page)}")
        tail_lines = [l.strip() for l in page.inner_text("body").splitlines() if l.strip()][-15:]
        print(f"[购物流程] 页面末尾文字: {tail_lines}")

    return missing, qrs


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
                if "javascript" in ctype or "json" in ctype or rurl.split("?")[0].endswith(".js"):
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

            # 先记录干净的首页正文和价格（不受后续购物车操作影响）
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

            searchable_source = f"{normalized_text}\n{raw_html}\n" + "\n".join(extra_chunks)
            req_texts = target.get("required_texts", [])
            missing_texts = [req for req in req_texts if req not in searchable_source]
            qr_hashes = extract_qr_hashes(page, url, searchable_source) if target.get("check_qr") else {}

            if target.get("interact_for_wechat") and (missing_texts or (target.get("check_qr") and not qr_hashes)):
                print(f"[{name}] 启动购物车结算流程检测微信号与二维码...")
                missing_texts, qr_hashes = interact_to_reveal_wechat(page, url, extra_chunks, req_texts)
                print(f"[{name}] 流程完成：识别二维码 {len(qr_hashes)} 张，缺失关键词: {missing_texts}")

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

            req_emails = [e.lower() for e in target.get("required_emails", [])]
            if req_emails:
                for req_email in req_emails:
                    if req_email not in current["emails"]:
                        site_issues.append(f"正确邮箱 `{req_email}` **已消失或被篡改**！当前检测到的邮箱：`{current['emails']}`")
                extra_emails = [e for e in current["emails"] if e not in req_emails]
                if extra_emails:
                    site_issues.append(f"发现异常/新增邮箱：`{extra_emails}`")

            for missing in current["missing_texts"]:
                site_issues.append(f"关键信息 `{missing}` **已消失或被篡改**！")

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
