# -*- coding: utf-8 -*-
"""比特浏览器本地 API 客户端"""
import requests


class BitBrowserClient:
    def __init__(self, host="http://127.0.0.1:54345"):
        self.host = host.rstrip("/")

    def _post(self, path, data=None):
        r = requests.post(self.host + path, json=data or {}, timeout=60)
        r.raise_for_status()
        res = r.json()
        if not res.get("success"):
            raise RuntimeError(f"比特浏览器接口错误: {res.get('msg')}")
        return res.get("data")

    def _get(self, path, params=None):
        r = requests.get(self.host + path, params=params or {}, timeout=60)
        r.raise_for_status()
        res = r.json()
        if not res.get("success"):
            raise RuntimeError(f"比特浏览器接口错误: {res.get('msg')}")
        return res.get("data")

    def ping(self):
        """检测连接"""
        self.browser_list(page=0, pageSize=1)
        return True

    def browser_list(self, page=0, pageSize=100, name=None, group=None):
        """获取窗口列表"""
        data = {"page": page, "pageSize": pageSize}
        if name:
            data["name"] = name
        if group:
            data["groupName"] = group
        return self._post("/browser/list", data) or {}

    def all_windows(self):
        """分页拉取全部窗口"""
        result, page = [], 0
        while True:
            data = self.browser_list(page=page, pageSize=100)
            lst = data.get("list", [])
            result.extend(lst)
            if len(lst) < 100 or page > 50:
                break
            page += 1
        return result

    def open_browser(self, window_id):
        """打开窗口，返回 {ws, http, driverPath} 等"""
        return self._post("/browser/open", {"id": window_id})

    def close_browser(self, window_id):
        try:
            self._post("/browser/close", {"id": window_id})
        except Exception:
            pass

    def group_list(self):
        return self._get("/group/list?page=0&pageSize=100") or {}


# -*- coding: utf-8 -*-
"""小红书自动发布核心逻辑（通过比特浏览器窗口 + Selenium 驱动）"""
import os
import time
import traceback

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

PUBLISH_URL = "https://creator.xiaohongshu.com/publish/publish?source=official"

# 页面元素选择器，页面改版时可在 config.json 中覆盖
DEFAULT_SELECTORS = {
    "video_tab": '//*[contains(text(),"上传视频")]',
    "graphic_tab": '//*[contains(text(),"上传图文")]',
    "file_input": 'input[type="file"]',
    "title": '//*[contains(@placeholder,"填写标题") or contains(@placeholder,"标题")]',
    "content": '//*[contains(@placeholder,"填写正文") or @contenteditable="true"]',
    "publish_btn": '//*[self::button or self::div][contains(@class,"btn") and (contains(text(),"发布") or contains(text(),"确定发布"))]',
    # 上传进度指示：上传中会出现“上传中/%”，完成后消失
    "uploading": '//*[contains(text(),"上传中") or contains(text(),"上传等待中")]',
}

VIDEO_EXTS = (".mp4", ".mov", ".avi", ".flv", ".mkv", ".webm")


def detect_type(media_files):
    """根据素材后缀判断发布类型: video / graphic"""
    return "video" if any(os.path.splitext(f)[1].lower() in VIDEO_EXTS
                          for f in media_files) else "graphic"


def wait_upload_done(driver, sel_map, timeout=600, poll=3):
    """轮询等待视频上传完成：'上传中'字样消失即认为完成"""
    waited = 0
    while waited < timeout:
        time.sleep(poll)
        waited += poll
        els = driver.find_elements(By.XPATH, sel_map.get(
            "uploading", DEFAULT_SELECTORS["uploading"]))
        if not els:
            return waited
    return waited


class PublishResult:
    def __init__(self, ok, msg=""):
        self.ok = ok
        self.msg = msg


def _build_driver(open_data, driver_path, timeout=90):
    """通过比特浏览器返回的 debuggerAddress 接管窗口"""
    ws = open_data.get("ws", "")
    http_addr = open_data.get("http", "")
    # http 形如 127.0.0.1:xxxxx
    address = http_addr if http_addr else ws.split("://")[-1]
    opts = Options()
    opts.add_experimental_option("debuggerAddress", address)
    svc = Service(executable_path=driver_path) if driver_path else Service()
    driver = webdriver.Chrome(service=svc, options=opts)
    driver.set_page_load_timeout(timeout)
    return driver


def _find(driver, sel, sel_map, timeout=10):
    xpath = sel_map.get(sel, DEFAULT_SELECTORS.get(sel, sel))
    return WebDriverWait(driver, timeout).until(
        EC.presence_of_element_located((By.XPATH, xpath))
    )


