# -*- coding: utf-8 -*-
from __future__ import annotations

import datetime as _dt
import os
import re
import subprocess
import sys
import threading
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, quote, urlparse

import tkinter as tk
from tkinter import filedialog, messagebox, ttk


APP_TITLE = "네이버 블로그 댓글 캡처"
APP_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
DEFAULT_SAVE_DIR = Path.home() / "Downloads" / "naver-comment-captures"
LOG_PATH = APP_DIR / "naver_comment_capture.log"
CHROME_USER_DATA_DIR = APP_DIR / "chrome-profile"
LOGIN_URL = "https://nid.naver.com/nidlogin.login"
CDP_PORT = 9222
CDP_URL = f"http://127.0.0.1:{CDP_PORT}"
EMAIL_PATTERN = re.compile(r"[\w.!#$%&'*+/=?^`{|}~-]+@[\w.-]+\.[A-Za-z]{2,}", re.IGNORECASE)
_CHROME_EXE_CACHE: Path | None = None
_CHROME_CANDIDATES_CHECKED: list[Path] = []

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
POINT_REQUEST_URL = "https://point.directwed.co.kr/point/pointrequest"
POINT_REMARK_URLS = {
    "224249960139": "https://docs.google.com/spreadsheets/d/1_VuJyqneyLB7H8easxbmFZ1rCuZe1FnkXqznDru3dNE/edit?usp=sharing",
    "224274558663": "https://docs.google.com/spreadsheets/d/1cBm3us7MkvBi5qf6H6OSU92Q-1EJbDyO/edit?usp=sharing&ouid=100290969452498788275&rtpof=true&sd=true",
    "224292770619": "https://docs.google.com/spreadsheets/d/1lL_yWFOGVNgwVlZVvs3OYhOFPInuDF1T/edit?usp=sharing&ouid=100290969452498788275&rtpof=true&sd=true",
    "224286946614": "",
    "224296086311": "",
    "224296188790": "",
}
POINT_COMPLETION_PHRASES = (
    "완료",
    "신청되었습니다",
    "등록되었습니다",
    "저장되었습니다",
)


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
    email: str | None = None
    emails: list[str] | None = None
    comment_text: str | None = None
    comment_saved_path: Path | None = None


@dataclass
class MailCaptureResult:
    status: str
    email: str
    saved_path: Path | None = None
    candidates: list[dict[str, Any]] | None = None
    mail_id: str | None = None


@dataclass
class TargetDiscoveryResult:
    targets: list[dict[str, Any]]
    total_count: int
    checked_count: int
    share_marker_index: int | None = None


@dataclass
class BatchCaptureResult:
    saved_paths: list[Path]
    failures: list[str]
    skipped: list[str] | None = None


@dataclass
class PointRequestBatchResult:
    completed: list[str]
    failures: list[str]
    skipped: list[str]


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


def target_id_from_email(email: str) -> str:
    email = compact_text(email)
    if "@" in email:
        return email.split("@", 1)[0]
    return email or "target"


def extract_emails(value: str) -> list[str]:
    emails: list[str] = []
    seen: set[str] = set()
    for match in EMAIL_PATTERN.findall(value or ""):
        email = match.strip(".,;:()[]{}<>\"'")
        key = email.casefold()
        if key and key not in seen:
            seen.add(key)
            emails.append(email)
    return emails


def find_preferred_mail_entry(entries: list[dict[str, Any]], subject_keyword: str) -> dict[str, Any] | None:
    keyword = compact_text(subject_keyword).casefold()
    if not keyword:
        return None
    for entry in entries:
        subject = compact_text(str(entry.get("subject", ""))).casefold()
        if keyword in subject:
            return entry
    return None


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


def current_page_matches_post(page: Any, post: BlogPost, raw_url: str) -> bool:
    try:
        current_url = page.url or ""
    except Exception:
        return False
    return url_matches_post(current_url, post, raw_url)


def url_matches_post(current_url: str, post: BlogPost, raw_url: str) -> bool:
    current = normalize_url_prefix(current_url)
    if not current or current == "about:blank":
        return False

    for candidate in post_url_prefix_candidates(post, raw_url):
        if candidate and current.startswith(candidate):
            return True

    return url_has_post_identity(current_url, post)


def post_url_prefix_candidates(post: BlogPost, raw_url: str) -> list[str]:
    mobile_postview_url = (
        "https://m.blog.naver.com/PostView.naver"
        f"?blogId={quote(post.blog_id)}&logNo={post.log_no}"
    )
    urls = [
        raw_url,
        post.mobile_url,
        post.mobile_comment_url,
        mobile_postview_url,
        post.desktop_postview_url,
    ]
    candidates: list[str] = []
    seen: set[str] = set()
    for url in urls:
        normalized = normalize_url_prefix(url)
        if normalized and normalized not in seen:
            seen.add(normalized)
            candidates.append(normalized)
    return candidates


def normalize_url_prefix(value: str) -> str:
    url = compact_text(value)
    if not url:
        return ""
    if "://" not in url and not url.casefold().startswith("about:"):
        url = "https://" + url
    parsed = urlparse(url)
    if parsed.scheme and parsed.netloc:
        url = parsed._replace(fragment="").geturl()
    return url.rstrip("/").casefold()


def url_has_post_identity(url: str, post: BlogPost) -> bool:
    parsed = urlparse(compact_text(url))
    query = parse_qs(parsed.query)
    query_blog_id = first_query_value(query, "blogId")
    query_log_no = first_query_value(query, "logNo")
    if query_blog_id.casefold() == post.blog_id.casefold() and query_log_no == post.log_no:
        return True

    parts = [part for part in parsed.path.strip("/").split("/") if part]
    for index, part in enumerate(parts[:-1]):
        if part.casefold() == post.blog_id.casefold() and parts[index + 1] == post.log_no:
            return True
    return False


def first_query_value(query: dict[str, list[str]], key: str) -> str:
    values = query.get(key) or []
    return values[0].strip() if values else ""


def chrome_command_lines() -> list[str]:
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
        return []
    return [line for line in (completed.stdout or "").splitlines() if line.strip()]


def is_automation_chrome_running() -> bool:
    return any(is_app_chrome_command_line(command_line) for command_line in chrome_command_lines())


def is_app_chrome_command_line(command_line: str) -> bool:
    command = (command_line or "").casefold()
    expected = str(CHROME_USER_DATA_DIR).casefold()
    has_current_profile = expected in command
    has_debug_port = f"--remote-debugging-port={CDP_PORT}" in command
    has_app_profile = "chrome-profile" in command
    return has_current_profile or (has_debug_port and has_app_profile)


def ensure_local_environment() -> Path:
    chrome_exe = ensure_chrome_exists()
    CHROME_USER_DATA_DIR.mkdir(parents=True, exist_ok=True)
    return chrome_exe


