# -*- coding: utf-8 -*-
from __future__ import annotations

import datetime as _dt
import os
import re
import subprocess
import threading
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, quote, urlparse

import tkinter as tk
from tkinter import filedialog, messagebox, ttk


APP_TITLE = "네이버 블로그 댓글 캡처"
APP_DIR = Path(__file__).resolve().parent
DEFAULT_SAVE_DIR = Path.home() / "Downloads" / "naver-comment-captures"
LOG_PATH = Path(__file__).with_name("naver_comment_capture.log")
CHROME_EXE = Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe")
CHROME_USER_DATA_DIR = APP_DIR / "chrome-profile"
LOGIN_URL = "https://nid.naver.com/nidlogin.login"
CDP_PORT = 9222
CDP_URL = f"http://127.0.0.1:{CDP_PORT}"

COMMENT_CONTAINER_SELECTORS = [
    ".u_cbox_comment_box",
    "li.u_cbox_comment",
    ".u_cbox_comment",
    ".u_cbox_area",
]
COMMENT_READY_SELECTORS = [
    ".u_cbox",
    ".commentbox_header",
    *COMMENT_CONTAINER_SELECTORS,
]


class CommentCaptureError(Exception):
    pass


@dataclass
class BlogPost:
    blog_id: str
    log_no: str

    @property
    def mobile_url(self) -> str:
        return f"https://m.blog.naver.com/{quote(self.blog_id)}/{self.log_no}"

    @property
    def mobile_comment_url(self) -> str:
        return (
            "https://m.blog.naver.com/PostView.naver"
            f"?blogId={quote(self.blog_id)}&logNo={self.log_no}&modal=comment"
        )

    @property
    def desktop_postview_url(self) -> str:
        return (
            "https://blog.naver.com/PostView.naver"
            f"?blogId={quote(self.blog_id)}&logNo={self.log_no}"
        )


@dataclass
class CommentEntry:
    frame_index: int
    selector: str
    element_index: int
    handle: Any
    nickname: str
    date: str
    content: str
    text: str
    secret: bool
    match_mode: str = ""

    def to_summary(self, one_based_index: int) -> dict[str, Any]:
        preview = self.content or self.text
        preview = compact_text(preview)
        if len(preview) > 90:
            preview = preview[:87] + "..."
        return {
            "index": one_based_index,
            "nickname": self.nickname,
            "date": self.date,
            "preview": preview,
            "secret": self.secret,
            "match_mode": self.match_mode,
        }


@dataclass
class CaptureResult:
    status: str
    saved_path: Path | None = None
    candidates: list[dict[str, Any]] | None = None
    match_mode: str | None = None
    visible_nicknames: list[str] | None = None


def compact_text(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def normalize_text(value: str) -> str:
    return re.sub(r"\s+", "", value or "").casefold()


def canonical_nickname(value: str) -> str:
    value = re.sub(r"\([^)]*\)", "", value or "")
    value = value.replace("작성자", "").replace("블로그 주인", "").replace("블로그주인", "")
    return normalize_text(value)


def nickname_exact_match(actual: str, target: str) -> bool:
    target_norm = normalize_text(target)
    return normalize_text(actual) == target_norm or canonical_nickname(actual) == target_norm


def nickname_partial_match(actual: str, target: str) -> bool:
    target_norm = normalize_text(target)
    return bool(target_norm) and target_norm in normalize_text(actual)


def sanitize_filename(value: str) -> str:
    value = compact_text(value)
    value = re.sub(r'[\\/:*?"<>|]+', "_", value)
    value = value.strip(" .")
    return value[:80] or "nickname"


def write_log(message: str) -> None:
    timestamp = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        with LOG_PATH.open("a", encoding="utf-8") as log_file:
            log_file.write(f"[{timestamp}] {message}\n")
    except Exception:
        pass


def parse_blog_post(raw_url: str) -> BlogPost:
    url = compact_text(raw_url)
    if not url:
        raise CommentCaptureError("블로그 글 링크를 입력해 주세요.")
    if "://" not in url:
        url = "https://" + url

    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    blog_id = first_query_value(query, "blogId")
    log_no = first_query_value(query, "logNo")
    if blog_id and log_no:
        return BlogPost(blog_id=blog_id, log_no=log_no)

    parts = [part for part in parsed.path.strip("/").split("/") if part]
    for index, part in enumerate(parts):
        if re.fullmatch(r"\d{8,}", part) and index > 0:
            return BlogPost(blog_id=parts[index - 1], log_no=part)

    raise CommentCaptureError(
        "링크에서 blogId와 글 번호(logNo)를 찾지 못했습니다. "
        "예: https://m.blog.naver.com/shuchel/224296188790"
    )


def first_query_value(query: dict[str, list[str]], key: str) -> str:
    values = query.get(key) or []
    return values[0].strip() if values else ""


def is_automation_chrome_running() -> bool:
    try:
        completed = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                (
                    "Get-CimInstance Win32_Process -Filter \"name='chrome.exe'\" | "
                    "Select-Object -ExpandProperty CommandLine"
                ),
            ],
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
    except Exception:
        return False
    expected = str(CHROME_USER_DATA_DIR).casefold()
    return expected in (completed.stdout or "").casefold()