def publish_to_xhs(open_data, driver_path, media_files, title, content,
                   sel_map=None, log=print, wait_upload=60):
    """在指定窗口内发布一篇小红书笔记（图文/视频自动判断）"""
    sel_map = sel_map or {}
    driver = _build_driver(open_data, driver_path)
    try:
        log("  打开创作者中心…")
        driver.get(PUBLISH_URL)
        time.sleep(5)

        is_video = detect_type(media_files) == "video"
        log(f"  发布类型: {'视频' if is_video else '图文'}")

        # 选择上传入口
        try:
            if is_video:
                _find(driver, "video_tab", sel_map, 8).click()
            else:
                _find(driver, "graphic_tab", sel_map, 8).click()
        except Exception:
            log("  未找到上传Tab，尝试直接上传")
        time.sleep(1)

        # 上传文件
        file_input = _find(driver, "file_input", sel_map, 15)
        file_input.send_keys("\n".join(os.path.abspath(f) for f in media_files))
        log(f"  已提交 {len(media_files)} 个文件，等待上传…")

        if is_video:
            # 视频：轮询等待“上传中”字样消失，比固定等待更可靠
            used = wait_upload_done(driver, sel_map,
                                    timeout=max(wait_upload, 120) * 10)
            log(f"  视频上传完成（等待约 {used} 秒）")
            time.sleep(3)  # 等封面自动生成
        else:
            time.sleep(wait_upload)  # 图文：固定等待

        # 填写标题
        try:
            t = _find(driver, "title", sel_map, 10)
            t.clear()
            t.send_keys(title[:20])
        except Exception:
            log("  ⚠ 标题输入框未找到")

        # 填写正文
        try:
            c = _find(driver, "content", sel_map, 10)
            c.click()
            c.send_keys(content)
        except Exception:
            log("  ⚠ 正文输入框未找到")

        # 点击发布
        btn = _find(driver, "publish_btn", sel_map, 10)
        btn.click()
        time.sleep(5)
        log("  ✅ 已点击发布")
        return PublishResult(True, "发布成功")
    except Exception as e:
        log(f"  ❌ 失败: {e}")
        traceback.print_exc()
        return PublishResult(False, str(e))
    finally:
        try:
            driver.quit()
        except Exception:
            pass


# -*- coding: utf-8 -*-
"""
比特上号模块：解析账号数据 + IP数据，批量创建比特浏览器窗口（含代理）
参考格式：
  IP:    173.239.237.132:5776|user|pass   （每行一个）
  账号:  账号——密码——手机号——Token        （分隔符可自定义，字段可勾选）
"""
import re

SEP_CANDIDATES = ["——", "--", "|", "----", "\t"]


def guess_separator(line):
    for sep in SEP_CANDIDATES:
        if sep in line:
            return sep
    return "——"


def parse_ips(text):
    """解析IP行：ip:port|user|pass 或 ip:port"""
    ips = []
    for line in text.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("|")
        hostport = parts[0]
        host, _, port = hostport.partition(":")
        item = {"host": host.strip(), "port": port.strip() or "0",
                "user": parts[1].strip() if len(parts) > 1 else "",
                "pass": parts[2].strip() if len(parts) > 2 else ""}
        ips.append(item)
    return ips


def parse_accounts(text, fields):
    """
    解析账号行。
    fields: 勾选的字段顺序列表，如 ["账号","密码","手机号","Token"]
    分隔符按每行自动猜测（—— / -- / | / tab）
    返回: [ {字段: 值, ...} ]
    """
    accounts = []
    for line in text.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        sep = guess_separator(line)
        vals = [v.strip() for v in re.split(r"\s*" + re.escape(sep) + r"\s*", line)]
        acc = {}
        for i, fname in enumerate(fields):
            acc[fname] = vals[i] if i < len(vals) else ""
        accounts.append(acc)
    return accounts