def open_login_profile() -> None:
    chrome_exe = ensure_chrome_exists()
    CHROME_USER_DATA_DIR.mkdir(parents=True, exist_ok=True)
    if is_automation_chrome_running():
        raise CommentCaptureError(
            "로그인/캡처용 Chrome 창이 이미 열려 있습니다. 그 창에서 로그인하거나, 닫은 뒤 다시 눌러 주세요."
        )
    subprocess.Popen(
        [
            str(chrome_exe),
            f"--user-data-dir={CHROME_USER_DATA_DIR}",
            *chrome_launch_args(),
            "--new-window",
            LOGIN_URL,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def open_or_connect_chrome_context(
    playwright: Any,
    chrome_exe: Path,
    viewport: dict[str, int],
    report: Callable[[str], None],
    launch_error_message: str,
) -> tuple[Any, bool, Any | None]:
    if is_automation_chrome_running():
        report("열려 있는 로그인/캡처용 Chrome에 연결하는 중...")
        try:
            browser = playwright.chromium.connect_over_cdp(CDP_URL, timeout=10_000)
            context = browser.contexts[0] if browser.contexts else browser.new_context()
            return context, False, browser
        except Exception as exc:
            raise CommentCaptureError(
                "열려 있는 로그인/캡처용 Chrome에 연결하지 못했습니다. "
                "열려 있는 로그인/캡처용 Chrome 창을 모두 닫고 다시 시도해 주세요."
            ) from exc

    report("Chrome 프로필을 여는 중...")
    try:
        context = playwright.chromium.launch_persistent_context(
            user_data_dir=str(CHROME_USER_DATA_DIR),
            executable_path=str(chrome_exe),
            headless=False,
            viewport=viewport,
            locale="ko-KR",
            timezone_id="Asia/Seoul",
            timeout=30_000,
            args=chrome_launch_args(),
        )
        return context, True, None
    except Exception as exc:
        raise CommentCaptureError(launch_error_message) from exc


def chrome_launch_args() -> list[str]:
    return [
        f"--remote-debugging-port={CDP_PORT}",
        "--remote-debugging-address=127.0.0.1",
        "--no-first-run",
        "--disable-session-crashed-bubble",
        "--disable-blink-features=AutomationControlled",
    ]


def ensure_chrome_exists() -> Path:
    chrome_exe = find_chrome_exe()
    if chrome_exe:
        return chrome_exe
    checked = "\n".join(f"- {path}" for path in _CHROME_CANDIDATES_CHECKED)
    raise CommentCaptureError(
        "Chrome 실행 파일을 찾지 못했습니다.\n\n"
        "Chrome을 설치한 뒤 다시 실행해 주세요. 확인한 경로:\n"
        f"{checked or '- 후보 경로 없음'}"
    )


def find_chrome_exe() -> Path | None:
    global _CHROME_EXE_CACHE, _CHROME_CANDIDATES_CHECKED

    if _CHROME_EXE_CACHE and _CHROME_EXE_CACHE.is_file():
        return _CHROME_EXE_CACHE

    candidates: list[Path] = []
    seen: set[str] = set()
    for candidate in chrome_path_candidates():
        key = str(candidate).casefold()
        if key in seen:
            continue
        seen.add(key)
        candidates.append(candidate)

    _CHROME_CANDIDATES_CHECKED = candidates
    for candidate in candidates:
        if candidate.is_file():
            _CHROME_EXE_CACHE = candidate
            return candidate
    _CHROME_EXE_CACHE = None
    return None


def chrome_path_candidates() -> list[Path]:
    candidates: list[Path] = []

    def add(path: str | Path | None) -> None:
        if path:
            candidates.append(Path(path).expanduser())

    for env_name in ("ProgramFiles", "ProgramFiles(x86)", "LocalAppData"):
        base = os.environ.get(env_name)
        if base:
            add(Path(base) / "Google" / "Chrome" / "Application" / "chrome.exe")

    user_profile = os.environ.get("UserProfile")
    if user_profile:
        add(Path(user_profile) / "AppData" / "Local" / "Google" / "Chrome" / "Application" / "chrome.exe")

    add(r"C:\Program Files\Google\Chrome\Application\chrome.exe")
    add(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe")

    candidates.extend(registry_chrome_candidates())
    candidates.extend(where_chrome_candidates())
    return candidates


def registry_chrome_candidates() -> list[Path]:
    try:
        import winreg
    except ImportError:
        return []

    paths: list[Path] = []
    subkeys = (
        r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe",
        r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe",
    )
    for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for subkey in subkeys:
            try:
                with winreg.OpenKey(root, subkey) as key:
                    value, _ = winreg.QueryValueEx(key, None)
                    if value:
                        paths.append(Path(value))
            except OSError:
                continue
    return paths


def where_chrome_candidates() -> list[Path]:
    try:
        completed = subprocess.run(
            ["where", "chrome"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except Exception:
        return []
    return [Path(line.strip()) for line in completed.stdout.splitlines() if line.strip()]


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
    chrome_exe = ensure_local_environment()
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
            context, close_context_when_done, browser = open_or_connect_chrome_context(
                playwright=playwright,
                chrome_exe=chrome_exe,
                viewport={"width": 390, "height": 844},
                report=report,
                launch_error_message="앱 전용 Chrome 프로필을 열지 못했습니다. 열린 로그인/캡처용 Chrome 창을 닫고 다시 실행해 주세요.",
            )
        except Exception as exc:
            if isinstance(exc, CommentCaptureError):
                raise
            raise CommentCaptureError("앱 전용 Chrome 프로필을 열지 못했습니다. 열린 로그인/캡처용 Chrome 창을 닫고 다시 실행해 주세요.") from exc

        try:
            report("첫 번째 Chrome 탭을 준비하는 중...")
            page = get_blog_page(context)
            page.set_default_timeout(10_000)
            reuse_current_page = current_page_matches_post(page, post, raw_url)
            if reuse_current_page:
                report("현재 블로그 탭의 글을 그대로 사용합니다.")
            open_comments(
                page,
                post,
                PlaywrightTimeoutError,
                report,
                reuse_current_page=reuse_current_page,
            )
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
            comment_text = "\n".join(part for part in [entry.content, entry.text] if part)
            emails = extract_emails(comment_text)
            target_id = target_id_from_email(emails[0]) if emails else nickname
            output_path = make_output_path(output_dir, post, target_id)
            capture_entry(page, entry, output_path)
            return CaptureResult(
                status="captured",
                saved_path=output_path,
                match_mode=match_mode,
                email=emails[0] if emails else None,
                emails=emails,
                comment_text=comment_text,
                comment_saved_path=output_path,
            )
        finally:
            if close_context_when_done and context is not None:
                context.close()


def open_comments(
    page: Any,
    post: BlogPost,
    timeout_error_type: type[Exception],
    report: Callable[[str], None] | None = None,
    reuse_current_page: bool = False,
) -> None:
    def tell(message: str) -> None:
        if report:
            report(message)

    if reuse_current_page:
        if wait_for_comment_area(page, timeout_ms=1_200):
            tell("댓글창이 이미 열려 있어 바로 댓글을 찾습니다.")
            return
        tell("현재 글에서 댓글창을 여는 중...")
    else:
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


def make_capture_output_path(output_dir: Path, blog_article_id: str, target_id: str, capture_kind: str) -> Path:
    base_stem = "_".join(
        [
            sanitize_filename(blog_article_id),
            sanitize_filename(target_id),
            sanitize_filename(capture_kind),
        ]
    )
    path = output_dir / f"{base_stem}.png"
    counter = 2
    while path.exists():
        path = output_dir / f"{base_stem}_{counter}.png"
        counter += 1
    return path


def make_output_path(output_dir: Path, post: BlogPost, target_id: str) -> Path:
    return make_capture_output_path(output_dir, post.log_no, target_id, "comment")


def capture_entry(page: Any, entry: CommentEntry, output_path: Path) -> None:
    entry.handle.scroll_into_view_if_needed(timeout=8_000)
    page.wait_for_timeout(700)
    entry.handle.screenshot(path=str(output_path))


def run_discover_targets(
    raw_url: str,
    progress: Callable[[str], None] | None = None,
) -> TargetDiscoveryResult:
    def report(message: str) -> None:
        write_log(message)
        if progress:
            progress(message)

    post = parse_blog_post(raw_url)
    report("캡처대상 탐색 환경 확인 중...")
    chrome_exe = ensure_local_environment()

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
            context, close_context_when_done, browser = open_or_connect_chrome_context(
                playwright=playwright,
                chrome_exe=chrome_exe,
                viewport={"width": 390, "height": 844},
                report=report,
                launch_error_message="앱 전용 Chrome 프로필을 열지 못했습니다. 열린 로그인/캡처용 Chrome 창을 닫고 다시 실행해 주세요.",
            )
            page = prepare_blog_comment_page(context, post, raw_url, PlaywrightTimeoutError, report)
            report("댓글을 최대한 불러오는 중...")
            load_all_comments(page, report)
            report("댓글 내용을 분석하는 중...")
            entries = collect_comment_entries(page)
            targets, share_marker_index = build_comment_targets(entries, blog_article_id=post.log_no)
            checked_count = sum(1 for target in targets if target["selected"])
            return TargetDiscoveryResult(
                targets=targets,
                total_count=len(targets),
                checked_count=checked_count,
                share_marker_index=share_marker_index,
            )
        finally:
            if close_context_when_done and context is not None:
                context.close()


def run_comment_batch_capture(
    raw_url: str,
    output_dir: Path,
    targets: list[dict[str, Any]],
    progress: Callable[[str], None] | None = None,
) -> BatchCaptureResult:
    def report(message: str) -> None:
        write_log(message)
        if progress:
            progress(message)

    selected_targets = [target for target in targets if target.get("selected")]
    if not selected_targets:
        raise CommentCaptureError("체크된 댓글 캡처대상이 없습니다.")

    post = parse_blog_post(raw_url)
    report("댓글 캡처 환경 확인 중...")
    chrome_exe = ensure_local_environment()
    output_dir.mkdir(parents=True, exist_ok=True)

    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise CommentCaptureError(
            "Playwright가 설치되어 있지 않습니다. run_naver_comment_capture.bat으로 실행하면 자동 설치됩니다."
        ) from exc

    saved_paths: list[Path] = []
    failures: list[str] = []
    context = None
    browser = None
    close_context_when_done = False
    with sync_playwright() as playwright:
        try:
            context, close_context_when_done, browser = open_or_connect_chrome_context(
                playwright=playwright,
                chrome_exe=chrome_exe,
                viewport={"width": 390, "height": 844},
                report=report,
                launch_error_message="앱 전용 Chrome 프로필을 열지 못했습니다. 열린 로그인/캡처용 Chrome 창을 닫고 다시 실행해 주세요.",
            )
            page = prepare_blog_comment_page(context, post, raw_url, PlaywrightTimeoutError, report)
            ensure_comments_loaded_for_targets(page, selected_targets, report)
            report("댓글 내용을 분석하는 중...")
            rows, _ = build_comment_targets(
                collect_comment_entries(page),
                include_entries=True,
                blog_article_id=post.log_no,
            )
            used_row_ids: set[str] = set()
            total = len(selected_targets)
            for index, target in enumerate(selected_targets, start=1):
                report(f"댓글 캡처 중... ({index}/{total}) {target.get('nickname', '')}")
                row = find_matching_target_row(target, rows, used_row_ids)
                if not row:
                    failures.append(f"{target.get('index')}. {target.get('nickname', '')}: 댓글을 다시 찾지 못했습니다.")
                    continue
                used_row_ids.add(row["row_id"])
                target_id = target_id_from_email(str(row.get("email", "")))
                output_path = make_output_path(output_dir, post, target_id)
                capture_entry(page, row["entry"], output_path)
                saved_paths.append(output_path)
            return BatchCaptureResult(saved_paths=saved_paths, failures=failures)
        finally:
            if close_context_when_done and context is not None:
                context.close()


def prepare_blog_comment_page(
    context: Any,
    post: BlogPost,
    raw_url: str,
    timeout_error_type: type[Exception],
    report: Callable[[str], None] | None = None,
) -> Any:
    page = get_blog_page(context)
    page.set_default_timeout(10_000)
    reuse_current_page = current_page_matches_post(page, post, raw_url)
    if reuse_current_page and report:
        report("현재 블로그 탭의 글을 그대로 사용합니다.")
    open_comments(
        page,
        post,
        timeout_error_type,
        report,
        reuse_current_page=reuse_current_page,
    )
    return page


def load_all_comments(
    page: Any,
    report: Callable[[str], None] | None = None,
    max_rounds: int = 35,
    stable_round_limit: int = 2,
) -> None:
    previous_count = -1
    stable_rounds = 0
    ineffective_click_rounds = 0
    for round_index in range(max_rounds):
        clicked = try_click_more_comments(page)
        scroll_comment_frames(page)
        page.wait_for_timeout(500 if clicked else 350)
        current_count = count_visible_comment_entries(page)
        if report and (round_index == 0 or current_count != previous_count):
            report(f"댓글을 불러오는 중... 현재 {current_count}개")
        if current_count <= previous_count:
            if clicked:
                ineffective_click_rounds += 1
            else:
                stable_rounds += 1
        else:
            stable_rounds = 0
            ineffective_click_rounds = 0
        previous_count = current_count
        if stable_rounds >= stable_round_limit or ineffective_click_rounds >= 4:
            break


def try_click_more_comments(page: Any) -> bool:
    script = """
        () => {
          const visible = (el) => {
            if (!el) return false;
            const rect = el.getBoundingClientRect();
            const style = window.getComputedStyle(el);
            return rect.width >= 5 && rect.height >= 5 && style.display !== 'none' && style.visibility !== 'hidden';
          };
          const clickNode = (el) => {
            el.scrollIntoView({ block: 'center', inline: 'nearest' });
            el.click();
            return true;
          };
          const selectors = [
            '.u_cbox_btn_more',
            '.u_cbox_more_wrap a',
            '.u_cbox_more_wrap button'
          ];
          for (const selector of selectors) {
            for (const node of Array.from(document.querySelectorAll(selector)).slice(0, 5)) {
              if (visible(node)) return clickNode(node);
            }
          }
          for (const node of Array.from(document.querySelectorAll('a, button')).slice(0, 80)) {
            const text = (node.innerText || node.textContent || '').replace(/\\s+/g, ' ').trim();
            if (visible(node) && (text.includes('댓글 더보기') || text.includes('더보기'))) {
              return clickNode(node);
            }
          }
          return false;
        }
    """
    for frame in page.frames:
        try:
            if frame.evaluate(script):
                return True
        except Exception:
            continue
    return False


def count_visible_comment_entries(page: Any) -> int:
    script = """
        (selectors) => {
          const visible = (el) => {
            if (!el) return false;
            const rect = el.getBoundingClientRect();
            const style = window.getComputedStyle(el);
            return rect.width >= 5 && rect.height >= 5 && style.display !== 'none' && style.visibility !== 'hidden';
          };
          for (const selector of selectors) {
            const count = Array.from(document.querySelectorAll(selector)).filter(visible).length;
            if (count > 0) return count;
          }
          return 0;
        }
    """
    total = 0
    for frame in page.frames:
        try:
            total += int(frame.evaluate(script, COMMENT_CONTAINER_SELECTORS) or 0)
        except Exception:
            continue
    return total


def ensure_comments_loaded_for_targets(
    page: Any,
    targets: list[dict[str, Any]],
    report: Callable[[str], None] | None = None,
) -> None:
    required_count = required_comment_count_for_targets(targets)
    current_count = count_visible_comment_entries(page)
    if required_count and current_count >= required_count:
        if report:
            report(f"이미 로드된 댓글 목록을 재사용합니다. 현재 {current_count}개")
        return
    if report:
        report("댓글 목록을 다시 확인하는 중...")
    load_all_comments(page, report)


def required_comment_count_for_targets(targets: list[dict[str, Any]]) -> int:
    required_count = 0
    for target in targets:
        try:
            required_count = max(required_count, int(target.get("original_index", -1)) + 1)
        except Exception:
            continue
    return required_count


def scroll_comment_frames(page: Any) -> None:
    for frame in page.frames:
        try:
            frame.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
        except Exception:
            continue
    try:
        page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
    except Exception:
        pass


def build_comment_targets(
    entries: list[CommentEntry],
    include_entries: bool = False,
    blog_article_id: str = "",
) -> tuple[list[dict[str, Any]], int | None]:
    rows: list[dict[str, Any]] = []
    for original_index, entry in enumerate(entries):
        comment_text = comment_entry_text(entry)
        emails = extract_emails(comment_text)
        preview = compact_text(entry.content or entry.text)
        if len(preview) > 120:
            preview = preview[:117] + "..."
        parsed_date = parse_comment_datetime(entry.date)
        row = {
            "row_id": f"target_{original_index}",
            "original_index": original_index,
            "blog_article_id": blog_article_id,
            "nickname": entry.nickname,
            "email": emails[0] if emails else "",
            "emails": emails,
            "date": entry.date,
            "preview": preview,
            "content": entry.content,
            "text": entry.text,
            "comment_text": comment_text,
            "secret": entry.secret,
            "sort_timestamp": parsed_date.timestamp() if parsed_date else None,
            "identity": comment_identity(entry),
            "selected": False,
        }
        if include_entries:
            row["entry"] = entry
        rows.append(row)

    rows.sort(key=comment_target_sort_key)
    share_marker_index: int | None = None
    share_text = normalize_text("공유 완료")
    for index, row in enumerate(rows):
        row["sorted_index"] = index + 1
        if share_text in normalize_text(row["comment_text"]):
            share_marker_index = index

    if share_marker_index is not None:
        for index, row in enumerate(rows):
            row["selected"] = index > share_marker_index

    rows = [row for row in rows if row.get("email")]
    for index, row in enumerate(rows):
        row["index"] = index + 1
    return rows, (share_marker_index + 1 if share_marker_index is not None else None)


def comment_target_sort_key(row: dict[str, Any]) -> tuple[int, float, int]:
    timestamp = row.get("sort_timestamp")
    if timestamp is None:
        return (1, float(row.get("original_index", 0)), int(row.get("original_index", 0)))
    return (0, float(timestamp), int(row.get("original_index", 0)))


def parse_comment_datetime(value: str) -> _dt.datetime | None:
    text = compact_text(value)
    now = _dt.datetime.now()
    if not text:
        return None
    if "방금" in text:
        return now
    relative_match = re.search(r"(\d+)\s*(분|시간|일)\s*전", text)
    if relative_match:
        amount = int(relative_match.group(1))
        unit = relative_match.group(2)
        if unit == "분":
            return now - _dt.timedelta(minutes=amount)
        if unit == "시간":
            return now - _dt.timedelta(hours=amount)
        if unit == "일":
            return now - _dt.timedelta(days=amount)
    if "어제" in text:
        time_match = re.search(r"(\d{1,2}):(\d{2})", text)
        base = now - _dt.timedelta(days=1)
        if time_match:
            return base.replace(hour=int(time_match.group(1)), minute=int(time_match.group(2)), second=0, microsecond=0)
        return base.replace(hour=0, minute=0, second=0, microsecond=0)

    normalized = text.replace("년", ".").replace("월", ".").replace("일", ".")
    patterns = [
        (r"(\d{4})[.\-/]\s*(\d{1,2})[.\-/]\s*(\d{1,2})\.?\s+(\d{1,2}):(\d{2})(?::(\d{2}))?", True),
        (r"(\d{4})[.\-/]\s*(\d{1,2})[.\-/]\s*(\d{1,2})", False),
        (r"(\d{1,2})[.\-/]\s*(\d{1,2})\.?\s+(\d{1,2}):(\d{2})(?::(\d{2}))?", "current_year"),
    ]
    for pattern, mode in patterns:
        match = re.search(pattern, normalized)
        if not match:
            continue
        try:
            if mode == "current_year":
                month, day, hour, minute, second = match.groups(default="0")
                return _dt.datetime(now.year, int(month), int(day), int(hour), int(minute), int(second or 0))
            year, month, day = match.group(1), match.group(2), match.group(3)
            if mode is True:
                hour, minute, second = match.group(4), match.group(5), match.group(6) or "0"
                return _dt.datetime(int(year), int(month), int(day), int(hour), int(minute), int(second))
            return _dt.datetime(int(year), int(month), int(day))
        except ValueError:
            continue
    return None


def comment_entry_text(entry: CommentEntry) -> str:
    return "\n".join(part for part in [entry.content, entry.text] if part)


def comment_identity(entry: CommentEntry) -> str:
    key = "|".join(
        [
            normalize_text(entry.nickname),
            normalize_text(entry.date),
            normalize_text(entry.content),
            normalize_text(entry.text),
        ]
    )
    return key[:500]


def target_identity(target: dict[str, Any]) -> str:
    return "|".join(
        [
            normalize_text(str(target.get("nickname", ""))),
            normalize_text(str(target.get("date", ""))),
            normalize_text(str(target.get("content", ""))),
            normalize_text(str(target.get("text", ""))),
        ]
    )[:500]


def target_matches_row(target: dict[str, Any], row: dict[str, Any]) -> bool:
    if target_identity(target) == row.get("identity"):
        return True
    if normalize_text(str(target.get("nickname", ""))) != normalize_text(str(row.get("nickname", ""))):
        return False
    if normalize_text(str(target.get("date", ""))) != normalize_text(str(row.get("date", ""))):
        return False
    target_email = normalize_text(str(target.get("email", "")))
    row_email = normalize_text(str(row.get("email", "")))
    if target_email and row_email and target_email != row_email:
        return False
    return normalize_text(str(target.get("preview", "")))[:80] == normalize_text(str(row.get("preview", "")))[:80]


def find_matching_target_row(
    target: dict[str, Any],
    rows: list[dict[str, Any]],
    used_row_ids: set[str],
) -> dict[str, Any] | None:
    target_index = int(target.get("index") or 0)
    if 1 <= target_index <= len(rows):
        row = rows[target_index - 1]
        if row["row_id"] not in used_row_ids and target_matches_row(target, row):
            return row

    for row in rows:
        if row["row_id"] in used_row_ids:
            continue
        if target_matches_row(target, row):
            return row
    return None


def run_mail_capture(
    email: str,
    output_dir: Path,
    selected_mail_id: str | None = None,
    blog_article_id: str = "",
    preferred_subject_keyword: str = "",
    progress: Callable[[str], None] | None = None,
) -> MailCaptureResult:
    def report(message: str) -> None:
        write_log(message)
        if progress:
            progress(message)

    email = compact_text(email)
    blog_article_id = compact_text(blog_article_id) or "unknown_article"
    if not email:
        raise CommentCaptureError("먼저 댓글에서 이메일을 찾아야 합니다.")

    report("메일 캡처 환경 확인 중...")
    chrome_exe = ensure_local_environment()
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
            context, close_context_when_done, browser = open_or_connect_chrome_context(
                playwright=playwright,
                chrome_exe=chrome_exe,
                viewport={"width": 1100, "height": 900},
                report=report,
                launch_error_message="앱 전용 Chrome 프로필을 열지 못했습니다. 로그인용 Chrome 창을 닫고 다시 실행해 주세요.",
            )
        except Exception as exc:
            if isinstance(exc, CommentCaptureError):
                raise
            raise CommentCaptureError("앱 전용 Chrome 프로필을 열지 못했습니다. 로그인용 Chrome 창을 닫고 다시 실행해 주세요.") from exc

        try:
            page = get_mail_page(context)
            page.set_default_timeout(12_000)

            mail_id = selected_mail_id
            if not mail_id:
                report(f"네이버 메일에서 {email} 검색 중...")
                open_mail_search(page, email, PlaywrightTimeoutError)
                report("메일 검색 결과를 분석하는 중...")
                entries = collect_mail_entries(page)
                if not entries:
                    raise CommentCaptureError(
                        f"'{email}'로 검색된 메일을 찾지 못했습니다. 네이버 메일 로그인 상태와 검색 결과를 확인해 주세요."
                    )
                if len(entries) > 1:
                    preferred_entry = find_preferred_mail_entry(entries, preferred_subject_keyword)
                    if preferred_entry:
                        mail_id = preferred_entry["mail_id"]
                        report("제목 키워드와 일치하는 메일을 선택했습니다.")
                    else:
                        if compact_text(preferred_subject_keyword):
                            report("제목 키워드와 일치하는 메일이 없어 선택 창을 표시합니다.")
                        return MailCaptureResult(status="multiple", email=email, candidates=entries)
                else:
                    mail_id = entries[0]["mail_id"]

            report("메일 본문을 여는 중...")
            read_url = f"https://mail.naver.com/v2/popup/read/1/{mail_id}"
            goto_page(page, read_url, PlaywrightTimeoutError)
            ensure_mail_accessible(page)
            report("메일 본문을 캡처하는 중...")
            output_path = make_mail_output_path(output_dir, blog_article_id, email)
            capture_mail_body(page, output_path)
            return MailCaptureResult(status="captured", email=email, saved_path=output_path, mail_id=mail_id)
        finally:
            if close_context_when_done and context is not None:
                context.close()


def run_point_request_batch(
    raw_url: str,
    output_dir: Path,
    targets: list[dict[str, Any]],
    progress: Callable[[str], None] | None = None,
    wait_for_user_continue: Callable[[dict[str, Any], int, int], bool] | None = None,
) -> PointRequestBatchResult:
    def report(message: str) -> None:
        write_log(message)
        if progress:
            progress(message)

    selected_targets = [target for target in targets if target.get("selected")]
    if not selected_targets:
        raise CommentCaptureError("체크된 포인트 신청 대상이 없습니다.")

    fallback_post = parse_blog_post(raw_url)
    report("포인트 신청 환경 확인 중...")
    chrome_exe = ensure_local_environment()
    output_dir.mkdir(parents=True, exist_ok=True)

    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise CommentCaptureError(
            "Playwright가 설치되어 있지 않습니다. run_naver_comment_capture.bat으로 실행하면 자동 설치됩니다."
        ) from exc

    completed: list[str] = []
    failures: list[str] = []
    skipped: list[str] = []
    context = None
    browser = None
    close_context_when_done = False
    with sync_playwright() as playwright:
        try:
            context, close_context_when_done, browser = open_or_connect_chrome_context(
                playwright=playwright,
                chrome_exe=chrome_exe,
                viewport={"width": 1200, "height": 900},
                report=report,
                launch_error_message="앱 전용 Chrome 프로필을 열지 못했습니다. 열린 로그인/캡처용 Chrome 창을 닫고 다시 실행해 주세요.",
            )
            page = get_point_page(context)
            page.set_default_timeout(12_000)
            dialog_messages: list[str] = []

            def on_dialog(dialog: Any) -> None:
                try:
                    dialog_messages.append(compact_text(dialog.message))
                    dialog.accept()
                except Exception:
                    pass

            try:
                page.on("dialog", on_dialog)
            except Exception:
                pass

            total = len(selected_targets)
            for index, target in enumerate(selected_targets, start=1):
                label = point_target_label(target)
                try:
                    payload = build_point_request_payload(target, output_dir, fallback_post)
                except Exception as exc:
                    failures.append(f"{label}: {exc}")
                    continue

                dialog_messages.clear()
                try:
                    report(f"포인트 신청 폼 입력 중... ({index}/{total}) {payload['target_id']}")
                    fill_point_request_form(page, payload, PlaywrightTimeoutError)
                    report(
                        f"포인트 신청 폼 입력 완료: {payload['target_id']} "
                        "사이트에서 내용을 확인하고 '신청' 버튼을 누른 뒤 앱에서 '계속'을 눌러 주세요."
                    )
                    if wait_for_user_continue and not wait_for_user_continue(payload, index, total):
                        skipped.append(f"{label}: 사용자가 포인트 신청 진행을 중단했습니다.")
                        report("포인트 신청 진행을 중단했습니다.")
                        break
                    completed.append(f"{payload['blog_article_id']}_{payload['target_id']}")
                    report(f"포인트 신청 사용자 확인: {payload['target_id']} ({index}/{total})")
                except Exception as exc:
                    failures.append(f"{label}: {exc}")
            return PointRequestBatchResult(completed=completed, failures=failures, skipped=skipped)
        finally:
            if close_context_when_done and context is not None:
                context.close()


def build_point_request_payload(target: dict[str, Any], output_dir: Path, fallback_post: BlogPost) -> dict[str, Any]:
    email = compact_text(str(target.get("email", "")))
    if not email:
        raise CommentCaptureError("이메일이 없어 target_id를 만들 수 없습니다.")
    target_id = target_id_from_email(email)
    blog_article_id = compact_text(str(target.get("blog_article_id", ""))) or fallback_post.log_no
    comment_file = find_existing_capture_file(output_dir, blog_article_id, target_id, "comment")
    mail_file = find_existing_capture_file(output_dir, blog_article_id, target_id, "mail")
    if not comment_file:
        raise CommentCaptureError(f"댓글 캡처 파일을 찾지 못했습니다: {blog_article_id}_{target_id}_comment.png")
    if not mail_file:
        raise CommentCaptureError(f"메일 캡처 파일을 찾지 못했습니다: {blog_article_id}_{target_id}_mail.png")
    return {
        "blog_article_id": blog_article_id,
        "target_id": target_id,
        "remark": POINT_REMARK_URLS.get(blog_article_id, ""),
        "comment_file": comment_file,
        "mail_file": mail_file,
    }


def point_target_label(target: dict[str, Any]) -> str:
    index = compact_text(str(target.get("index", "")))
    email = compact_text(str(target.get("email", "")))
    target_id = target_id_from_email(email) if email else compact_text(str(target.get("nickname", "")))
    if index:
        return f"{index}. {target_id}"
    return target_id or "대상"


def fill_point_request_form(page: Any, payload: dict[str, Any], timeout_error_type: type[Exception]) -> None:
    goto_page(page, POINT_REQUEST_URL, timeout_error_type)
    ensure_point_accessible(page)
    wait_for_point_form(page, timeout_error_type)
    scroll_to_point_request_section(page)
    select_point_option(page, "신청 구분", "적립 신청")
    page.wait_for_timeout(500)
    select_point_option(page, "포인트 구분", "블로그 정보공유")
    set_point_url_fields(page, payload["blog_article_id"])
    set_point_share_id_field(page, payload["target_id"])
    set_point_text_field(page, "비고", payload["remark"])
    check_point_checkbox(page, "동의함", fallback_label="주의 사항")
    check_point_radio(page, "홍보문구작성 여부", "예")
    set_point_file_input(page, "파일 첨부 1", Path(payload["comment_file"]), fallback_index=0)
    set_point_file_input(page, "파일 첨부 2", Path(payload["mail_file"]), fallback_index=1)


def ensure_point_accessible(page: Any) -> None:
    url = (page.url or "").casefold()
    if "login" in url:
        raise CommentCaptureError("포인트 사이트 로그인 필요: 로그인 후 다시 시도해 주세요.")
    try:
        body_text = compact_text(page.locator("body").inner_text(timeout=2_000))
    except Exception:
        body_text = ""
    if "로그인 후 사용" in body_text or "로그인" in body_text and "포인트 신청" not in body_text:
        raise CommentCaptureError("포인트 사이트 로그인 필요: 로그인 후 다시 시도해 주세요.")


def wait_for_point_form(page: Any, timeout_error_type: type[Exception], timeout_ms: int = 20_000) -> None:
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        ensure_point_accessible(page)
        try:
            has_form = page.evaluate(
                """
                () => {
                  const text = (document.body && document.body.innerText || '').replace(/\\s+/g, ' ');
                  return text.includes('포인트 신청') && text.includes('신청 구분');
                }
                """
            )
        except Exception:
            has_form = False
        if has_form:
            return
        try:
            page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
        except Exception:
            pass
        page.wait_for_timeout(500)
    raise CommentCaptureError("포인트 신청 폼을 찾지 못했습니다. 포인트 사이트 로그인 상태를 확인해 주세요.")


def scroll_to_point_request_section(page: Any) -> None:
    try:
        page.evaluate(
            """
            () => {
              const nodes = Array.from(document.querySelectorAll('section, article, form, div, h1, h2, h3, h4'));
              const node = nodes.find((el) => (el.innerText || el.textContent || '').includes('포인트 신청'));
              if (node) node.scrollIntoView({ block: 'center', inline: 'nearest' });
              else window.scrollTo(0, document.body.scrollHeight);
            }
            """
        )
    except Exception:
        pass
    page.wait_for_timeout(400)


def select_point_option(page: Any, label: str, option_text: str) -> None:
    for _ in range(12):
        if set_native_select_by_label(page, label, option_text):
            return
        if click_custom_select_by_label(page, label, option_text):
            return
        page.wait_for_timeout(400)
    raise CommentCaptureError(f"'{label}' 드롭다운에서 '{option_text}' 옵션을 선택하지 못했습니다.")


def set_native_select_by_label(page: Any, label: str, option_text: str) -> bool:
    try:
        return bool(
            page.evaluate(
                point_form_script(
                    """
                  const { label, optionText } = args;
                  const controls = controlsNearLabel(label, 'select');
                  for (const select of controls) {
                    const option = Array.from(select.options || []).find((item) => clean(item.textContent).includes(optionText));
                    if (option) {
                      select.value = option.value;
                      dispatchChange(select);
                      return true;
                    }
                  }
                  return false;
                    """
                ),
                {"label": label, "optionText": option_text},
            )
        )
    except Exception:
        return False


def click_custom_select_by_label(page: Any, label: str, option_text: str) -> bool:
    try:
        opened = bool(
            page.evaluate(
                point_form_script(
                    """
                  const { label } = args;
                  const controls = controlsNearLabel(label, 'button, [role="combobox"], input[readonly], .select, .dropdown, [class*="select"], [class*="dropdown"]');
                  for (const control of controls) {
                    if (control.tagName && control.tagName.toLowerCase() === 'select') continue;
                    if (!visible(control)) continue;
                    control.scrollIntoView({ block: 'center', inline: 'nearest' });
                    control.click();
                    return true;
                  }
                  return false;
                    """
                ),
                {"label": label},
            )
        )
    except Exception:
        opened = False
    if not opened:
        return False
    page.wait_for_timeout(500)
    try:
        return bool(
            page.evaluate(
                point_form_script(
                    """
                  const { optionText } = args;
                  const nodes = Array.from(document.querySelectorAll('li, a, button, div, span, [role="option"]'));
                  for (const node of nodes) {
                    if (!visible(node)) continue;
                    const text = clean(node.innerText || node.textContent);
                    if (text === optionText || text.includes(optionText)) {
                      node.scrollIntoView({ block: 'center', inline: 'nearest' });
                      node.click();
                      return true;
                    }
                  }
                  return false;
                    """
                ),
                {"optionText": option_text},
            )
        )
    except Exception:
        return False


def set_point_url_fields(page: Any, blog_article_id: str) -> None:
    for _ in range(15):
        ok = page.evaluate(
            point_form_script(
                """
              const { blogArticleId } = args;
              const fields = findUrlFields();
              if (!fields || fields.inputs.length < 2) return false;
              setValue(fields.inputs[0], 'shuchel');
              setValue(fields.inputs[1], blogArticleId);
              if (fields.select) selectOptionByTextOrValue(fields.select, '1');
              return true;
                """
            ),
            {"blogArticleId": blog_article_id},
        )
        if ok:
            return
        page.wait_for_timeout(400)
    raise CommentCaptureError("URL 입력칸을 찾지 못했습니다. 포인트 구분 선택 후 URL 입력 영역이 보이는지 확인해 주세요.")


def set_point_share_id_field(page: Any, target_id: str) -> None:
    labels = ("공유 아이디", "공유아이디", "공유 ID", "공유ID")
    for _ in range(10):
        for label in labels:
            if try_set_point_text_field(page, label, target_id):
                return
        page.wait_for_timeout(300)
    raise CommentCaptureError("'공유 아이디' 입력칸을 찾지 못했습니다.")


def set_point_text_field(page: Any, label: str, value: str) -> None:
    if not try_set_point_text_field(page, label, value):
        raise CommentCaptureError(f"'{label}' 입력칸을 찾지 못했습니다.")


def try_set_point_text_field(page: Any, label: str, value: str) -> bool:
    try:
        return bool(
            page.evaluate(
                point_form_script(
                    """
                  const { label, value } = args;
          const controls = controlsNearLabel(label, `${textInputSelector}, textarea`)
            .filter((control) => !control.disabled && !control.readOnly);
          if (!controls.length) return false;
          const emptyControl = controls.find((control) => clean(control.value) === '');
          setValue(emptyControl || controls[0], value || '');
          return true;
                    """
                ),
                {"label": label, "value": value},
            )
        )
    except Exception:
        return False


def check_point_checkbox(page: Any, label: str, fallback_label: str = "") -> None:
    ok = page.evaluate(
        point_form_script(
            """
          const { label, fallbackLabel } = args;
          let controls = controlsNearLabel(label, 'input[type="checkbox"]');
          if (!controls.length && fallbackLabel) controls = controlsNearLabel(fallbackLabel, 'input[type="checkbox"]');
          if (!controls.length) controls = Array.from(document.querySelectorAll('input[type="checkbox"]')).filter(visible);
          for (const checkbox of controls) {
            if (!checkbox.checked) checkbox.click();
            dispatchChange(checkbox);
            return true;
          }
          return false;
            """
        ),
        {"label": label, "fallbackLabel": fallback_label},
    )
    if not ok:
        raise CommentCaptureError("'주의 사항' 동의 체크박스를 찾지 못했습니다.")


def check_point_radio(page: Any, group_label: str, option_label: str) -> None:
    ok = page.evaluate(
        point_form_script(
            """
          const { groupLabel, optionLabel } = args;
          const controls = controlsNearLabel(groupLabel, 'input[type="radio"]');
          for (const radio of controls) {
            const container = closestGroup(radio);
            const text = clean(container && (container.innerText || container.textContent));
            if (text.includes(optionLabel) || clean(radio.value).includes(optionLabel)) {
              if (!radio.checked) radio.click();
              dispatchChange(radio);
              return true;
            }
          }
          return false;
            """
        ),
        {"groupLabel": group_label, "optionLabel": option_label},
    )
    if not ok:
        raise CommentCaptureError(f"'{group_label}' 라디오 버튼에서 '{option_label}'를 선택하지 못했습니다.")


def set_point_file_input(page: Any, label: str, file_path: Path, fallback_index: int) -> None:
    file_inputs = page.locator("input[type=file]")
    try:
        count = file_inputs.count()
    except Exception:
        count = 0
    if count <= 0:
        raise CommentCaptureError("파일 첨부 input을 찾지 못했습니다.")
    index = find_point_file_input_index(page, label)
    if index is None:
        index = fallback_index if fallback_index < count else 0
    file_inputs.nth(index).set_input_files(str(file_path))


def find_point_file_input_index(page: Any, label: str) -> int | None:
    try:
        index = page.evaluate(
            point_form_script(
                """
              const { label } = args;
              const inputs = Array.from(document.querySelectorAll('input[type="file"]'));
              const controls = controlsNearLabel(label, 'input[type="file"]');
              if (!controls.length) return null;
              const index = inputs.indexOf(controls[0]);
              return index >= 0 ? index : null;
                """
            ),
            {"label": label},
        )
    except Exception:
        return None
    return int(index) if index is not None else None


def wait_for_point_request_completion(
    page: Any,
    dialog_messages: list[str],
    timeout_error_type: type[Exception],
    timeout_ms: int = 300_000,
) -> None:
    start_url = page.url or ""
    start_completion_text = point_completion_text(page)
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        if any(point_message_is_completion(message) for message in dialog_messages):
            return
        current_url = page.url or ""
        if current_url and current_url != start_url and "/point/pointrequest" not in current_url:
            return
        current_completion_text = point_completion_text(page)
        if current_completion_text and current_completion_text != start_completion_text:
            return
        page.wait_for_timeout(1_000)
    raise CommentCaptureError("5분 안에 포인트 신청 완료를 감지하지 못했습니다.")


def point_completion_text(page: Any) -> str:
    try:
        return compact_text(
            page.evaluate(
                """
                (phrases) => {
                  const text = (document.body && document.body.innerText || '').replace(/\\s+/g, ' ').trim();
                  for (const phrase of phrases) {
                    if (text.includes(phrase)) return phrase;
                  }
                  return '';
                }
                """,
                list(POINT_COMPLETION_PHRASES),
            )
        )
    except Exception:
        return ""


def point_message_is_completion(message: str) -> bool:
    return any(phrase in (message or "") for phrase in POINT_COMPLETION_PHRASES)


def point_form_script(body: str) -> str:
    return f"(args) => {{\n{POINT_FORM_JS}\n{body}\n}}"


POINT_FORM_JS = """
    const clean = (value) => (value || '').replace(/\\s+/g, ' ').trim();
    const cleanLower = (value) => clean(value).toLocaleLowerCase();
    const textIncludes = (value, needle) => cleanLower(value).includes(cleanLower(needle));
    const visible = (el) => {
      if (!el) return false;
      const rect = el.getBoundingClientRect();
      const style = window.getComputedStyle(el);
      return rect.width >= 1 && rect.height >= 1 && style.display !== 'none' && style.visibility !== 'hidden';
    };
    const closestGroup = (el) => {
      let current = el;
      for (let i = 0; current && i < 8; i += 1) {
        if (current.matches && current.matches('tr, li, fieldset, section, article, .form-group, .form-row, .row, .input-group, div')) {
          return current;
        }
        current = current.parentElement;
      }
      return el && el.parentElement;
    };
    const nodesWithText = (label) => Array.from(document.querySelectorAll('label, th, td, dt, span, p, div, strong, b'))
      .map((node) => ({ node, text: clean(node.innerText || node.textContent) }))
      .filter((item) => textIncludes(item.text, label))
      .sort((a, b) => a.text.length - b.text.length)
      .map((item) => item.node);
    const controlsNearLabel = (label, selector) => {
      const results = [];
      const seen = new Set();
      const add = (node) => {
        if (node && !seen.has(node)) {
          seen.add(node);
          results.push(node);
        }
      };
      for (const node of nodesWithText(label)) {
        if (node.tagName && node.tagName.toLowerCase() === 'label') {
          const forId = node.getAttribute('for');
          if (forId) add(document.getElementById(forId));
        }
        let group = closestGroup(node);
        for (let depth = 0; group && depth < 5; depth += 1) {
          for (const control of Array.from(group.querySelectorAll(selector))) add(control);
          if (results.length) return results.filter(Boolean);
          group = group.parentElement;
        }
        let sibling = node.nextElementSibling;
        for (let i = 0; sibling && i < 6; i += 1) {
          if (sibling.matches && sibling.matches(selector)) add(sibling);
          for (const control of Array.from(sibling.querySelectorAll ? sibling.querySelectorAll(selector) : [])) add(control);
          if (results.length) return results.filter(Boolean);
          sibling = sibling.nextElementSibling;
        }
      }
      return results.filter(Boolean);
    };
    const dispatchChange = (el) => {
      el.dispatchEvent(new Event('input', { bubbles: true }));
      el.dispatchEvent(new Event('change', { bubbles: true }));
    };
    const setValue = (el, value) => {
      el.focus && el.focus();
      el.value = value;
      dispatchChange(el);
    };
    const textInputSelector = 'input:not([type="hidden"]):not([type="radio"]):not([type="checkbox"]):not([type="file"]):not([type="button"]):not([type="submit"]):not([type="reset"])';
    const editableControlSelector = `${textInputSelector}, textarea, select`;
    const isTextInput = (el) => el && el.matches && el.matches(textInputSelector);
    const visibleTextInputs = (container = document) => Array.from(container.querySelectorAll(textInputSelector)).filter(visible);
    const visibleSelects = (container = document) => Array.from(container.querySelectorAll('select')).filter(visible);
    const optionMatches = (option, wanted) => clean(option.textContent) === wanted || clean(option.value) === wanted;
    const selectOptionByTextOrValue = (select, wanted) => {
      if (!select) return false;
      const option = Array.from(select.options || []).find((item) => optionMatches(item, wanted));
      if (!option) return false;
      select.value = option.value;
      dispatchChange(select);
      return true;
    };
    const controlsIn = (container) => Array.from(container.querySelectorAll(editableControlSelector)).filter(visible);
    const fieldSetFromControls = (controls) => {
      const inputs = controls.filter(isTextInput);
      const select = controls.find((item) => item.matches && item.matches('select') && Array.from(item.options || []).some((option) => optionMatches(option, '1')));
      return { inputs, select };
    };
    const firstCompleteUrlFieldSet = (containers) => {
      const seen = new Set();
      for (const container of containers) {
        if (!container || seen.has(container)) continue;
        seen.add(container);
        const fields = fieldSetFromControls(controlsIn(container));
        if (fields.inputs.length >= 2 && fields.inputs.length <= 4) return fields;
      }
      return null;
    };
    const urlLabelContainers = () => {
      const containers = [];
      const add = (node) => {
        if (node && !containers.includes(node)) containers.push(node);
      };
      for (const labelNode of nodesWithText('URL')) {
        let group = closestGroup(labelNode);
        for (let depth = 0; group && depth < 5; depth += 1) {
          add(group);
          group = group.parentElement;
        }
        let sibling = labelNode.nextElementSibling;
        for (let i = 0; sibling && i < 6; i += 1) {
          add(sibling);
          sibling = sibling.nextElementSibling;
        }
      }
      for (const container of Array.from(document.querySelectorAll('tr, li, fieldset, section, article, .form-group, .form-row, .row, .input-group, div'))) {
        const text = clean(container.innerText || container.textContent);
        if (textIncludes(text, 'URL') || textIncludes(text, '주소')) add(container);
      }
      return containers;
    };
    const urlFieldsByGeometry = () => {
      const labelNode = nodesWithText('URL')[0] || nodesWithText('주소')[0];
      if (!labelNode) return null;
      const labelRect = labelNode.getBoundingClientRect();
      const labelCenterY = labelRect.top + labelRect.height / 2;
      const nearControls = Array.from(document.querySelectorAll(editableControlSelector))
        .filter(visible)
        .filter((control) => {
          const rect = control.getBoundingClientRect();
          const centerY = rect.top + rect.height / 2;
          return Math.abs(centerY - labelCenterY) <= 140 && rect.left >= labelRect.left - 20;
        })
        .sort((a, b) => {
          const ar = a.getBoundingClientRect();
          const br = b.getBoundingClientRect();
          return ar.top - br.top || ar.left - br.left;
        });
      const fields = fieldSetFromControls(nearControls);
      return fields.inputs.length >= 2 ? fields : null;
    };
    const urlFieldsBeforeRemark = () => {
      const inputs = visibleTextInputs(document);
      if (inputs.length < 2) return null;
      let remarkTop = Number.POSITIVE_INFINITY;
      const remarkControl = controlsNearLabel('비고', `${textInputSelector}, textarea`)[0];
      if (remarkControl) remarkTop = remarkControl.getBoundingClientRect().top;
      const beforeRemark = inputs.filter((input) => input.getBoundingClientRect().top < remarkTop - 1);
      if (beforeRemark.length < 2) return null;
      const selectedInputs = beforeRemark.slice(-2);
      const secondRect = selectedInputs[1].getBoundingClientRect();
      const select = visibleSelects(document)
        .filter((item) => item.getBoundingClientRect().top <= secondRect.bottom + 80)
        .filter((item) => Array.from(item.options || []).some((option) => optionMatches(option, '1')))
        .slice(-1)[0] || null;
      return { inputs: selectedInputs, select };
    };
    const findUrlFields = () => {
      const directControls = controlsNearLabel('URL', editableControlSelector);
      const directFields = fieldSetFromControls(directControls);
      if (directFields.inputs.length >= 2 && directFields.inputs.length <= 4) return directFields;
      const containerFields = firstCompleteUrlFieldSet(urlLabelContainers());
      if (containerFields) return containerFields;
      const geometryFields = urlFieldsByGeometry();
      if (geometryFields) return geometryFields;
      return urlFieldsBeforeRemark();
    };
"""


def get_blog_page(context: Any) -> Any:
    pages = open_pages(context)
    blog_page = find_page_by_hosts(pages, ("blog.naver.com", "m.blog.naver.com"))
    if blog_page:
        bring_page_to_front(blog_page)
        return blog_page

    non_login_pages = [page for page in pages if not is_login_page(page)]
    blank_page = first_blank_page(non_login_pages)
    if blank_page:
        bring_page_to_front(blank_page)
        return blank_page

    if non_login_pages:
        bring_page_to_front(non_login_pages[0])
        return non_login_pages[0]

    page = context.new_page()
    bring_page_to_front(page)
    return page


def get_mail_page(context: Any) -> Any:
    pages = open_pages(context)
    mail_page = find_page_by_hosts(pages, ("mail.naver.com",))
    if mail_page:
        bring_page_to_front(mail_page)
        return mail_page

    non_login_pages = [page for page in pages if not is_login_page(page)]
    blank_pages = [page for page in non_login_pages if is_blank_page(page)]
    if len(blank_pages) >= 2:
        bring_page_to_front(blank_pages[1])
        return blank_pages[1]

    if len(non_login_pages) >= 2:
        bring_page_to_front(non_login_pages[1])
        return non_login_pages[1]

    page = context.new_page()
    bring_page_to_front(page)
    return page


def get_point_page(context: Any) -> Any:
    pages = open_pages(context)
    point_page = find_page_by_hosts(pages, ("point.directwed.co.kr",))
    if point_page:
        bring_page_to_front(point_page)
        return point_page

    page = context.new_page()
    bring_page_to_front(page)
    return page


def open_pages(context: Any) -> list[Any]:
    pages: list[Any] = []
    for page in context.pages:
        try:
            if not page.is_closed():
                pages.append(page)
        except Exception:
            continue
    return pages


def find_page_by_hosts(pages: list[Any], hosts: tuple[str, ...]) -> Any | None:
    for page in pages:
        try:
            host = urlparse(page.url or "").netloc.casefold()
        except Exception:
            host = ""
        if any(host == expected or host.endswith("." + expected) for expected in hosts):
            return page
    return None


def first_blank_page(pages: list[Any]) -> Any | None:
    for page in pages:
        if is_blank_page(page):
            return page
    return None


def is_blank_page(page: Any) -> bool:
    try:
        return (page.url or "").casefold() in ("", "about:blank")
    except Exception:
        return False


def is_login_page(page: Any) -> bool:
    try:
        url = (page.url or "").casefold()
    except Exception:
        return False
    return "nid.naver.com" in url or "nidlogin" in url


def bring_page_to_front(page: Any) -> None:
    try:
        page.bring_to_front()
    except Exception:
        pass


def open_mail_search(page: Any, email: str, timeout_error_type: type[Exception]) -> None:
    search_url = f"https://mail.naver.com/v2/folders/-1/search?body={quote(email, safe='')}"
    goto_page(page, search_url, timeout_error_type)
    ensure_mail_accessible(page)
    wait_for_mail_search(page)


def ensure_mail_accessible(page: Any) -> None:
    current_url = (page.url or "").casefold()
    if "nid.naver.com" in current_url or "nidlogin" in current_url:
        raise CommentCaptureError("네이버 로그인이 필요합니다. '네이버 로그인 열기'로 로그인한 뒤 다시 시도해 주세요.")


def wait_for_mail_search(page: Any, timeout_ms: int = 15_000) -> None:
    deadline = _dt.datetime.now() + _dt.timedelta(milliseconds=timeout_ms)
    while _dt.datetime.now() < deadline:
        if collect_mail_entries(page):
            return
        page.wait_for_timeout(600)


def collect_mail_entries(page: Any) -> list[dict[str, Any]]:
    try:
        entries = page.evaluate(
            """
            () => {
              const clean = (value) => (value || '').replace(/\\s+/g, ' ').trim();
              const pick = (root, selectors) => {
                for (const selector of selectors) {
                  const node = root.querySelector(selector);
                  const text = clean(node && (node.innerText || node.textContent));
                  if (text) return text;
                }
                return '';
              };
              const nodes = Array.from(document.querySelectorAll('li.mail_item.read[class*="mail-"], li.mail_item[class*="mail-"]'));
              const seen = new Set();
              return nodes.map((el) => {
                const className = String(el.className || '');
                const match = className.match(/(?:^|\\s)mail-(\\d+)(?=\\s|$)/);
                if (!match) return null;
                const mailId = match[1];
                if (seen.has(mailId)) return null;
                seen.add(mailId);
                const text = clean(el.innerText || el.textContent);
                return {
                  mail_id: mailId,
                  sender: pick(el, ['.sender', '.mail_sender', '.name', '[class*="sender"]', '[class*="from"]']),
                  subject: pick(el, ['.subject', '.mail_title', '.title', 'strong', '[class*="subject"]', '[class*="title"]']),
                  date: pick(el, ['.date', '.mail_date', '.time', '[class*="date"]', '[class*="time"]']),
                  preview: pick(el, ['.preview', '.mail_preview', '.summary', '[class*="preview"]', '[class*="summary"]']) || text,
                  text
                };
              }).filter(Boolean);
            }
            """
        )
    except Exception:
        return []

    normalized: list[dict[str, Any]] = []
    for entry in entries or []:
        preview = compact_text(entry.get("preview", "") or entry.get("text", ""))
        if len(preview) > 120:
            preview = preview[:117] + "..."
        normalized.append(
            {
                "mail_id": compact_text(entry.get("mail_id", "")),
                "sender": compact_text(entry.get("sender", "")),
                "subject": compact_text(entry.get("subject", "")),
                "date": compact_text(entry.get("date", "")),
                "preview": preview,
            }
        )
    return [entry for entry in normalized if entry["mail_id"]]


def make_mail_output_path(output_dir: Path, blog_article_id: str, email: str) -> Path:
    return make_capture_output_path(output_dir, blog_article_id, target_id_from_email(email), "mail")


def find_existing_capture_file(output_dir: Path, blog_article_id: str, target_id: str, capture_kind: str) -> Path | None:
    base_stem = "_".join(
        [
            sanitize_filename(blog_article_id),
            sanitize_filename(target_id),
            sanitize_filename(capture_kind),
        ]
    )
    exact_path = output_dir / f"{base_stem}.png"
    if exact_path.is_file():
        return exact_path
    candidates = [path for path in output_dir.glob(f"{base_stem}_*.png") if path.is_file()]
    if not candidates:
        return None
    return max(candidates, key=lambda path: path.stat().st_mtime)


def capture_mail_body(page: Any, output_path: Path) -> None:
    selectors = [
        "#mail_read",
        "#readFrame",
        ".mail_view",
        ".mail_view_contents",
        ".mail_view_content",
        ".mail_view_body",
        ".mail_viewer",
        ".mail_body",
        ".read_body",
        ".read_content",
        ".viewer_body",
        ".content_body",
        ".mail_contents",
        ".mail_content",
        ".view_content",
        "[class*='mail'][class*='body']",
        "[class*='mail'][class*='content']",
        "[class*='read'][class*='body']",
        "[class*='read'][class*='content']",
        "article",
        "main",
    ]
    scroll_mail_to_bottom(page)
    deadline = _dt.datetime.now() + _dt.timedelta(seconds=12)
    while _dt.datetime.now() < deadline:
        candidates: list[tuple[float, float, Any]] = []
        for frame in page.frames:
            for selector in selectors:
                try:
                    handles = frame.query_selector_all(selector)
                except Exception:
                    continue
                for handle in handles:
                    try:
                        metrics = handle.evaluate(
                            """
                            (el) => {
                              const text = (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
                              const box = el.getBoundingClientRect();
                              const images = el.querySelectorAll('img').length;
                              return {
                                width: Math.max(box.width, el.scrollWidth || 0, el.offsetWidth || 0),
                                height: Math.max(box.height, el.scrollHeight || 0, el.offsetHeight || 0),
                                textLength: text.length,
                                images,
                                childCount: el.children.length
                              };
                            }
                            """
                        )
                    except Exception:
                        metrics = None
                    if not metrics:
                        continue
                    width = float(metrics.get("width") or 0)
                    height = float(metrics.get("height") or 0)
                    text_length = float(metrics.get("textLength") or 0)
                    image_count = float(metrics.get("images") or 0)
                    child_count = float(metrics.get("childCount") or 0)
                    if width < 150 or height < 80:
                        continue
                    if text_length < 5 and image_count == 0 and child_count < 2:
                        continue
                    content_score = text_length + image_count * 200 + child_count * 15
                    # Prefer content-rich containers, then the tighter box to avoid huge blank wrappers.
                    tightness_score = -height if height > 1600 and content_score < 800 else -abs(height - min(height, 2200))
                    candidates.append((content_score, tightness_score, handle))
        if candidates:
            _, _, handle = max(candidates, key=lambda item: (item[0], item[1]))
            handle.scroll_into_view_if_needed(timeout=5_000)
            page.wait_for_timeout(700)
            screenshot_full_element(handle, page, output_path)
            return
        page.wait_for_timeout(600)

    page.screenshot(path=str(output_path), full_page=True)


def scroll_mail_to_bottom(page: Any) -> None:
    scrollable_selectors = [
        ".mail_view_contents",
        ".mail_view_body",
        ".mail_body",
        ".read_body",
        ".viewer_body",
        ".content_body",
        "[class*='mail'][class*='body']",
        "[class*='read'][class*='body']",
        "main",
        "body",
        "html",
    ]
    previous_state = None
    stable_count = 0
    for _ in range(24):
        state = page.evaluate(
            """
            (selectors) => {
              const scrolled = [];
              for (const selector of selectors) {
                for (const el of Array.from(document.querySelectorAll(selector))) {
                  const maxScroll = Math.max(0, (el.scrollHeight || 0) - (el.clientHeight || 0));
                  if (maxScroll > 0) {
                    el.scrollTop = maxScroll;
                    scrolled.push(`${selector}:${Math.round(el.scrollTop)}/${Math.round(el.scrollHeight)}`);
                  }
                }
              }
              window.scrollTo(0, Math.max(document.body.scrollHeight, document.documentElement.scrollHeight));
              return [
                window.scrollY,
                document.body.scrollHeight,
                document.documentElement.scrollHeight,
                scrolled.join('|')
              ].join(':');
            }
            """,
            scrollable_selectors,
        )
        page.wait_for_timeout(500)
        if state == previous_state:
            stable_count += 1
            if stable_count >= 3:
                break
        else:
            stable_count = 0
            previous_state = state
    page.evaluate(
        """
        (selectors) => {
          for (const selector of selectors) {
            for (const el of Array.from(document.querySelectorAll(selector))) {
              if ((el.scrollHeight || 0) > (el.clientHeight || 0)) {
                el.scrollTop = 0;
              }
            }
          }
          window.scrollTo(0, 0);
        }
        """,
        scrollable_selectors,
    )
    page.wait_for_timeout(500)


def screenshot_full_element(handle: Any, page: Any, output_path: Path) -> None:
    try:
        metrics = handle.evaluate(
            """
            (el) => {
              const rect = el.getBoundingClientRect();
              return {
                width: Math.ceil(Math.max(rect.width, el.scrollWidth || 0, el.offsetWidth || 0)),
                height: Math.ceil(Math.max(rect.height, el.scrollHeight || 0, el.offsetHeight || 0))
              };
            }
            """
        )
    except Exception:
        metrics = {}

    width = int(metrics.get("width") or 0)
    height = int(metrics.get("height") or 0)
    if width > 0 and height > 0:
        viewport = page.viewport_size or {"width": 1100, "height": 900}
        page.set_viewport_size(
            {
                "width": max(int(viewport.get("width") or 1100), min(width + 80, 2000)),
                "height": max(int(viewport.get("height") or 900), min(height + 160, 16000)),
            }
        )
        page.wait_for_timeout(400)
    handle.screenshot(path=str(output_path))


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("780x560")
        self.minsize(720, 500)

        self.url_var = tk.StringVar(value="https://m.blog.naver.com/shuchel/224296188790")
        self.subject_keyword_var = tk.StringVar()
        self.nickname_var = tk.StringVar()
        self.email_var = tk.StringVar()
        self.output_dir_var = tk.StringVar(value=str(DEFAULT_SAVE_DIR))
        self.status_var = tk.StringVar(value="블로그 글 링크를 입력한 뒤 캡처대상 찾기를 누르세요.")
        self.email_status_var = tk.StringVar(value="찾은 이메일: 없음")

        self.current_match_mode: str | None = None
        self.current_candidates: list[dict[str, Any]] = []
        self.current_targets: list[dict[str, Any]] = []
        self.mail_batch_targets: list[dict[str, Any]] = []
        self.mail_batch_index = 0
        self.mail_batch_saved_paths: list[Path] = []
        self.mail_batch_failures: list[str] = []
        self.mail_batch_skipped: list[str] = []
        self.mail_batch_subject_keyword = ""
        self.last_found_email: str | None = None
        self.last_found_emails: list[str] = []
        self.last_comment_text: str = ""
        self.last_comment_saved_path: Path | None = None
        self.is_busy = False
        self.last_toggled_target_iid: str | None = None

        self._build_widgets()

    def _build_widgets(self) -> None:
        outer = ttk.Frame(self, padding=16)
        outer.pack(fill=tk.BOTH, expand=True)
        outer.columnconfigure(1, weight=1)
        outer.rowconfigure(5, weight=1)

        ttk.Label(outer, text="블로그 글 링크").grid(row=0, column=0, sticky=tk.W, pady=(0, 8))
        self.url_entry = ttk.Entry(outer, textvariable=self.url_var)
        self.url_entry.grid(row=0, column=1, columnspan=2, sticky=tk.EW, pady=(0, 8))

        ttk.Label(outer, text="이 키워드를 제목에 포함하는 메일 우선선택").grid(
            row=1, column=0, sticky=tk.W, pady=(0, 8)
        )
        self.subject_keyword_entry = ttk.Entry(outer, textvariable=self.subject_keyword_var)
        self.subject_keyword_entry.grid(row=1, column=1, columnspan=2, sticky=tk.EW, pady=(0, 8))

        ttk.Label(outer, text="저장 폴더").grid(row=2, column=0, sticky=tk.W, pady=(0, 8))
        self.output_dir_entry = ttk.Entry(outer, textvariable=self.output_dir_var)
        self.output_dir_entry.grid(row=2, column=1, sticky=tk.EW, pady=(0, 8))
        self.choose_folder_button = ttk.Button(outer, text="폴더 선택", command=self.choose_output_dir)
        self.choose_folder_button.grid(row=2, column=2, sticky=tk.E, padx=(8, 0), pady=(0, 8))

        button_bar = ttk.Frame(outer)
        button_bar.grid(row=3, column=0, columnspan=3, sticky=tk.EW, pady=(4, 12))
        self.login_button = ttk.Button(button_bar, text="네이버 로그인 열기", command=self.open_login_window)
        self.login_button.pack(side=tk.LEFT)
        self.start_button = ttk.Button(button_bar, text="캡처대상 찾기", command=self.start_find_or_capture)
        self.start_button.pack(side=tk.LEFT, padx=(8, 0))
        self.comment_capture_button = ttk.Button(
            button_bar,
            text="댓글 캡처",
            command=self.start_comment_batch_capture,
            state=tk.DISABLED,
        )
        self.comment_capture_button.pack(side=tk.LEFT, padx=(8, 0))
        self.email_capture_button = ttk.Button(
            button_bar,
            text="이메일 본문 캡처",
            command=self.start_email_batch_capture,
            state=tk.DISABLED,
        )
        self.email_capture_button.pack(side=tk.LEFT, padx=(8, 0))
        self.point_request_button = ttk.Button(
            button_bar,
            text="포인트 신청",
            command=self.start_point_request_batch,
            state=tk.DISABLED,
        )
        self.point_request_button.pack(side=tk.LEFT, padx=(8, 0))
        self.open_folder_button = ttk.Button(button_bar, text="저장 폴더 열기", command=self.open_output_dir)
        self.open_folder_button.pack(side=tk.LEFT, padx=(8, 0))
        self.progress = ttk.Progressbar(button_bar, mode="indeterminate", length=180)
        self.progress.pack(side=tk.RIGHT)

        ttk.Label(outer, textvariable=self.status_var, foreground="#155724").grid(
            row=4, column=0, columnspan=3, sticky=tk.EW, pady=(0, 8)
        )

        self.tree = ttk.Treeview(
            outer,
            columns=("selected", "index", "nickname", "email", "date", "preview"),
            show="headings",
            selectmode="browse",
            height=12,
        )
        self.tree.heading("selected", text="캡처대상")
        self.tree.heading("index", text="번호")
        self.tree.heading("nickname", text="닉네임")
        self.tree.heading("email", text="이메일")
        self.tree.heading("date", text="작성 시각")
        self.tree.heading("preview", text="댓글 미리보기")
        self.tree.column("selected", width=80, anchor=tk.CENTER, stretch=False)
        self.tree.column("index", width=55, anchor=tk.CENTER, stretch=False)
        self.tree.column("nickname", width=130, stretch=False)
        self.tree.column("email", width=190, stretch=False)
        self.tree.column("date", width=150, stretch=False)
        self.tree.column("preview", width=320, stretch=True)
        self.tree.grid(row=5, column=0, columnspan=3, sticky=tk.NSEW)
        self.tree.bind("<Button-1>", self.on_target_tree_click)
        self.tree.bind("<space>", self.on_target_tree_space)

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
                "로그인을 마친 뒤 이 Chrome 창을 그대로 둔 채 '캡처대상 찾기'를 눌러 주세요.\n"
                "이미 닫았다면 저장된 로그인 세션으로 새 캡처 창을 엽니다.",
            )
        except Exception as exc:
            messagebox.showerror(APP_TITLE, str(exc))

    def start_find_or_capture(self) -> None:
        self.clear_targets()
        raw_url = self.url_var.get()
        self.set_busy(True)
        self.status_var.set("Chrome을 열고 캡처대상을 찾는 중입니다...")

        def progress(message: str) -> None:
            self.after(0, lambda: self.status_var.set(message))

        def worker() -> None:
            try:
                result = run_discover_targets(raw_url=raw_url, progress=progress)
                self.after(0, lambda: self.handle_discovery_result(result))
            except Exception as exc:
                details = str(exc)
                if not isinstance(exc, CommentCaptureError):
                    details = f"{details}\n\n{traceback.format_exc()}"
                self.after(0, lambda: self.handle_error(details))

        threading.Thread(target=worker, daemon=True).start()

    def start_comment_batch_capture(self) -> None:
        selected_targets = self.checked_targets()
        if not selected_targets:
            messagebox.showinfo(APP_TITLE, "체크된 댓글 캡처대상이 없습니다.")
            return

        raw_url = self.url_var.get()
        output_dir = Path(self.output_dir_var.get() or DEFAULT_SAVE_DIR)
        self.set_busy(True)
        self.status_var.set("체크된 댓글을 캡처하는 중입니다...")

        def progress(message: str) -> None:
            self.after(0, lambda: self.status_var.set(message))

        def worker() -> None:
            try:
                result = run_comment_batch_capture(
                    raw_url=raw_url,
                    output_dir=output_dir,
                    targets=selected_targets,
                    progress=progress,
                )
                self.after(0, lambda: self.handle_comment_batch_result(result))
            except Exception as exc:
                details = str(exc)
                if not isinstance(exc, CommentCaptureError):
                    details = f"{details}\n\n{traceback.format_exc()}"
                self.after(0, lambda: self.handle_error(details))

        threading.Thread(target=worker, daemon=True).start()

    def start_email_batch_capture(self) -> None:
        selected_targets = self.checked_targets()
        if not selected_targets:
            messagebox.showinfo(APP_TITLE, "체크된 이메일 캡처대상이 없습니다.")
            return

        self.mail_batch_targets = selected_targets
        self.mail_batch_index = 0
        self.mail_batch_saved_paths = []
        self.mail_batch_failures = []
        self.mail_batch_skipped = []
        self.mail_batch_subject_keyword = compact_text(self.subject_keyword_var.get())
        self.set_busy(True)
        self.process_next_mail_target()

    def start_point_request_batch(self) -> None:
        selected_targets = self.checked_targets()
        if not selected_targets:
            messagebox.showinfo(APP_TITLE, "체크된 포인트 신청 대상이 없습니다.")
            return

        raw_url = self.url_var.get()
        output_dir = Path(self.output_dir_var.get() or DEFAULT_SAVE_DIR)
        self.set_busy(True)
        self.status_var.set("포인트 신청을 준비하는 중입니다...")

        def progress(message: str) -> None:
            self.after(0, lambda: self.status_var.set(message))

        def worker() -> None:
            try:
                result = run_point_request_batch(
                    raw_url=raw_url,
                    output_dir=output_dir,
                    targets=selected_targets,
                    progress=progress,
                    wait_for_user_continue=self.wait_for_point_continue,
                )
                self.after(0, lambda: self.handle_point_request_result(result))
            except Exception as exc:
                details = str(exc)
                if not isinstance(exc, CommentCaptureError):
                    details = f"{details}\n\n{traceback.format_exc()}"
                self.after(0, lambda: self.handle_error(details))

        threading.Thread(target=worker, daemon=True).start()

    def wait_for_point_continue(self, payload: dict[str, Any], index: int, total: int) -> bool:
        event = threading.Event()
        result = {"continue": False}

        def show_dialog() -> None:
            self.show_point_continue_dialog(payload, index, total, result, event)

        self.after(0, show_dialog)
        event.wait()
        return bool(result["continue"])

    def show_point_continue_dialog(
        self,
        payload: dict[str, Any],
        index: int,
        total: int,
        result: dict[str, bool],
        event: threading.Event,
    ) -> None:
        target_id = compact_text(str(payload.get("target_id", "")))
        blog_article_id = compact_text(str(payload.get("blog_article_id", "")))
        dialog = tk.Toplevel(self)
        dialog.title("포인트 신청 계속")
        dialog.resizable(False, False)
        dialog.transient(self)
        dialog.columnconfigure(0, weight=1)

        message = (
            f"포인트 신청 폼 입력이 완료되었습니다. ({index}/{total})\n\n"
            f"글 번호: {blog_article_id}\n"
            f"공유 아이디: {target_id}\n\n"
            "Chrome에서 내용을 확인하고 사이트의 '신청' 버튼을 직접 누른 뒤,\n"
            "이 창의 '계속'을 누르면 다음 대상의 포인트 신청 폼으로 넘어갑니다."
        )
        ttk.Label(dialog, text=message, justify=tk.LEFT, wraplength=460).grid(
            row=0,
            column=0,
            padx=18,
            pady=(18, 12),
            sticky=tk.W,
        )
        button_bar = ttk.Frame(dialog)
        button_bar.grid(row=1, column=0, padx=18, pady=(0, 18), sticky=tk.E)

        def close_with(value: bool) -> None:
            result["continue"] = value
            try:
                dialog.destroy()
            except Exception:
                pass
            event.set()

        ttk.Button(button_bar, text="중단", command=lambda: close_with(False)).pack(side=tk.RIGHT, padx=(8, 0))
        ttk.Button(button_bar, text="계속", command=lambda: close_with(True)).pack(side=tk.RIGHT)
        dialog.protocol("WM_DELETE_WINDOW", lambda: close_with(False))
        try:
            dialog.lift()
            dialog.focus_force()
        except Exception:
            pass

    def process_next_mail_target(self, selected_mail_id: str | None = None) -> None:
        while self.mail_batch_index < len(self.mail_batch_targets):
            target = self.mail_batch_targets[self.mail_batch_index]
            email = compact_text(str(target.get("email", "")))
            if email:
                break
            self.mail_batch_skipped.append(f"{target.get('index')}. {target.get('nickname', '')}: 이메일 없음")
            self.mail_batch_index += 1

        if self.mail_batch_index >= len(self.mail_batch_targets):
            self.finish_mail_batch()
            return

        target = self.mail_batch_targets[self.mail_batch_index]
        email = compact_text(str(target.get("email", "")))
        blog_article_id = compact_text(str(target.get("blog_article_id", "")))
        if not blog_article_id:
            try:
                blog_article_id = parse_blog_post(self.url_var.get()).log_no
            except Exception:
                blog_article_id = "unknown_article"
        output_dir = Path(self.output_dir_var.get() or DEFAULT_SAVE_DIR)
        self.status_var.set(f"네이버 메일에서 {email} 검색 중... ({self.mail_batch_index + 1}/{len(self.mail_batch_targets)})")

        def progress(message: str) -> None:
            self.after(0, lambda: self.status_var.set(message))

        def worker() -> None:
            try:
                result = run_mail_capture(
                    email=email,
                    output_dir=output_dir,
                    selected_mail_id=selected_mail_id,
                    blog_article_id=blog_article_id,
                    preferred_subject_keyword=self.mail_batch_subject_keyword,
                    progress=progress,
                )
                self.after(0, lambda: self.handle_batch_mail_result(result))
            except Exception as exc:
                details = str(exc)
                if not isinstance(exc, CommentCaptureError):
                    details = f"{details}\n\n{traceback.format_exc()}"
                self.after(0, lambda: self.handle_batch_mail_error(email, details))

        threading.Thread(target=worker, daemon=True).start()

    def handle_discovery_result(self, result: TargetDiscoveryResult) -> None:
        self.current_targets = result.targets
        self.populate_targets(result.targets)
        self.set_busy(False)
        if result.share_marker_index is None:
            self.status_var.set(f"댓글 {result.total_count}개를 찾았습니다. '공유 완료' 댓글이 없어 자동 체크하지 않았습니다.")
        else:
            self.status_var.set(
                f"댓글 {result.total_count}개를 찾았습니다. 마지막 '공유 완료'는 {result.share_marker_index}번, "
                f"자동 체크 {result.checked_count}개입니다."
            )

    def handle_comment_batch_result(self, result: BatchCaptureResult) -> None:
        self.set_busy(False)
        saved_count = len(result.saved_paths)
        failure_count = len(result.failures)
        self.status_var.set(f"댓글 캡처 완료: 저장 {saved_count}개, 실패 {failure_count}개")
        message = self.batch_result_message("댓글 캡처", result)
        messagebox.showinfo(APP_TITLE, message)

    def handle_batch_mail_result(self, result: MailCaptureResult) -> None:
        if result.status == "captured" and result.saved_path:
            self.mail_batch_saved_paths.append(result.saved_path)
            self.mail_batch_index += 1
            self.process_next_mail_target()
            return

        if result.status == "multiple" and result.candidates:
            self.status_var.set(f"{result.email} 검색 결과 {len(result.candidates)}개를 찾았습니다. 메일을 선택하세요.")
            self.show_mail_choice_dialog(
                result.email,
                result.candidates,
                on_select=lambda mail_id: self.process_next_mail_target(selected_mail_id=mail_id),
                on_cancel=lambda: self.handle_batch_mail_error(result.email, "메일 선택을 취소했습니다."),
            )
            return

        self.handle_batch_mail_error(result.email, "메일 캡처를 완료했지만 저장된 파일이 없습니다.")

    def handle_batch_mail_error(self, email: str, details: str) -> None:
        self.mail_batch_failures.append(f"{email}: {details}")
        self.mail_batch_index += 1
        self.process_next_mail_target()

    def finish_mail_batch(self) -> None:
        self.set_busy(False)
        result = BatchCaptureResult(
            saved_paths=self.mail_batch_saved_paths,
            failures=self.mail_batch_failures,
            skipped=self.mail_batch_skipped,
        )
        saved_count = len(result.saved_paths)
        failure_count = len(result.failures)
        skipped_count = len(result.skipped or [])
        self.status_var.set(f"메일 본문 캡처 완료: 저장 {saved_count}개, 실패 {failure_count}개, 건너뜀 {skipped_count}개")
        messagebox.showinfo(APP_TITLE, self.batch_result_message("메일 본문 캡처", result))

    def handle_point_request_result(self, result: PointRequestBatchResult) -> None:
        self.set_busy(False)
        self.status_var.set(f"포인트 신청 진행 완료: 사용자 확인 {len(result.completed)}개, 실패 {len(result.failures)}개")
        messagebox.showinfo(APP_TITLE, self.point_result_message(result))

    def point_result_message(self, result: PointRequestBatchResult) -> str:
        lines = [
            "포인트 신청 결과",
            "",
            f"사용자 확인: {len(result.completed)}개",
            f"실패: {len(result.failures)}개",
            f"건너뜀: {len(result.skipped)}개",
        ]
        if result.completed:
            lines.extend(["", "사용자 확인 대상:"])
            lines.extend(result.completed[:10])
            if len(result.completed) > 10:
                lines.append(f"...외 {len(result.completed) - 10}개")
        if result.failures:
            lines.extend(["", "실패:"])
            lines.extend(result.failures[:10])
            if len(result.failures) > 10:
                lines.append(f"...외 {len(result.failures) - 10}개")
        if result.skipped:
            lines.extend(["", "건너뜀:"])
            lines.extend(result.skipped[:10])
            if len(result.skipped) > 10:
                lines.append(f"...외 {len(result.skipped) - 10}개")
        return "\n".join(lines)

    def batch_result_message(self, title: str, result: BatchCaptureResult) -> str:
        lines = [
            f"{title} 결과",
            "",
            f"저장: {len(result.saved_paths)}개",
            f"실패: {len(result.failures)}개",
        ]
        if result.skipped is not None:
            lines.append(f"건너뜀: {len(result.skipped)}개")
        if result.saved_paths:
            lines.extend(["", "저장 파일:"])
            lines.extend(str(path) for path in result.saved_paths[:10])
            if len(result.saved_paths) > 10:
                lines.append(f"...외 {len(result.saved_paths) - 10}개")
        if result.failures:
            lines.extend(["", "실패:"])
            lines.extend(result.failures[:10])
            if len(result.failures) > 10:
                lines.append(f"...외 {len(result.failures) - 10}개")
        if result.skipped:
            lines.extend(["", "건너뜀:"])
            lines.extend(result.skipped[:10])
            if len(result.skipped) > 10:
                lines.append(f"...외 {len(result.skipped) - 10}개")
        return "\n".join(lines)

    def handle_result(self, result: CaptureResult) -> None:
        self.set_busy(False)
        if result.status == "captured" and result.saved_path:
            self.clear_candidates()
            self.update_email_state(result)
            self.status_var.set(f"댓글 저장 완료: {result.saved_path}")
            if result.email:
                messagebox.showinfo(
                    APP_TITLE,
                    f"댓글 캡처를 저장했습니다.\n\n{result.saved_path}\n\n찾은 이메일: {result.email}",
                )
            else:
                messagebox.showinfo(
                    APP_TITLE,
                    f"댓글 캡처를 저장했습니다.\n\n{result.saved_path}\n\n댓글에서 이메일을 찾지 못했습니다.",
                )
            return

        if result.status == "multiple" and result.candidates:
            self.clear_email_state()
            self.current_candidates = result.candidates
            self.current_match_mode = result.match_mode
            self.populate_candidates(result.candidates)
            mode_label = "정확 일치" if result.match_mode == "exact" else "부분 일치"
            self.status_var.set(f"{mode_label} 후보 {len(result.candidates)}개를 찾았습니다. 목록에서 캡처할 댓글을 더블클릭하세요.")
            return

        self.status_var.set("작업을 완료했지만 저장된 파일이 없습니다.")

    def handle_mail_result(self, result: MailCaptureResult) -> None:
        self.set_busy(False)
        if result.status == "captured" and result.saved_path:
            self.status_var.set(f"메일 본문 저장 완료: {result.saved_path}")
            messagebox.showinfo(APP_TITLE, f"메일 본문 캡처를 저장했습니다.\n\n{result.saved_path}")
            return

        if result.status == "multiple" and result.candidates:
            self.status_var.set(f"{result.email} 검색 결과 {len(result.candidates)}개를 찾았습니다. 메일을 선택하세요.")
            self.show_mail_choice_dialog(result.email, result.candidates)
            return

        self.status_var.set("메일 캡처를 완료했지만 저장된 파일이 없습니다.")

    def handle_error(self, details: str) -> None:
        self.set_busy(False)
        self.status_var.set("오류가 발생했습니다.")
        messagebox.showerror(APP_TITLE, details)

    def set_busy(self, busy: bool) -> None:
        self.is_busy = busy
        state = tk.DISABLED if busy else tk.NORMAL
        self.url_entry.configure(state=state)
        self.subject_keyword_entry.configure(state=state)
        self.output_dir_entry.configure(state=state)
        self.choose_folder_button.configure(state=state)
        self.start_button.configure(state=state)
        self.login_button.configure(state=state)
        self.open_folder_button.configure(state=state)
        if busy:
            self.comment_capture_button.configure(state=tk.DISABLED)
            self.email_capture_button.configure(state=tk.DISABLED)
            self.point_request_button.configure(state=tk.DISABLED)
            self.progress.start(12)
        else:
            self.progress.stop()
            target_state = tk.NORMAL if self.current_targets else tk.DISABLED
            self.comment_capture_button.configure(state=target_state)
            self.email_capture_button.configure(state=target_state)
            self.point_request_button.configure(state=target_state)

    def clear_candidates(self) -> None:
        self.current_candidates = []
        for item in self.tree.get_children():
            self.tree.delete(item)

    def clear_targets(self) -> None:
        self.current_targets = []
        self.mail_batch_targets = []
        self.mail_batch_index = 0
        self.mail_batch_saved_paths = []
        self.mail_batch_failures = []
        self.mail_batch_skipped = []
        self.last_toggled_target_iid = None
        for item in self.tree.get_children():
            self.tree.delete(item)
        self.comment_capture_button.configure(state=tk.DISABLED)
        self.email_capture_button.configure(state=tk.DISABLED)
        self.point_request_button.configure(state=tk.DISABLED)

    def checked_targets(self) -> list[dict[str, Any]]:
        return [target.copy() for target in self.current_targets if target.get("selected")]

    def target_by_iid(self, iid: str) -> dict[str, Any] | None:
        for target in self.current_targets:
            if str(target.get("row_id")) == str(iid):
                return target
        return None

    def on_target_tree_click(self, event: tk.Event) -> str | None:
        if self.is_busy:
            return None
        region = self.tree.identify("region", event.x, event.y)
        column = self.tree.identify_column(event.x)
        if region == "heading" and column == "#1":
            self.toggle_all_targets()
            return "break"
        if region != "cell":
            return None
        if column != "#1":
            return None
        iid = self.tree.identify_row(event.y)
        if iid:
            self.toggle_target(iid, extend=bool(event.state & 0x0001))
        return None

    def on_target_tree_space(self, event: tk.Event) -> str | None:
        if self.is_busy:
            return "break"
        selected = self.tree.selection()
        if selected:
            self.toggle_target(selected[0])
        return "break"

    def toggle_target(self, iid: str, extend: bool = False) -> None:
        target = self.target_by_iid(iid)
        if not target:
            return
        new_value = not bool(target.get("selected"))
        if extend and self.last_toggled_target_iid:
            changed = self.set_target_range_selected(self.last_toggled_target_iid, iid, new_value)
            if changed:
                self.update_selected_count_status()
                self.last_toggled_target_iid = iid
                return

        target["selected"] = new_value
        self.refresh_target_row(target)
        self.last_toggled_target_iid = iid
        self.update_selected_count_status()

    def set_target_range_selected(self, start_iid: str, end_iid: str, selected: bool) -> bool:
        iids = [str(target.get("row_id")) for target in self.current_targets]
        if start_iid not in iids or end_iid not in iids:
            return False
        start_index = iids.index(start_iid)
        end_index = iids.index(end_iid)
        if start_index > end_index:
            start_index, end_index = end_index, start_index
        for target in self.current_targets[start_index : end_index + 1]:
            target["selected"] = selected
            self.refresh_target_row(target)
        return True

    def toggle_all_targets(self) -> None:
        if not self.current_targets:
            return
        selected = not all(bool(target.get("selected")) for target in self.current_targets)
        for target in self.current_targets:
            target["selected"] = selected
            self.refresh_target_row(target)
        self.last_toggled_target_iid = None
        self.update_selected_count_status()

    def update_selected_count_status(self) -> None:
        checked_count = len([item for item in self.current_targets if item.get("selected")])
        self.status_var.set(f"캡처대상 {checked_count}개 선택됨")

    def refresh_target_row(self, target: dict[str, Any]) -> None:
        iid = str(target["row_id"])
        if not self.tree.exists(iid):
            return
        self.tree.item(
            iid,
            values=(
                "☑" if target.get("selected") else "☐",
                target.get("index", ""),
                target.get("nickname", ""),
                target.get("email", ""),
                target.get("date", ""),
                target.get("preview", ""),
            ),
        )

    def clear_email_state(self) -> None:
        self.last_found_email = None
        self.last_found_emails = []
        self.last_comment_text = ""
        self.last_comment_saved_path = None
        self.email_status_var.set("찾은 이메일: 없음")

    def update_email_state(self, result: CaptureResult) -> None:
        self.last_found_email = result.email
        self.last_found_emails = result.emails or []
        self.last_comment_text = result.comment_text or ""
        self.last_comment_saved_path = result.comment_saved_path or result.saved_path
        if result.email:
            self.email_var.set(result.email)
            if len(self.last_found_emails) > 1:
                all_emails = ", ".join(self.last_found_emails)
                self.email_status_var.set(f"찾은 이메일: {result.email} (전체: {all_emails})")
            else:
                self.email_status_var.set(f"찾은 이메일: {result.email}")
        else:
            self.email_status_var.set("찾은 이메일: 없음")

    def show_mail_choice_dialog(
        self,
        email: str,
        candidates: list[dict[str, Any]],
        on_select: Callable[[str], None] | None = None,
        on_cancel: Callable[[], None] | None = None,
    ) -> None:
        dialog = tk.Toplevel(self)
        dialog.title("메일 선택")
        dialog.geometry("820x420")
        dialog.minsize(720, 360)
        dialog.transient(self)
        dialog.grab_set()

        frame = ttk.Frame(dialog, padding=12)
        frame.pack(fill=tk.BOTH, expand=True)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(1, weight=1)

        ttk.Label(frame, text=f"'{email}' 검색 결과가 여러 개입니다. 캡처할 메일을 선택하세요.").grid(
            row=0, column=0, sticky=tk.EW, pady=(0, 8)
        )

        tree = ttk.Treeview(
            frame,
            columns=("mail_id", "sender", "subject", "date", "preview"),
            show="headings",
            selectmode="browse",
            height=10,
        )
        tree.heading("mail_id", text="mailId")
        tree.heading("sender", text="보낸 사람")
        tree.heading("subject", text="제목")
        tree.heading("date", text="날짜")
        tree.heading("preview", text="미리보기")
        tree.column("mail_id", width=90, stretch=False)
        tree.column("sender", width=130, stretch=False)
        tree.column("subject", width=220, stretch=True)
        tree.column("date", width=120, stretch=False)
        tree.column("preview", width=240, stretch=True)
        tree.grid(row=1, column=0, sticky=tk.NSEW)

        scrollbar = ttk.Scrollbar(frame, orient=tk.VERTICAL, command=tree.yview)
        tree.configure(yscrollcommand=scrollbar.set)
        scrollbar.grid(row=1, column=1, sticky=tk.NS)

        for candidate in candidates:
            tree.insert(
                "",
                tk.END,
                values=(
                    candidate.get("mail_id", ""),
                    candidate.get("sender", ""),
                    candidate.get("subject", ""),
                    candidate.get("date", ""),
                    candidate.get("preview", ""),
                ),
            )

        first = tree.get_children()
        if first:
            tree.selection_set(first[0])
            tree.focus(first[0])

        button_bar = ttk.Frame(frame)
        button_bar.grid(row=2, column=0, columnspan=2, sticky=tk.E, pady=(12, 0))

        def capture_selected() -> None:
            selected = tree.selection()
            if not selected:
                messagebox.showinfo(APP_TITLE, "캡처할 메일을 선택해 주세요.", parent=dialog)
                return
            mail_id = tree.set(selected[0], "mail_id")
            dialog.destroy()
            if on_select:
                on_select(mail_id)

        def cancel() -> None:
            dialog.destroy()
            if on_cancel:
                on_cancel()

        dialog.protocol("WM_DELETE_WINDOW", cancel)
        ttk.Button(button_bar, text="선택 메일 캡처", command=capture_selected).pack(side=tk.LEFT)
        ttk.Button(button_bar, text="취소", command=cancel).pack(side=tk.LEFT, padx=(8, 0))

    def populate_targets(self, targets: list[dict[str, Any]]) -> None:
        for item in self.tree.get_children():
            self.tree.delete(item)
        for target in targets:
            iid = str(target["row_id"])
            self.tree.insert(
                "",
                tk.END,
                iid=iid,
                values=(
                    "☑" if target.get("selected") else "☐",
                    target.get("index", ""),
                    target.get("nickname", ""),
                    target.get("email", ""),
                    target.get("date", ""),
                    target.get("preview", ""),
                ),
            )
        first = self.tree.get_children()
        if first:
            self.tree.selection_set(first[0])
            self.tree.focus(first[0])

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