def ensure_local_environment() -> None:
    ensure_chrome_exists()
    CHROME_USER_DATA_DIR.mkdir(parents=True, exist_ok=True)


def open_login_profile() -> None:
    ensure_chrome_exists()
    CHROME_USER_DATA_DIR.mkdir(parents=True, exist_ok=True)
    if is_automation_chrome_running():
        raise CommentCaptureError(
            "로그인/캡처용 Chrome 창이 이미 열려 있습니다. 그 창에서 로그인하거나, 닫은 뒤 다시 눌러 주세요."
        )
    subprocess.Popen(
        [
            str(CHROME_EXE),
            f"--user-data-dir={CHROME_USER_DATA_DIR}",
            f"--remote-debugging-port={CDP_PORT}",
            "--remote-debugging-address=127.0.0.1",
            "--no-first-run",
            "--disable-session-crashed-bubble",
            "--new-window",
            LOGIN_URL,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def ensure_chrome_exists() -> None:
    if not CHROME_EXE.exists():
        raise CommentCaptureError(f"Chrome 실행 파일을 찾지 못했습니다: {CHROME_EXE}")


def run_capture(
    raw_url: str,
    nickname: str,
    output_dir: Path,
    selected_index: int | None = None,
    forced_match_mode: str | None = None,
    progress: Callable[[str], None] | None = None,
) -> CaptureResult:
    def report(message: str) -> None:
        write_log(message)
        if progress:
            progress(message)

    post = parse_blog_post(raw_url)
    nickname = compact_text(nickname)
    if not nickname:
        raise CommentCaptureError("찾을 닉네임을 입력해 주세요.")

    report("환경 확인 중...")
    ensure_local_environment()
    output_dir.mkdir(parents=True, exist_ok=True)

    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise CommentCaptureError(
            "Playwright가 설치되어 있지 않습니다. run_naver_comment_capture.bat으로 실행하면 자동 설치됩니다."
        ) from exc

    context = None
    browser = None
    close_context_when_done = False
    with sync_playwright() as playwright:
        try:
            if is_automation_chrome_running():
                report("열려 있는 로그인 Chrome에 연결하는 중...")
                try:
                    browser = playwright.chromium.connect_over_cdp(CDP_URL, timeout=10_000)
                    context = browser.contexts[0] if browser.contexts else browser.new_context()
                except Exception as exc:
                    raise CommentCaptureError(
                        "열려 있는 로그인용 Chrome에 연결하지 못했습니다. "
                        "이전 버전으로 열린 Chrome 창일 수 있으니 닫고, '네이버 로그인 열기'를 다시 눌러 로그인해 주세요."
                    ) from exc
            else:
                report("Chrome 프로필을 여는 중...")
                context = playwright.chromium.launch_persistent_context(
                    user_data_dir=str(CHROME_USER_DATA_DIR),
                    executable_path=str(CHROME_EXE),
                    headless=False,
                    viewport={"width": 390, "height": 844},
                    locale="ko-KR",
                    timezone_id="Asia/Seoul",
                    timeout=30_000,
                    args=[
                        f"--remote-debugging-port={CDP_PORT}",
                        "--remote-debugging-address=127.0.0.1",
                        "--no-first-run",
                        "--disable-session-crashed-bubble",
                        "--disable-blink-features=AutomationControlled",
                    ],
                )
                close_context_when_done = True
        except Exception as exc:
            if isinstance(exc, CommentCaptureError):
                raise
            raise CommentCaptureError(
                "앱 전용 Chrome 프로필을 열지 못했습니다. 열린 로그인/캡처용 Chrome 창을 닫고 다시 실행해 주세요."
            ) from exc

        try:
            report("첫 번째 Chrome 탭을 준비하는 중...")
            page = context.pages[0] if context.pages else context.new_page()
            page.set_default_timeout(10_000)
            open_comments(page, post, PlaywrightTimeoutError, report)
            report("댓글 후보를 분석하는 중...")
            entries, match_mode = find_matching_comments(page, nickname, forced_match_mode)

            if not entries:
                visible_nicknames = collect_visible_nicknames(page)
                raise CommentCaptureError(build_not_found_message(nickname, visible_nicknames))

            if selected_index is None and len(entries) > 1:
                return CaptureResult(
                    status="multiple",
                    candidates=[
                        entry.to_summary(index + 1)
                        for index, entry in enumerate(entries)
                    ],
                    match_mode=match_mode,
                    visible_nicknames=collect_visible_nicknames(page),
                )

            target_index = selected_index if selected_index is not None else 0
            if target_index < 0 or target_index >= len(entries):
                raise CommentCaptureError("선택한 댓글 번호가 현재 후보 목록 범위를 벗어났습니다. 다시 검색해 주세요.")

            entry = entries[target_index]
            output_path = make_output_path(output_dir, post, nickname)
            capture_entry(page, entry, output_path)
            return CaptureResult(status="captured", saved_path=output_path, match_mode=match_mode)
        finally:
            if close_context_when_done and context is not None:
                context.close()


def open_comments(
    page: Any,
    post: BlogPost,
    timeout_error_type: type[Exception],
    report: Callable[[str], None] | None = None,
) -> None:
    def tell(message: str) -> None:
        if report:
            report(message)

    tell("블로그 글을 여는 중...")
    goto_page(page, post.mobile_url, timeout_error_type)
    tell("댓글 버튼을 누르는 중...")
    try_click_comment_button(page)
    if wait_for_comment_area(page):
        return

    tell("댓글 모달 주소로 다시 여는 중...")
    goto_page(page, post.mobile_comment_url, timeout_error_type)
    if wait_for_comment_area(page):
        return

    tell("PC 댓글 영역으로 다시 시도하는 중...")
    goto_page(page, post.desktop_postview_url, timeout_error_type)
    try_click_comment_button(page)
    wait_for_comment_area(page)


def goto_page(page: Any, url: str, timeout_error_type: type[Exception]) -> None:
    page.goto(url, wait_until="domcontentloaded", timeout=45_000)
    try:
        page.wait_for_load_state("networkidle", timeout=8_000)
    except timeout_error_type:
        pass
    page.wait_for_timeout(1_200)


def try_click_comment_button(page: Any) -> bool:
    selectors = [
        "a.btn_comment",
        "button.btn_comment",
        "a._cmtList",
        "a._floating_bottom_btn_comment",
        "a:has-text('댓글')",
        "button:has-text('댓글')",
    ]
    for selector in selectors:
        try:
            locator = page.locator(selector)
            count = min(locator.count(), 8)
        except Exception:
            continue
        for index in range(count):
            item = locator.nth(index)
            try:
                if not item.is_visible(timeout=700):
                    continue
                item.scroll_into_view_if_needed(timeout=2_000)
                item.click(timeout=4_000)
                page.wait_for_timeout(2_000)
                return True
            except Exception:
                continue
    return False


def wait_for_comment_area(page: Any, timeout_ms: int = 9_000) -> bool:
    deadline = _dt.datetime.now() + _dt.timedelta(milliseconds=timeout_ms)
    while _dt.datetime.now() < deadline:
        for frame in page.frames:
            for selector in COMMENT_READY_SELECTORS:
                try:
                    if frame.locator(selector).count() > 0:
                        return True
                except Exception:
                    continue
        page.wait_for_timeout(500)
    return False


def find_matching_comments(
    page: Any,
    target_nickname: str,
    forced_match_mode: str | None = None,
) -> tuple[list[CommentEntry], str]:
    entries = collect_comment_entries(page)
    if forced_match_mode == "exact":
        matched = [entry for entry in entries if nickname_exact_match(entry.nickname, target_nickname)]
        for entry in matched:
            entry.match_mode = "exact"
        return matched, "exact"
    if forced_match_mode == "partial":
        matched = [entry for entry in entries if nickname_partial_match(entry.nickname, target_nickname)]
        for entry in matched:
            entry.match_mode = "partial"
        return matched, "partial"

    exact = [entry for entry in entries if nickname_exact_match(entry.nickname, target_nickname)]
    if exact:
        for entry in exact:
            entry.match_mode = "exact"
        return exact, "exact"

    partial = [entry for entry in entries if nickname_partial_match(entry.nickname, target_nickname)]
    for entry in partial:
        entry.match_mode = "partial"
    return partial, "partial"


def collect_comment_entries(page: Any) -> list[CommentEntry]:
    entries: list[CommentEntry] = []
    for frame_index, frame in enumerate(page.frames):
        for selector in COMMENT_CONTAINER_SELECTORS:
            try:
                handles = frame.query_selector_all(selector)
            except Exception:
                continue
            visible_handles = []
            for element_index, handle in enumerate(handles):
                if element_is_visible(handle):
                    visible_handles.append((element_index, handle))
            if not visible_handles:
                continue

            for element_index, handle in visible_handles:
                summary = extract_comment_summary(handle)
                if not summary:
                    continue
                entries.append(
                    CommentEntry(
                        frame_index=frame_index,
                        selector=selector,
                        element_index=element_index,
                        handle=handle,
                        nickname=summary["nickname"],
                        date=summary["date"],
                        content=summary["content"],
                        text=summary["text"],
                        secret=summary["secret"],
                    )
                )
            break
    return entries


def element_is_visible(handle: Any) -> bool:
    try:
        box = handle.bounding_box()
    except Exception:
        return False
    return bool(box and box.get("width", 0) >= 5 and box.get("height", 0) >= 5)


def extract_comment_summary(handle: Any) -> dict[str, Any] | None:
    try:
        summary = handle.evaluate(
            """
            (el) => {
              const clean = (value) => (value || '').replace(/\\s+/g, ' ').trim();
              const pick = (selectors) => {
                for (const selector of selectors) {
                  const node = el.querySelector(selector);
                  const text = clean(node && (node.innerText || node.textContent));
                  if (text) return text;
                }
                return '';
              };
              const nickname = pick([
                '.u_cbox_name',
                '.u_cbox_nick',
                '.u_cbox_nickname',
                '.u_cbox_name_area',
                'a[class*="name"]',
                'span[class*="name"]'
              ]);
              const date = pick([
                '.u_cbox_date',
                '.u_cbox_info_base .u_cbox_date',
                'span[class*="date"]'
              ]);
              const content = pick([
                '.u_cbox_contents',
                '.u_cbox_text_wrap',
                '.u_cbox_text',
                '.u_cbox_comment_content',
                '[class*="contents"]',
                '[class*="text_wrap"]'
              ]);
              const allText = clean(el.innerText || el.textContent);
              return {
                nickname,
                date,
                content,
                text: allText,
                secret: Boolean(el.querySelector('.u_cbox_ico_stat_secret, [class*="secret"]'))
              };
            }
            """
        )
    except Exception:
        return None

    if not summary:
        return None
    summary["nickname"] = compact_text(summary.get("nickname", ""))
    summary["date"] = compact_text(summary.get("date", ""))
    summary["content"] = compact_text(summary.get("content", ""))
    summary["text"] = compact_text(summary.get("text", ""))
    summary["secret"] = bool(summary.get("secret", False))
    if not summary["nickname"] and not summary["text"]:
        return None
    return summary


def collect_visible_nicknames(page: Any, limit: int = 30) -> list[str]:
    nicknames: list[str] = []
    seen: set[str] = set()
    for entry in collect_comment_entries(page):
        nickname = compact_text(entry.nickname)
        key = normalize_text(nickname)
        if not nickname or key in seen:
            continue
        seen.add(key)
        nicknames.append(nickname)
        if len(nicknames) >= limit:
            break
    return nicknames


def build_not_found_message(nickname: str, visible_nicknames: list[str]) -> str:
    if visible_nicknames:
        shown = ", ".join(visible_nicknames[:15])
        return f"'{nickname}' 닉네임의 댓글을 찾지 못했습니다. 현재 보이는 닉네임: {shown}"
    return (
        f"'{nickname}' 닉네임의 댓글을 찾지 못했습니다. "
        "댓글창이 열리지 않았거나, 현재 로그인 계정에서 해당 댓글이 보이지 않을 수 있습니다."
    )


def make_output_path(output_dir: Path, post: BlogPost, nickname: str) -> Path:
    timestamp = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{sanitize_filename(post.blog_id)}_{post.log_no}_{sanitize_filename(nickname)}_{timestamp}.png"
    path = output_dir / filename
    counter = 2
    while path.exists():
        path = output_dir / f"{path.stem}_{counter}{path.suffix}"
        counter += 1
    return path


def capture_entry(page: Any, entry: CommentEntry, output_path: Path) -> None:
    entry.handle.scroll_into_view_if_needed(timeout=8_000)
    page.wait_for_timeout(700)
    entry.handle.screenshot(path=str(output_path))


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("780x560")
        self.minsize(720, 500)

        self.url_var = tk.StringVar(value="https://m.blog.naver.com/shuchel/224296188790")
        self.nickname_var = tk.StringVar()
        self.output_dir_var = tk.StringVar(value=str(DEFAULT_SAVE_DIR))
        self.status_var = tk.StringVar(value="링크와 닉네임을 입력한 뒤 캡처를 시작하세요.")

        self.current_match_mode: str | None = None
        self.current_candidates: list[dict[str, Any]] = []

        self._build_widgets()

    def _build_widgets(self) -> None:
        outer = ttk.Frame(self, padding=16)
        outer.pack(fill=tk.BOTH, expand=True)
        outer.columnconfigure(1, weight=1)
        outer.rowconfigure(5, weight=1)

        ttk.Label(outer, text="블로그 글 링크").grid(row=0, column=0, sticky=tk.W, pady=(0, 8))
        ttk.Entry(outer, textvariable=self.url_var).grid(row=0, column=1, columnspan=2, sticky=tk.EW, pady=(0, 8))

        ttk.Label(outer, text="닉네임").grid(row=1, column=0, sticky=tk.W, pady=(0, 8))
        ttk.Entry(outer, textvariable=self.nickname_var, width=30).grid(row=1, column=1, sticky=tk.EW, pady=(0, 8))

        ttk.Label(outer, text="저장 폴더").grid(row=2, column=0, sticky=tk.W, pady=(0, 8))
        ttk.Entry(outer, textvariable=self.output_dir_var).grid(row=2, column=1, sticky=tk.EW, pady=(0, 8))
        ttk.Button(outer, text="폴더 선택", command=self.choose_output_dir).grid(row=2, column=2, sticky=tk.E, padx=(8, 0), pady=(0, 8))

        button_bar = ttk.Frame(outer)
        button_bar.grid(row=3, column=0, columnspan=3, sticky=tk.EW, pady=(4, 12))
        self.start_button = ttk.Button(button_bar, text="찾고 캡처", command=self.start_find_or_capture)
        self.start_button.pack(side=tk.LEFT)
        self.login_button = ttk.Button(button_bar, text="네이버 로그인 열기", command=self.open_login_window)
        self.login_button.pack(side=tk.LEFT, padx=(8, 0))
        self.capture_selected_button = ttk.Button(
            button_bar,
            text="선택 댓글 캡처",
            command=self.capture_selected_candidate,
            state=tk.DISABLED,
        )
        self.capture_selected_button.pack(side=tk.LEFT, padx=(8, 0))
        self.open_folder_button = ttk.Button(button_bar, text="저장 폴더 열기", command=self.open_output_dir)
        self.open_folder_button.pack(side=tk.LEFT, padx=(8, 0))

        self.progress = ttk.Progressbar(button_bar, mode="indeterminate", length=180)
        self.progress.pack(side=tk.RIGHT)

        ttk.Label(outer, textvariable=self.status_var, foreground="#155724").grid(
            row=4, column=0, columnspan=3, sticky=tk.EW, pady=(0, 8)
        )

        self.tree = ttk.Treeview(
            outer,
            columns=("index", "nickname", "date", "preview", "secret"),
            show="headings",
            selectmode="browse",
            height=12,
        )
        self.tree.heading("index", text="번호")
        self.tree.heading("nickname", text="닉네임")
        self.tree.heading("date", text="작성 시각")
        self.tree.heading("preview", text="댓글 미리보기")
        self.tree.heading("secret", text="비밀")
        self.tree.column("index", width=55, anchor=tk.CENTER, stretch=False)
        self.tree.column("nickname", width=130, stretch=False)
        self.tree.column("date", width=150, stretch=False)
        self.tree.column("preview", width=360, stretch=True)
        self.tree.column("secret", width=60, anchor=tk.CENTER, stretch=False)
        self.tree.grid(row=5, column=0, columnspan=3, sticky=tk.NSEW)

        scroll = ttk.Scrollbar(outer, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        scroll.grid(row=5, column=3, sticky=tk.NS)

        note = (
            "앱 전용 Chrome 프로필을 사용합니다. 첫 사용이면 '네이버 로그인 열기'로 로그인한 뒤 "
            "그 Chrome 창을 그대로 둔 채 캡처를 시작하세요. 비밀댓글은 로그인 계정에서 보이는 경우에만 캡처됩니다."
        )
        ttk.Label(outer, text=note, foreground="#555555", wraplength=720).grid(
            row=6, column=0, columnspan=3, sticky=tk.EW, pady=(12, 0)
        )

    def choose_output_dir(self) -> None:
        selected = filedialog.askdirectory(initialdir=self.output_dir_var.get() or str(DEFAULT_SAVE_DIR))
        if selected:
            self.output_dir_var.set(selected)

    def open_output_dir(self) -> None:
        path = Path(self.output_dir_var.get() or DEFAULT_SAVE_DIR)
        path.mkdir(parents=True, exist_ok=True)
        os.startfile(path)

    def open_login_window(self) -> None:
        try:
            open_login_profile()
            self.status_var.set("네이버 로그인 창을 열었습니다. 로그인 후 창을 그대로 둔 채 캡처를 시작해도 됩니다.")
            messagebox.showinfo(
                APP_TITLE,
                "네이버 로그인 창을 열었습니다.\n\n"
                "로그인을 마친 뒤 이 Chrome 창을 그대로 둔 채 '찾고 캡처'를 눌러 주세요.\n"
                "이미 닫았다면 저장된 로그인 세션으로 새 캡처 창을 엽니다.",
            )
        except Exception as exc:
            messagebox.showerror(APP_TITLE, str(exc))

    def start_find_or_capture(self) -> None:
        self.clear_candidates()
        self.current_match_mode = None
        self.start_worker(selected_index=None, forced_match_mode=None)

    def capture_selected_candidate(self) -> None:
        selected_items = self.tree.selection()
        if not selected_items:
            messagebox.showinfo(APP_TITLE, "캡처할 댓글 후보를 선택해 주세요.")
            return
        item = selected_items[0]
        selected_index = int(self.tree.set(item, "index")) - 1
        self.start_worker(selected_index=selected_index, forced_match_mode=self.current_match_mode)

    def start_worker(self, selected_index: int | None, forced_match_mode: str | None) -> None:
        raw_url = self.url_var.get()
        nickname = self.nickname_var.get()
        output_dir = Path(self.output_dir_var.get() or DEFAULT_SAVE_DIR)

        self.set_busy(True)
        self.status_var.set("Chrome을 열고 댓글을 찾는 중입니다...")

        def progress(message: str) -> None:
            self.after(0, lambda: self.status_var.set(message))

        def worker() -> None:
            try:
                result = run_capture(
                    raw_url=raw_url,
                    nickname=nickname,
                    output_dir=output_dir,
                    selected_index=selected_index,
                    forced_match_mode=forced_match_mode,
                    progress=progress,
                )
                self.after(0, lambda: self.handle_result(result))
            except Exception as exc:
                details = str(exc)
                if not isinstance(exc, CommentCaptureError):
                    details = f"{details}\n\n{traceback.format_exc()}"
                self.after(0, lambda: self.handle_error(details))

        threading.Thread(target=worker, daemon=True).start()

    def handle_result(self, result: CaptureResult) -> None:
        self.set_busy(False)
        if result.status == "captured" and result.saved_path:
            self.clear_candidates()
            self.status_var.set(f"저장 완료: {result.saved_path}")
            messagebox.showinfo(APP_TITLE, f"댓글 캡처를 저장했습니다.\n\n{result.saved_path}")
            return

        if result.status == "multiple" and result.candidates:
            self.current_candidates = result.candidates
            self.current_match_mode = result.match_mode
            self.populate_candidates(result.candidates)
            self.capture_selected_button.configure(state=tk.NORMAL)
            mode_label = "정확 일치" if result.match_mode == "exact" else "부분 일치"
            self.status_var.set(f"{mode_label} 후보 {len(result.candidates)}개를 찾았습니다. 캡처할 댓글을 선택하세요.")
            return

        self.status_var.set("작업을 완료했지만 저장된 파일이 없습니다.")

    def handle_error(self, details: str) -> None:
        self.set_busy(False)
        self.status_var.set("오류가 발생했습니다.")
        messagebox.showerror(APP_TITLE, details)

    def set_busy(self, busy: bool) -> None:
        state = tk.DISABLED if busy else tk.NORMAL
        self.start_button.configure(state=state)
        self.login_button.configure(state=state)
        self.open_folder_button.configure(state=state)
        if busy:
            self.capture_selected_button.configure(state=tk.DISABLED)
            self.progress.start(12)
        else:
            self.progress.stop()
            if self.current_candidates:
                self.capture_selected_button.configure(state=tk.NORMAL)

    def clear_candidates(self) -> None:
        self.current_candidates = []
        self.capture_selected_button.configure(state=tk.DISABLED)
        for item in self.tree.get_children():
            self.tree.delete(item)

    def populate_candidates(self, candidates: list[dict[str, Any]]) -> None:
        self.clear_candidates()
        self.current_candidates = candidates
        for candidate in candidates:
            secret_label = "예" if candidate.get("secret") else ""
            self.tree.insert(
                "",
                tk.END,
                values=(
                    candidate["index"],
                    candidate["nickname"],
                    candidate["date"],
                    candidate["preview"],
                    secret_label,
                ),
            )
        first = self.tree.get_children()
        if first:
            self.tree.selection_set(first[0])
            self.tree.focus(first[0])


def main() -> None:
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