def make_windows_payload(accounts, ips, name_prefix, group_id,
                         proxy_type="socks5", one_ip_per=1):
    """
    组装创建比特浏览器窗口的参数列表。
    one_ip_per: 每个IP关联的账号数（对应截图里的“单IP关联账号数”）
    """
    payloads = []
    for i, acc in enumerate(accounts):
        ip = ips[min(i // max(one_ip_per, 1), len(ips) - 1)] if ips else None
        username = acc.get("账号", "") or acc.get("用户名", "") or ""
        data = {
            "name": f"{name_prefix}{i + 1}",
            "remark": username,
            "groupId": group_id or "0",
            "browserFingerPrint": {"coreVersion": "124"},
        }
        if ip:
            data.update({
                "proxyMethod": 2,  # 自定义代理
                "proxyType": proxy_type,
                "host": ip["host"],
                "port": ip["port"],
                "proxyUserName": ip["user"],
                "proxyPassword": ip["pass"],
            })
        payloads.append(data)
    return payloads


def export_xlsx(accounts, ips, name_prefix, path):
    """导出账号预览 XLSX"""
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.title = "比特上号"
    headers = ["窗口名称", "账号", "密码", "手机号", "Token", "IP", "IP账号", "IP密码"]
    ws.append(headers)
    for i, acc in enumerate(accounts):
        ip = ips[i] if i < len(ips) else {}
        ws.append([
            f"{name_prefix}{i + 1}",
            acc.get("账号", ""), acc.get("密码", ""), acc.get("手机号", ""),
            acc.get("Token", ""), f"{ip.get('host','')}:{ip.get('port','')}",
            ip.get("user", ""), ip.get("pass", ""),
        ])
    wb.save(path)


# -*- coding: utf-8 -*-
"""
内容随机化引擎：降低多账号发布同内容的查重风险
- 标题: 随机 emoji 前后缀、随机分隔
- 正文: 同义词替换、随机 emoji 插入、段落顺序打乱
- 图片: 随机裁剪边缘 0~3%、亮度 ±3%、质量抖动 → 每个账号生成唯一副本
"""
import os
import random
import shutil
import subprocess

from PIL import Image

EMOJIS = ["✨", "🔥", "💫", "🌸", "🌈", "💕", "🌟", "🍃", "🎀", "🧸",
          "🍋", "☕", "🌙", "🪄", "💭", "🫧", "🌷", "🍓", "🐚", "🦋"]

# 同义词库，可按行业自行扩充（config.json 的 synonyms 会合并进来）
SYNONYMS = {
    "超级": ["超", "特别", "十分", "非常"],
    "喜欢": ["爱", "心动", "种草", "上头"],
    "推荐": ["安利", "分享", "种草", "提名"],
    "好看": ["好看", "好美", "绝美", "颜值超高"],
    "便宜": ["划算", "性价比高", "平价", "白菜价"],
    "好用": ["好用", "好用", "上手很快", "真香"],
    "教程": ["攻略", "指南", "干货", "笔记"],
    "今天": ["今天", "今日", "最近"],
    "非常": ["超", "特别", "相当", "真的很"],
    "不错": ["可以", "在线", "很好", "没得说"],
    "大家": ["姐妹们", "宝子们", "集美们", "家人们"],
    "真的": ["真的", "真滴", "实在是", "确实"],
}


def _sub_synonyms(text, extra=None):
    syn = dict(SYNONYMS)
    if extra:
        syn.update(extra)
    for word, alts in syn.items():
        if word in text and alts:
            text = text.replace(word, random.choice(alts))
    return text


def randomize_title(title, enable=True):
    """标题限20字内，随机加emoji前后缀"""
    if not enable or not title:
        return title
    t = title.strip()
    if random.random() < 0.7:
        t = random.choice(EMOJIS) + t
    if random.random() < 0.5 and len(t) < 18:
        t = t + random.choice(EMOJIS)
    return t[:20]


def randomize_content(content, enable=True, extra_synonyms=None):
    """正文随机化：同义词替换 + emoji 插入 + 段落打乱"""
    if not enable or not content:
        return content
    paras = [p for p in content.split("\n") if p.strip()]
    # 段落顺序轻度打乱（首段保留）
    if len(paras) > 2 and random.random() < 0.6:
        head, rest = paras[0], paras[1:]
        random.shuffle(rest)
        paras = [head] + rest
    out = []
    for p in paras:
        p = _sub_synonyms(p, extra_synonyms)
        # 随机在句尾插 emoji
        if random.random() < 0.5:
            p = p.rstrip() + random.choice(EMOJIS)
        out.append(p)
    # 随机结尾话题 emoji 行
    if random.random() < 0.4:
        out.append(random.choice(EMOJIS) * random.randint(2, 3))
    return "\n".join(out)


def prepare_media(media_files, out_dir, index, enable=True):
    """
    为一个账号生成素材的唯一副本。
    图片: 随机裁边+亮度微调+质量抖动 → 内容哈希不同
    视频: 原样复制（视频去重需 ffmpeg 转码，可选）
    返回新的文件路径列表
    """
    os.makedirs(out_dir, exist_ok=True)
    result = []
    for i, f in enumerate(media_files):
        ext = os.path.splitext(f)[1].lower()
        dst = os.path.join(out_dir, f"acc{index}_m{i}{ext}")
        if enable and ext in (".jpg", ".jpeg", ".png", ".webp"):
            try:
                img = Image.open(f).convert("RGB")
                w, h = img.size
                # 随机裁掉边缘 0~3%
                l = random.randint(0, int(w * 0.03))
                t = random.randint(0, int(h * 0.03))
                r = w - random.randint(0, int(w * 0.03))
                b = h - random.randint(0, int(h * 0.03))
                img = img.crop((l, t, r, b))
                # 亮度微调
                from PIL import ImageEnhance
                img = ImageEnhance.Brightness(img).enhance(
                    random.uniform(0.97, 1.03))
                img.save(dst, quality=random.randint(85, 95))
                result.append(dst)
                continue
            except Exception:
                pass  # 处理失败则原样复制
        if enable and ext in (".mp4", ".mov", ".avi", ".mkv", ".webm", ".flv"):
            dedup_video(f, dst)  # ffmpeg不可用时自动原样复制
            result.append(dst)
            continue
        shutil.copy(f, dst)
        result.append(dst)
    return result


def has_ffmpeg():
    try:
        subprocess.run(["ffmpeg", "-version"], capture_output=True, timeout=10)
        return True
    except Exception:
        return False


def dedup_video(src, dst):
    """
    视频唯一化：ffmpeg 轻度转码（随机微裁剪 + 亮度微调 + 随机元数据），
    每个账号生成内容不同的副本。需要本机装有 ffmpeg。
    """
    if not has_ffmpeg():
        shutil.copy(src, dst)
        return False
    crop = random.uniform(0.005, 0.02)   # 裁掉 0.5%~2% 边缘
    bright = random.uniform(-0.03, 0.03)
    vf_full = (f"crop=iw*{1 - crop * 2}:ih*{1 - crop * 2}:"
               f"iw*{crop}:ih*{crop},eq=brightness={bright:.4f}")
    vf_crop = f"crop=iw*{1 - crop * 2}:ih*{1 - crop * 2}:iw*{crop}:ih*{crop}"
    for vf in (vf_full, vf_crop, None):   # 多级降级：滤镜齐全→仅裁剪→纯转码→复制
        for codec, extra in (("libx264", ["-preset", "veryfast",
                                          "-crf", str(random.randint(22, 26))]),
                             ("mpeg4", ["-q:v", str(random.randint(4, 7))])):
            cmd = ["ffmpeg", "-y", "-i", src]
            if vf:
                cmd += ["-vf", vf]
            cmd += ["-metadata", f"comment=xhs{random.randint(0, 999999)}",
                    "-c:v", codec, *extra, "-c:a", "aac", "-b:a", "128k", dst]
            try:
                subprocess.run(cmd, capture_output=True, timeout=1800,
                               check=True)
                return True
            except Exception:
                continue
    shutil.copy(src, dst)
    return False


# -*- coding: utf-8 -*-
"""
小红书批量发布助手 · 网页版
运行: python app_web.py  然后浏览器打开 http://127.0.0.1:8000
"""
import glob
import json
import os
import threading
import time

from flask import Flask, Response, jsonify, render_template_string, request, send_file


CONFIG_FILE = "config.json"
STATUS = {0: "待发布", 1: "发布中", 2: "发布成功", 3: "失败", 4: "跳过"}
VERSION = "1.2.0"
REPO_RAW = "https://raw.githubusercontent.com/65ad/xhs-publisher/main/"
REPO_RAW_BACKUP = "https://cdn.jsdelivr.net/gh/65ad/xhs-publisher@main/"

app = Flask(__name__)
lock = threading.Lock()
state = {
    "config": {},
    "bit": None,
    "tasks": [],
    "logs": [],
    "running": False,
    "stop_flag": False,
    "parsed": {"accounts": [], "ips": []},
}


def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"bit_host": "http://127.0.0.1:54345", "driver_path": "",
            "wait_upload": 60, "interval": 15, "selectors": {},
            "randomize": True, "schedule": []}


def save_config():
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(state["config"], f, ensure_ascii=False, indent=2)


def log(msg):
    with lock:
        state["logs"].append(time.strftime("[%H:%M:%S] ") + str(msg))
        state["logs"] = state["logs"][-500:]


# ---------------- 发布线程 ----------------
def run_publish(tasks):
    state["running"], state["stop_flag"] = True, False
    cfg = state["config"]
    rnd = cfg.get("randomize", True)
    total = len(tasks)
    for i, t in enumerate(tasks, 1):
        if state["stop_flag"]:
            break
        log(f"({i}/{total}) 窗口[{t['window_name']}] 开始…")
        t["status"], t["msg"] = 1, "发布中"
        files = sorted(glob.glob(os.path.join(t.get("media_dir", ""), "*"))) if t.get("media_dir") else []
        files = [f for f in files if os.path.isfile(f)]
        if not files:
            t["status"], t["msg"] = 3, "素材为空"
            continue
        try:
            # 内容随机化 + 素材唯一副本
            if rnd:
                tmp_dir = os.path.join("/tmp" if os.name != "nt" else os.environ.get("TEMP", "C:/Temp"),
                                       "xhs_media", str(t["window_id"]))
                files = prepare_media(files, tmp_dir, abs(hash(t["window_id"])) % 100000, True)
            title = randomize_title(t.get("title", ""), rnd)
            content = randomize_content(t.get("content", ""), rnd,
                                        cfg.get("synonyms"))
            if rnd:
                log(f"  随机化后标题: {title}")
            open_data = state["bit"].open_browser(t["window_id"])
            time.sleep(3)
            res = publish_to_xhs(
                open_data, cfg.get("driver_path", ""), files,
                title, content,
                sel_map=cfg.get("selectors", {}), log=log,
                wait_upload=cfg.get("wait_upload", 60))
            t["status"], t["msg"] = (2 if res.ok else 3), res.msg
            state["bit"].close_browser(t["window_id"])
        except Exception as e:
            t["status"], t["msg"] = 3, str(e)
        # 随机抖动间隔（±40%），降低批量特征
        base = cfg.get("interval", 15)
        wait = int(base * random.uniform(0.6, 1.4))
        log(f"  等待 {wait} 秒后处理下一个账号")
        for _ in range(wait):
            if state["stop_flag"]:
                break
            time.sleep(1)
    state["running"] = False
    log("任务结束" if not state["stop_flag"] else "已停止")
    save_config()


# ---------------- 定时发布 ----------------
def scheduler_loop():
    """每分钟检查一次是否到达定时发布时间点"""
    while True:
        try:
            sched = state["config"].get("schedule", [])
            now = time.strftime("%H:%M")
            if sched and not state["running"] and not state["stop_flag"] \
                    and now in sched and state.get("_last_run_slot") != now:
                state["_last_run_slot"] = now
                pending = [t for t in state["tasks"] if t["status"] == 0]
                if pending:
                    log(f"⏰ 定时触发（{now}）：开始发布 {len(pending)} 个任务")
                    threading.Thread(target=run_publish, args=(pending,),
                                     daemon=True).start()
        except Exception as e:
            log(f"定时器异常: {e}")
        time.sleep(20)


threading.Thread(target=scheduler_loop, daemon=True).start()


# ---------------- API ----------------
@app.post("/api/connect")
def api_connect():
    data = request.json or {}
    state["config"]["bit_host"] = data.get("bit_host", state["config"]["bit_host"])
    state["config"]["driver_path"] = data.get("driver_path", state["config"].get("driver_path", ""))
    state["bit"] = BitBrowserClient(state["config"]["bit_host"])
    try:
        state["bit"].ping()
        save_config()
        return jsonify(ok=True)
    except Exception as e:
        return jsonify(ok=False, error=str(e))


@app.get("/api/windows")
def api_windows():
    group = request.args.get("group", "").strip()
    try:
        wins = state["bit"].all_windows() if state["bit"] else []
    except Exception as e:
        return jsonify(ok=False, error=str(e))
    wins = [w for w in wins if not group or w.get("groupName", "") == group]
    return jsonify(ok=True, windows=[{"id": w["id"], "name": w.get("name", ""),
                                      "group": w.get("groupName", "")} for w in wins])


@app.post("/api/windows/create")
def api_create_windows():
    """比特上号：批量创建窗口（含代理），多方案自动重试"""
    try:
        data = request.json or {}
        fields = data["fields"]
        accs = parse_accounts(data["accounts"], fields)
        ips = parse_ips(data["ips"])
        if not accs:
            return jsonify(ok=False, error="没有解析到账号数据")
        payloads = make_windows_payload(
            accs, ips, data.get("prefix") or "账号_",
            "", data.get("proxy_type", "socks5"), int(data.get("per_ip", 1)))
        ok = fail = 0
        last_err = ""
        for p in payloads:
            # 依次尝试多种参数组合，兼容不同比特浏览器版本
            variants = []
            base = dict(p)
            base.pop("browserFingerPrint", None)          # 方案1: 最简(不指定内核)
            variants.append(base)
            v2 = dict(base); v2["browserFingerPrint"] = {"coreVersion": "124"}
            variants.append(v2)                            # 方案2: 指定内核124
            v3 = dict(v2)
            if "proxyType" in v3:
                v3["proxyType"] = v3["proxyType"].upper()  # 方案3: SOCKS5 大写
            variants.append(v3)
            created = False
            for v in variants:
                try:
                    state["bit"]._post("/browser/update", v)
                    created = True
                    last_err = ""
                    break
                except Exception as e:
                    last_err = str(e)
            if created:
                ok += 1
            else:
                fail += 1
                log(f"创建窗口[{p['name']}]失败: {last_err}")
        log(f"比特上号: 已创建 {ok} 个窗口，失败 {fail} 个")
        if fail and not ok:
            log("💡 提示: 全部失败。常见原因: ①比特浏览器是网页版/海外版(需本地客户端) "
                "②比特浏览器版本太旧，请升级客户端 ③分组权限问题")
        return jsonify(ok=True, created=ok, failed=fail, last_error=last_err)
    except Exception as e:
        return jsonify(ok=False, error=str(e))


@app.post("/api/parse")
def api_parse():
    try:
        data = request.json or {}
        accs = parse_accounts(data["accounts"], data["fields"])
        ips = parse_ips(data["ips"])
        state["parsed"] = {"accounts": accs, "ips": ips}
        return jsonify(ok=True, count=len(accs), ip_count=len(ips),
                       sample=accs[:10])
    except Exception as e:
        return jsonify(ok=False, error=str(e))


@app.get("/api/export")
def api_export():
    accs, ips = state["parsed"]["accounts"], state["parsed"]["ips"]
    if not accs:
        return jsonify(error="请先解析账号数据"), 400
    path = "/tmp/比特上号.xlsx"
    export_xlsx(accs, ips, "账号_", path)
    return send_file(path, as_attachment=True, download_name="比特上号.xlsx")


@app.get("/api/tasks")
def api_tasks():
    ts = state["tasks"]
    return jsonify(ok=True, tasks=ts,
                   stat={"total": len(ts),
                         "ok": sum(1 for t in ts if t["status"] == 2),
                         "fail": sum(1 for t in ts if t["status"] == 3),
                         "running": state["running"]},
                   logs=state["logs"][-80:])


@app.post("/api/tasks/set")
def api_tasks_set():
    """从选中的窗口建立任务，应用全局素材/文案"""
    data = request.json or {}
    wins = data.get("windows", [])
    for w in wins:
        exist = next((t for t in state["tasks"] if t["window_id"] == w["id"]), None)
        if exist:
            exist.update({"window_name": w["name"], "group": w["group"]})
        else:
            state["tasks"].append({
                "window_id": w["id"], "window_name": w["name"], "group": w["group"],
                "media_dir": data.get("media_dir", ""), "title": data.get("title", ""),
                "content": data.get("content", ""), "status": 0, "msg": ""})
    save_config()
    return jsonify(ok=True)


@app.post("/api/task/edit")
def api_task_edit():
    data = request.json or {}
    for t in state["tasks"]:
        if t["window_id"] == data["window_id"]:
            t["media_dir"] = data.get("media_dir", t["media_dir"])
            t["title"] = data.get("title", t["title"])
            t["content"] = data.get("content", t["content"])
    save_config()
    return jsonify(ok=True)


@app.post("/api/start")
def api_start():
    if state["running"]:
        return jsonify(ok=False, error="正在发布中")
    mode = (request.json or {}).get("mode", "all")
    if mode == "sample":
        n = int((request.json or {}).get("sample_count", 2))
        pending = [t for t in state["tasks"] if t["status"] == 0][:n]
    else:
        pending = [t for t in state["tasks"] if t["status"] == 0]
    if not pending:
        return jsonify(ok=False, error="没有待发布任务")
    threading.Thread(target=run_publish, args=(pending,), daemon=True).start()
    return jsonify(ok=True, count=len(pending))


@app.post("/api/stop")
def api_stop():
    state["stop_flag"] = True
    return jsonify(ok=True)


@app.post("/api/reset")
def api_reset():
    for t in state["tasks"]:
        if t["status"] in (3, 4):
            t["status"], t["msg"] = 0, ""
    return jsonify(ok=True)


@app.post("/api/config")
def api_config():
    data = request.json or {}
    state["config"].update({k: data[k] for k in
                            ("wait_upload", "interval", "randomize",
                             "schedule", "synonyms") if k in data})
    save_config()
    return jsonify(ok=True)


# ---------------- 在线更新 ----------------
@app.get("/api/update/check")
def api_update_check():
    import requests as _rq
    for base in (REPO_RAW, REPO_RAW_BACKUP):
        try:
            info = _rq.get(base + "version.json", timeout=15).json()
            remote = info.get("version", "")
            def vt(v):
                try: return tuple(int(x) for x in v.split("."))
                except Exception: return (0, 0, 0)
            if remote and vt(remote) > vt(VERSION):
                return jsonify(ok=True, has_update=True,
                               current=VERSION, latest=remote,
                               notes=info.get("notes", ""))
            return jsonify(ok=True, has_update=False, current=VERSION,
                           latest=remote)
        except Exception:
            continue
    return jsonify(ok=False, error="无法连接更新服务器（检查网络）")


@app.post("/api/update/do")
def api_update_do():
    import requests as _rq
    self_path = os.path.abspath(__file__)
    tmp_path = self_path + ".new"
    try:
        content = None
        for base in (REPO_RAW, REPO_RAW_BACKUP):
            try:
                r = _rq.get(base + "软件.py", timeout=30)
                if r.ok and len(r.content) > 10000:
                    content = r.content
                    break
            except Exception:
                continue
        if not content:
            return jsonify(ok=False, error="下载新版本失败，请检查网络")
        with open(tmp_path, "wb") as f:
            f.write(content)
        os.replace(tmp_path, self_path)   # 覆盖自己
        return jsonify(ok=True, msg="已更新完成！请关闭本软件并重新启动。")
    except Exception as e:
        try:
            os.remove(tmp_path)
        except Exception:
            pass
        return jsonify(ok=False, error=str(e))


# ---------------- 页面 ----------------
PAGE = """<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">
<title>小红书批量发布助手 · 比特浏览器版</title>
<style>
*{box-sizing:border-box} body{font-family:Microsoft YaHei,sans-serif;background:#1b1f27;color:#e6e6e6;margin:0;padding:16px}
h2{color:#ff4d6d;margin:18px 0 8px;font-size:17px} .card{background:#242a35;border-radius:10px;padding:14px;margin-bottom:14px}
input,textarea,select{background:#1b1f27;color:#eee;border:1px solid #3a4356;border-radius:6px;padding:6px 8px}
input[type=text],input[type=number]{width:200px} textarea{width:100%;height:110px;font-family:monospace}
button{background:#ff4d6d;color:#fff;border:0;border-radius:6px;padding:7px 16px;cursor:pointer;margin-right:8px}
button.gray{background:#3a4356} button.green{background:#2e9e5b}
.row{display:flex;gap:14px;flex-wrap:wrap;align-items:center} .col{flex:1;min-width:280px}
table{width:100%;border-collapse:collapse;font-size:13px} th,td{border-bottom:1px solid #313a4a;padding:6px 8px;text-align:left}
th{color:#9fb0c9} .st0{color:#9fb0c9}.st1{color:#ffd166}.st2{color:#4cd97b}.st3{color:#ff5c5c}
#log{background:#141821;height:170px;overflow-y:auto;padding:10px;border-radius:8px;font-family:monospace;font-size:12px;white-space:pre-wrap}
label{margin-right:8px;font-size:13px} .stat span{margin-right:18px}
</style></head><body>
<h2 style="font-size:20px">🔴 小红书批量发布助手（比特浏览器版 · 网页）</h2>

<div class="card"><h2>① 连接比特浏览器</h2>
<div class="row">
  API地址: <input type="text" id="host" style="width:230px" value="http://127.0.0.1:54345">
  chromedriver路径(可留空): <input type="text" id="driver" style="width:220px">
  <button onclick="connect()">测试连接</button>
  <button class="gray" onclick="loadWindows()">拉取窗口列表</button>
  分组筛选: <input type="text" id="group" style="width:110px">
  <span id="connmsg"></span>
  <button class="gray" onclick="checkUpdate()">检查更新</button> <span id="updmsg"></span>
</div></div>

<div class="card"><h2>② 比特上号（解析账号+IP，批量创建窗口）</h2>
<div class="row"><div class="col"><b>IP数据(每行一个 ip:port|账号|密码)</b>
<textarea id="ips" placeholder="173.239.237.132:5776|user|pass"></textarea></div>
<div class="col"><b>账号数据(每行一个，按勾选字段顺序)</b>
<textarea id="accs" placeholder="账号——密码——手机号——Token"></textarea></div></div>
<div class="row" style="margin-top:8px">
 字段顺序:
<label><input type="checkbox" value="账号" class="fld" checked>账号</label>
<label><input type="checkbox" value="密码" class="fld" checked>密码</label>
<label><input type="checkbox" value="手机号" class="fld">手机号</label>
<label><input type="checkbox" value="Token" class="fld" checked>Token</label>
<label><input type="checkbox" value="CK" class="fld">CK</label>
窗口前缀: <input type="text" id="prefix" value="小红书_" style="width:90px">
代理类型: <select id="ptype"><option>socks5</option><option>http</option><option>noproxy</option></select>
单IP关联账号数: <input type="number" id="perip" value="1" style="width:50px">
<button onclick="doParse()">预览解析</button>
<button onclick="createWindows()">导入比特(创建窗口)</button>
<button class="gray" onclick="location='/api/export'">导出XLSX</button>
<span id="parsemsg"></span>
</div></div>

<div class="card"><h2>③ 素材与内容（新建任务时应用）</h2>
<div class="row">
素材文件夹: <input type="text" id="media" style="width:330px" placeholder="D:/xhs/素材1">
标题: <input type="text" id="title" style="width:260px">
正文: <input type="text" id="content" style="width:260px">
上传等待(秒): <input type="number" id="wait" value="60" style="width:60px">
账号间隔(秒): <input type="number" id="intv" value="15" style="width:60px">
<label><input type="checkbox" id="rnd" checked onchange="saveCfg()">内容随机化(防查重)</label>
定时发布(HH:MM逗号分隔,留空关闭): <input type="text" id="sched" placeholder="06:00,08:00" style="width:130px">
<button class="green" onclick="addTasks()">把选中窗口加为任务</button>
<button onclick="doStart('sample')">选样试发(前2个)</button>
</div></div>

<div class="card"><h2>④ 任务列表</h2>
<div class="row stat" style="margin-bottom:8px">
<button class="green" onclick="doStart()">▶ 开始发布</button>
<button onclick="doStop()">■ 停止</button>
<button class="gray" onclick="doReset()">↻ 重置失败</button>
<span>总数 <b id="stt">0</b> | 成功 <b id="sto" style="color:#4cd97b">0</b> | 失败 <b id="stf" style="color:#ff5c5c">0</b> <span id="runmsg"></span></span></div>
<table><thead><tr><th>窗口ID</th><th>窗口名称</th><th>分组</th><th>素材目录</th><th>状态</th><th>备注</th><th>操作</th></tr></thead>
<tbody id="tbody"></tbody></table>
<p style="font-size:12px;color:#9fb0c9">双击"素材目录/标题/正文"单元格可单独修改该账号任务，回车保存。</p></div>

<div class="card"><h2>⑤ 运行日志</h2><div id="log"></div></div>

<script>
let windows=[], sel=new Set();
async function post(u,d){const r=await fetch(u,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(d||{})});return r.json()}
async function connect(){const r=await post('/api/connect',{bit_host:host.value,driver_path:driver.value});
 connmsg.textContent=r.ok?'✅ 连接成功':'❌ '+r.error; connmsg.style.color=r.ok?'#4cd97b':'#ff5c5c'}
async function loadWindows(){const g=group.value;const r=await fetch('/api/windows?group='+encodeURIComponent(g)).then(x=>x.json());
 if(!r.ok){alert(r.error);return} windows=r.windows; renderWindows();
 if(!r.windows.length){alert('未拉取到窗口，请确认已连接并填对分组')}}
function renderWindows(){
 const c=document.querySelector('#tbw'); if(c)c.remove();
 if(!windows.length)return;
 let h='<div style="margin:10px 0"><b>勾选要建任务的窗口：</b> <button class="gray" onclick="selAll(1)">全选</button><button class="gray" onclick="selAll(0)">全不选</button> <table><tr><th></th><th>窗口名称</th><th>分组</th><th>ID</th></tr>';
 windows.forEach((w,i)=>{h+=`<tr><td><input type=checkbox onchange="sel.has(${i})?sel.delete(${i}):sel.add(${i})" ${sel.has(i)?'checked':''}></td><td>${w.name}</td><td>${w.group}</td><td>${w.id}</td></tr>`});
 const d=document.createElement('div'); d.id='tbw'; d.innerHTML=h+'</table></div>';
 document.querySelectorAll('.card')[0].after(d);}
function selAll(v){sel.clear(); if(v)windows.forEach((_,i)=>sel.add(i)); renderWindows()}
async function doParse(){const r=await post('/api/parse',{accounts:accs.value,ips:ips.value,fields:[...document.querySelectorAll('.fld:checked')].map(x=>x.value)});
 parsemsg.textContent=r.ok?`✅ 解析 ${r.count} 个账号 / ${r.ip_count} 个IP`:'❌ '+r.error;
 parsemsg.style.color=r.ok?'#4cd97b':'#ff5c5c';
 if(r.ok&&r.sample)parsemsg.title=JSON.stringify(r.sample,null,1)}
async function createWindows(){const r=await post('/api/windows/create',{accounts:accs.value,ips:ips.value,
 fields:[...document.querySelectorAll('.fld:checked')].map(x=>x.value),prefix:prefix.value,
 proxy_type:ptype.value,per_ip:perip.value});
 alert(r.ok?`已创建 ${r.created} 个窗口，失败 ${r.failed} 个`:'失败: '+r.error)}
async function addTasks(){if(!sel.size){alert('请先在上方勾选窗口');return}
 const r=await post('/api/tasks/set',{windows:[...sel].map(i=>windows[i]),media_dir:media.value,title:title.value,content:content.value});
 if(r.ok)refresh()}
async function doStart(mode){const r=await post('/api/start',{mode:mode||'all',sample_count:2});if(!r.ok)alert(r.error);else alert(mode==='sample'?`已开始试发 ${r.count} 个账号，成功后再点“开始发布”跑全量`:`已开始发布 ${r.count} 个任务`)}
async function saveCfg(){await post('/api/config',{randomize:rnd.checked,schedule:sched.value.split(',').map(s=>s.trim()).filter(Boolean)})}
sched.addEventListener('change',saveCfg);
async function doStop(){await post('/api/stop')}
async function checkUpdate(){updmsg.textContent='检查中…';
 const c=await fetch('/api/update/check').then(x=>x.json());
 if(!c.ok){updmsg.textContent='❌ '+c.error;return}
 if(!c.has_update){updmsg.textContent=`✅ 已是最新版 v${c.current}`;return}
 if(!confirm(`发现新版本 v${c.latest}（当前 v${c.current}）\\n${c.notes||''}\\n现在更新吗？`))return;
 updmsg.textContent='下载更新中…';
 const u=await post('/api/update/do');
 updmsg.textContent=u.ok?'✅ '+u.msg:'❌ '+u.error;
 if(u.ok)setTimeout(()=>location.reload(),2000)}
async function doReset(){await post('/api/reset');refresh()}
async function editTask(id,field,el){const d={};d.window_id=id;d[field]=el.value;
 if(field==='content')d.title=undefined; await post('/api/task/edit',d)}
function editCell(t,field){const id=t.window_id;
 const v=prompt('修改'+field+'（账号窗口: '+t.window_name+'）',t[field]||'');
 if(v===null)return; const d={window_id:id}; d[field]=v; post('/api/task/edit',d).then(refresh)}
async function refresh(){const r=await fetch('/api/tasks').then(x=>x.json());
 stt.textContent=r.stat.total;sto.textContent=r.stat.ok;stf.textContent=r.stat.fail;
 runmsg.textContent=r.stat.running?'（发布中…）':'';
 document.querySelector('#wait').value&&(body=0);
 tbody.innerHTML=r.tasks.map(t=>`<tr>
  <td>${t.window_id}</td><td>${t.window_name}</td><td>${t.group||''}</td>
  <td style="cursor:pointer;color:#7fb4ff" onclick="editCell(t_${t.window_id.replace(/[^0-9a-zA-Z]/g,'')},'media_dir')">${t.media_dir||'(点此设置)'}</td>
  <td class="st${t.status}">${({0:'待发布',1:'发布中',2:'发布成功',3:'失败',4:'跳过'})[t.status]}</td>
  <td>${t.msg||''}</td>
  <td style="color:#7fb4ff;cursor:pointer" onclick='editRow(${JSON.stringify(t)})'>编辑</td></tr>`).join('');
 log.textContent=r.logs.join('\\n'); log.scrollTop=log.scrollHeight;
 // 保存行对象引用
 r.tasks.forEach(t=>{window['t_'+t.window_id.replace(/[^0-9a-zA-Z]/g,'')]=t})}
function editRow(t){const m=prompt('素材目录:',t.media_dir||'');if(m===null)return;
 const ti=prompt('标题:',t.title||'');if(ti===null)return;
 const c=prompt('正文:',t.content||'');if(c===null)return;
 post('/api/task/edit',{window_id:t.window_id,media_dir:m,title:ti,content:c}).then(refresh)}
refresh(); setInterval(refresh,2000);
</script></body></html>"""


@app.get("/")
def index():
    cfg = state["config"]
    return render_template_string(PAGE)


if __name__ == "__main__":
    state["config"] = load_config()
    print("=" * 50)
    print("小红书批量发布助手 已启动")
    print("请用浏览器打开:  http://127.0.0.1:8000")
    print("停止软件: 关闭本窗口 或 按 Ctrl+C")
    print("=" * 50)
    try:
        app.run(host="127.0.0.1", port=8000, debug=False)
    except OSError as e:
        print(f"\n[启动失败] {e}")
        print("最常见原因: 8000 端口被占用（上次没关干净）。")
        print("解决办法: 重启电脑，或把上面 app.run 里的 8000 改成 8001 再试。")
        input("按回车键退出...")
