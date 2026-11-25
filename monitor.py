import difflib
import hashlib
import json
import os
import re
import time
from urllib.parse import urljoin, urlparse

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
        "qr_selector": "",
        "qr_urls": []
    },
    {
        "name": "FAVILLA 官网",
        "url": "https://www.favillacigar.com",
        "required_emails": [],
        "required_texts": ["13587447654"],
        "check_qr": True,
        "qr_selector": "",
        "qr_urls": []
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
    """计算图片哈希（同时兼容 Base64 内嵌图和普通图片链接）"""
    if src.startswith("data:image"):
        return sha256_bytes(src.encode("utf-8"))
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }
    resp = requests.get(src, headers=headers, timeout=15)
    resp.raise_for_status()
    return sha256_bytes(resp.content)


def get_root_domain(host: str) -> str:
    parts = host.lower().split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else host.lower()


def fetch_site_data(browser, target):
    """访问网页，同时捕获正文、HTML、前端 JS 及同域 JSON 接口数据"""
    url = target["url"]
    root_domain = get_root_domain(urlparse(url).netloc)
    last_err = None

    for attempt in range(2):
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )
        page = context.new_page()
        extra_chunks = []

        def handle_response(resp):
            try:
                if resp.status != 200:
                    return
                resp_host = urlparse(resp.url).netloc.lower()
                if not resp_host.endswith(root_domain):
                    return
                ctype = resp.headers.get("content-type", "").lower()
                path_no_query = resp.url.split("?")[0].lower()
                if (
                    "javascript" in ctype
                    or "json" in ctype
                    or path_no_query.endswith(".js")
                ):
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

            page.wait_for_timeout(4000)

            visible_text = page.inner_text("body")
            raw_html = page.content()
            extra_text = "\n".join(extra_chunks)

            lines = [line.strip() for line in visible_text.splitlines() if line.strip()]
            normalized_text = "\n".join(lines)
            text_hash = sha256_bytes(normalized_text.encode("utf-8"))

            # 仅从正文和 HTML 提取邮箱，避免 JS 注释引发误报
            emails = sorted(set(
                e.lower() for e in re.findall(
                    r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}",
                    normalized_text + "\n" + raw_html
                )
            ))

            # 在正文、HTML、JS 及 JSON 接口中校验关键信息（如企业微信号）
            searchable_source = f"{normalized_text}\n{raw_html}\n{extra_text}"
            missing_texts = [
                req for req in target.get("required_texts", [])
                if req not in searchable_source
            ]

            # 收集并校验二维码图片（同时支持普通 URL 和 Base64 内嵌图）
            qr_hashes = {}
            if target.get("check_qr"):
                candidate_urls = set()

                # 1) 手动指定的图片直链
                for manual_url in target.get("qr_urls", []):
                    if manual_url.strip():
                        candidate_urls.add(manual_url.strip())

                # 2) 当前页面 <img> 标签
                selector = target.get("qr_selector") or "img"
                imgs = page.eval_on_selector_all(
                    selector,
                    """els => els.map(el => ({
                        src: el.getAttribute('src') || '',
                        alt: el.getAttribute('alt') || '',
                        cls: el.getAttribute('class') || ''
                    }))"""
                )
                for img in imgs:
                    src = img["src"].strip()
                    if not src:
                        continue
                    full_src = src if src.startswith("data:image") else urljoin(url, src)
                    if not target.get("qr_selector"):
                        if full_src.startswith("data:image"):
                            if len(full_src) < 1500:
                                continue
                        else:
                            meta = f"{full_src} {img['alt']} {img['cls']}".lower()
                            if not any(k in meta for k in QR_KEYWORDS):
                                continue
                    candidate_urls.add(full_src)

                # 3) 从前端 JS / 接口中扫描二维码图片路径
                js_img_matches = re.findall(
                    r'["\']((?:https?://|/|assets/)[^"\'\s]{1,200}\.(?:png|jpe?g|webp|gif))["\']',
                    extra_text,
                    flags=re.IGNORECASE
                )
                for match in js_img_matches:
                    if any(k in match.lower() for k in QR_KEYWORDS):
                        candidate_urls.add(urljoin(url, match))

                # 4) 从 HTML / JS / 接口中扫描 Base64 内嵌大图（过滤掉小图标，锁定二维码等内嵌图）
                b64_matches = re.findall(
                    r'data:image/(?:png|jpe?g|webp|gif);base64,[A-Za-z0-9+/=]{1500,}',
                    searchable_source,
                    flags=re.IGNORECASE
                )
                for b64_img in set(b64_matches):
                    img_hash = sha256_bytes(b64_img.encode("utf-8"))
                    qr_hashes[f"base64:{img_hash[:16]}"] = img_hash

                for qurl in sorted(candidate_urls):
                    try:
                        img_hash = hash_image_source(qurl)
                        key = f"base64:{img_hash[:16]}" if qurl.startswith("data:image") else qurl
                        qr_hashes[key] = img_hash
                    except Exception as img_err:
                        key = "base64:error" if qurl.startswith("data:image") else qurl
                        qr_hashes[key] = f"DOWNLOAD_ERROR:{img_err}"

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
