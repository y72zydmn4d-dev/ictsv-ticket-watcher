#!/usr/bin/env python3
"""Theo doi su kien moi tren CTSV HUST va gui thong bao qua Telegram.

Khong can cai thu vien ngoai: chi can Python 3.

Chay:
    python3 ictsv_web_watcher.py --check
    python3 ictsv_web_watcher.py --test-telegram
    python3 ictsv_web_watcher.py --once
    python3 ictsv_web_watcher.py

Luu y: file trang thai ``.ictsv_web_watcher_state.json`` se duoc tao canh
file nay sau lan goi API dau tien. Lan dau chi tao baseline, khong gui cac
su kien cu.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import ssl
import subprocess
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# CAU HINH: dien gia tri vao giua dau ngoac kep.
# Khong gui file da dien token cho nguoi khac va khong commit len GitHub.
# Bien moi truong cung ten se duoc uu tien neu duoc khai bao.
# ---------------------------------------------------------------------------
BKNEXUS_TOKEN = ""
TELEGRAM_BOT_TOKEN = ""
TELEGRAM_CHAT_ID = ""

# Cau hinh rieng cho trang cham diem ren luyen (/api-t).
# Lay TokenCode trong Payload va chuoi sau "Bearer " trong Request Headers.
TRAINING_TOKEN_CODE = ""
TRAINING_AUTH_TOKEN = ""
TRAINING_SEMESTER = ""  # De trong de tu dong dung hoc ky moi nhat.

# Doi ten nay neu Payload trong DevTools dung key khac, vi du TokenBKNexus.
TOKEN_FIELD = "Token"

# Thuong Payload cua request trinh duyet la Form Data. Neu DevTools hien
# Request Payload dang JSON, doi thanh "json".
PAYLOAD_MODE = "json"  # "form" hoac "json"

# Neu request goc co them cac truong bat buoc, them tai day.
# Vi du: {"Page": 1, "PageSize": 100}
EXTRA_PAYLOAD: dict[str, Any] = {}

POLL_SECONDS = 300
API_URL = "https://ctsv.hust.edu.vn/bknexus/Event/GetEvents"
TRAINING_API_BASE = "https://ctsv.hust.edu.vn/api-t/"
STATE_DIR = Path(os.getenv("STATE_DIR", str(Path(__file__).parent))).expanduser()
STATE_FILE = STATE_DIR / ".ictsv_web_watcher_state.json"
COMMAND_STATE_FILE = STATE_DIR / ".ictsv_telegram_command_state.json"
REQUEST_TIMEOUT = 30


def setting(name: str, file_value: str) -> str:
    """Lay bien moi truong neu co, neu khong dung gia tri trong file."""
    return os.getenv(name, file_value).strip()


def config() -> dict[str, Any]:
    mode = setting("PAYLOAD_MODE", PAYLOAD_MODE).lower()
    if mode not in {"form", "json"}:
        raise ValueError('PAYLOAD_MODE phai la "form" hoac "json".')

    extra = EXTRA_PAYLOAD.copy()
    raw_extra = os.getenv("BKNEXUS_EXTRA_PAYLOAD", "").strip()
    if raw_extra:
        parsed = json.loads(raw_extra)
        if not isinstance(parsed, dict):
            raise ValueError("BKNEXUS_EXTRA_PAYLOAD phai la mot JSON object.")
        extra.update(parsed)

    return {
        "bknexus_token": setting("BKNEXUS_TOKEN", BKNEXUS_TOKEN),
        "telegram_bot_token": setting("TELEGRAM_BOT_TOKEN", TELEGRAM_BOT_TOKEN),
        "telegram_chat_id": setting("TELEGRAM_CHAT_ID", TELEGRAM_CHAT_ID),
        "training_token_code": setting("TRAINING_TOKEN_CODE", TRAINING_TOKEN_CODE),
        "training_auth_token": setting("TRAINING_AUTH_TOKEN", TRAINING_AUTH_TOKEN),
        "training_semester": setting("TRAINING_SEMESTER", TRAINING_SEMESTER),
        "token_field": setting("TOKEN_FIELD", TOKEN_FIELD),
        "payload_mode": mode,
        "extra_payload": extra,
        "poll_seconds": int(os.getenv("POLL_SECONDS", str(POLL_SECONDS))),
    }


def missing_settings(cfg: dict[str, Any], *, need_api: bool, need_telegram: bool) -> list[str]:
    missing: list[str] = []
    if need_api and not cfg["bknexus_token"]:
        missing.append("BKNEXUS_TOKEN")
    if need_telegram and not cfg["telegram_bot_token"]:
        missing.append("TELEGRAM_BOT_TOKEN")
    if need_telegram and not cfg["telegram_chat_id"]:
        missing.append("TELEGRAM_CHAT_ID")
    return missing


def require_settings(cfg: dict[str, Any], *, need_api: bool, need_telegram: bool) -> None:
    missing = missing_settings(cfg, need_api=need_api, need_telegram=need_telegram)
    if missing:
        joined = ", ".join(missing)
        raise RuntimeError(
            f"Con thieu: {joined}. Hay mo file va dien cac gia tri trong muc CAU HINH o dau file."
        )


def request_json_with_macos_curl(request: urllib.request.Request) -> Any:
    """Dung kho chung chi macOS khi Python khong doc duoc chung chi he thong."""
    command = [
        "/usr/bin/curl",
        "--silent",
        "--show-error",
        "--fail-with-body",
        "--max-time",
        str(REQUEST_TIMEOUT),
        "--request",
        request.get_method(),
    ]
    for name, value in request.header_items():
        command.extend(["--header", f"{name}: {value}"])
    if request.data is not None:
        command.extend(["--data-binary", "@-"])
    command.append(request.full_url)

    try:
        completed = subprocess.run(
            command,
            input=request.data,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=REQUEST_TIMEOUT + 5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"Khong goi duoc HTTPS bang macOS: {exc}") from exc

    raw = completed.stdout.decode("utf-8", errors="replace")
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"Ket noi HTTPS macOS that bai: {detail or raw[:300]}")
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Server tra ve du lieu khong phai JSON.") from exc


def request_json(request: urllib.request.Request) -> Any:
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
            raw = response.read().decode("utf-8", errors="replace")
            if response.status < 200 or response.status >= 300:
                raise RuntimeError(f"HTTP {response.status}: {raw[:300]}")
            return json.loads(raw)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code}: {body[:500]}") from exc
    except urllib.error.URLError as exc:
        if sys.platform == "darwin" and isinstance(exc.reason, ssl.SSLCertVerificationError):
            return request_json_with_macos_curl(request)
        raise RuntimeError(f"Khong ket noi duoc: {exc.reason}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError("Server tra ve du lieu khong phai JSON.") from exc


def fetch_events(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    payload = dict(cfg["extra_payload"])
    payload[cfg["token_field"]] = cfg["bknexus_token"]

    if cfg["payload_mode"] == "json":
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        content_type = "application/json"
    else:
        body = urllib.parse.urlencode(payload).encode("utf-8")
        content_type = "application/x-www-form-urlencoded; charset=UTF-8"

    request = urllib.request.Request(
        API_URL,
        data=body,
        method="POST",
        headers={
            "Accept": "application/json, text/plain, */*",
            "Content-Type": content_type,
            "Origin": "https://ctsv.hust.edu.vn",
            "Referer": "https://ctsv.hust.edu.vn/",
            "User-Agent": "iCTSV-Web-Watcher/1.0",
        },
    )
    data = request_json(request)

    # Ho tro cac dang response pho bien: {Events: [...]}, {Data: {Events: [...]}}
    # hoac tra thang mot danh sach.
    candidates: list[Any] = [data]
    if isinstance(data, dict):
        candidates.extend(data.get(key) for key in ("Data", "data", "Result", "result"))

    for candidate in candidates:
        if isinstance(candidate, list):
            return [item for item in candidate if isinstance(item, dict)]
        if isinstance(candidate, dict):
            for key in ("Events", "events"):
                events = candidate.get(key)
                if isinstance(events, list):
                    return [item for item in events if isinstance(item, dict)]

    top_keys = ", ".join(data.keys()) if isinstance(data, dict) else type(data).__name__
    raise RuntimeError(f"Khong tim thay mang Events trong response. Du lieu cap cao: {top_keys}")


def event_id(event: dict[str, Any]) -> str | None:
    for key in ("Id", "ID", "id", "EventId", "EventID", "eventId"):
        value = event.get(key)
        if value is not None and str(value).strip():
            return str(value)
    return None


def first_value(event: dict[str, Any], *keys: str, default: str = "-") -> str:
    for key in keys:
        value = event.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return default


def integer_value(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return None


def boolean_value(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "open"}


def event_snapshot(event: dict[str, Any]) -> dict[str, Any]:
    capacity = integer_value(event.get("Capacity"))
    registered = integer_value(event.get("Registered"))
    remaining = integer_value(event.get("Remaining"))
    if remaining is None and capacity is not None and registered is not None:
        remaining = max(capacity - registered, 0)
    return {
        "title": first_value(event, "Title", "title", "Name", "name"),
        "state": first_value(event, "State", default=""),
        "is_open": boolean_value(event.get("IsOpen")),
        "remaining": remaining,
        "capacity": capacity,
        "registered": registered,
        "start_time": first_value(event, "StartTime", default=""),
    }


def availability_change(old: dict[str, Any], new: dict[str, Any]) -> str | None:
    if str(new.get("state", "")).upper() == "ENDED":
        return None
    old_remaining = integer_value(old.get("remaining"))
    new_remaining = integer_value(new.get("remaining"))
    if not boolean_value(old.get("is_open")) and boolean_value(new.get("is_open")):
        return "Sự kiện vừa mở đăng ký"
    if new_remaining is not None and new_remaining > 0:
        if old_remaining is not None and old_remaining <= 0:
            return "Vừa có chỗ trống trở lại"
        if old_remaining is not None and new_remaining > old_remaining:
            return f"Số chỗ trống tăng từ {old_remaining} lên {new_remaining}"
    return None


def format_event(event: dict[str, Any]) -> str:
    eid = event_id(event) or "-"
    title = first_value(event, "Title", "title", "Name", "name")
    location = first_value(event, "Location", "location", "Place", "place", "Address")
    start = first_value(event, "StartTime", "StartDate", "FromDate", "BeginTime", "Time")
    end = first_value(event, "EndTime", "EndDate", "ToDate", "FinishTime")
    remaining = first_value(event, "Remaining", "remaining", "Remain")
    capacity = first_value(event, "Capacity", "capacity", "MaxParticipants")
    is_open = first_value(event, "IsOpen", "isOpen", "Open")

    return "\n".join(
        [
            "🔔 Sự kiện CTSV mới",
            f"Tiêu đề: {title}",
            f"Địa điểm: {location}",
            f"Bắt đầu: {start}",
            f"Kết thúc: {end}",
            f"Còn lại: {remaining}",
            f"Sức chứa: {capacity}",
            f"Đang mở: {is_open}",
            f"ID: {eid}",
        ]
    )


def format_availability_alert(event: dict[str, Any], reason: str) -> str:
    remaining = first_value(event, "Remaining", "remaining", "Remain")
    capacity = first_value(event, "Capacity", "capacity", "MaxParticipants")
    return "\n".join(
        [
            "🚨 VÉ CTSV CÓ THAY ĐỔI",
            f"Lý do: {reason}",
            f"Sự kiện: {first_value(event, 'Title', 'Name')}",
            f"Thời gian: {first_value(event, 'StartTime')} – {first_value(event, 'EndTime')}",
            f"Địa điểm: {first_value(event, 'Location', 'Place')}",
            f"Còn lại: {remaining} / {capacity}",
            f"Đăng ký: {'Đang mở' if boolean_value(event.get('IsOpen')) else 'Đã đóng'}",
            "Mở trang đặt vé ngay: https://ctsv.hust.edu.vn/dat-ve",
        ]
    )


def send_telegram(cfg: dict[str, Any], text: str) -> None:
    url = f"https://api.telegram.org/bot{cfg['telegram_bot_token']}/sendMessage"
    body = urllib.parse.urlencode(
        {"chat_id": cfg["telegram_chat_id"], "text": text, "disable_web_page_preview": "true"}
    ).encode("utf-8")
    request = urllib.request.Request(url, data=body, method="POST")
    result = request_json(request)
    if not isinstance(result, dict) or not result.get("ok"):
        raise RuntimeError(f"Telegram tu choi tin nhan: {result}")


def get_telegram_updates(cfg: dict[str, Any], offset: int | None) -> list[dict[str, Any]]:
    url = f"https://api.telegram.org/bot{cfg['telegram_bot_token']}/getUpdates"
    params: dict[str, Any] = {
        "timeout": 10,
        "allowed_updates": json.dumps(["message"]),
    }
    if offset is not None:
        params["offset"] = offset
    body = urllib.parse.urlencode(params).encode("utf-8")
    request = urllib.request.Request(url, data=body, method="POST")
    result = request_json(request)
    if not isinstance(result, dict) or not result.get("ok"):
        raise RuntimeError(f"Telegram tu choi yeu cau doc lenh: {result}")
    updates = result.get("result", [])
    return [item for item in updates if isinstance(item, dict)]


def load_telegram_offset() -> int | None:
    if not COMMAND_STATE_FILE.exists():
        return None
    try:
        data = json.loads(COMMAND_STATE_FILE.read_text(encoding="utf-8"))
        value = data.get("next_update_id")
        return int(value) if value is not None else None
    except (OSError, ValueError, TypeError, json.JSONDecodeError, AttributeError):
        return None


def save_telegram_offset(offset: int) -> None:
    data = {
        "next_update_id": offset,
        "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    COMMAND_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = COMMAND_STATE_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(COMMAND_STATE_FILE)


def normalize_command(text: str) -> str:
    normalized = unicodedata.normalize("NFD", text.strip().lower())
    normalized = "".join(char for char in normalized if unicodedata.category(char) != "Mn")
    normalized = normalized.replace("đ", "d")
    return " ".join(normalized.split())


def format_ticket(event: dict[str, Any]) -> str:
    ticket = event.get("MyTicket") if isinstance(event.get("MyTicket"), dict) else {}
    checked_in = bool(ticket.get("CheckedIn"))
    lines = [
        "🎟 VÉ SỰ KIỆN SẮP TỚI",
        f"Sự kiện: {first_value(event, 'Title', 'Name')}",
        f"Thời gian: {first_value(event, 'StartTime')} – {first_value(event, 'EndTime')}",
        f"Địa điểm: {first_value(event, 'Location', 'Place')}",
    ]
    group_name = first_value(event, "GroupName", default="")
    if group_name:
        lines.append(f"Nhóm: {group_name}")
    lines.extend(
        [
            f"Mã vé: {ticket.get('TicketCode') or '-'}",
            f"Số thứ tự: {ticket.get('SeatNo') if ticket.get('SeatNo') is not None else '-'}",
            f"Đăng ký lúc: {ticket.get('TimeCreate') or '-'}",
            f"Trạng thái: {'Đã điểm danh' if checked_in else 'Chưa sử dụng'}",
            "Trang vé: https://ctsv.hust.edu.vn/dat-ve",
        ]
    )
    return "\n".join(lines)


def send_upcoming_tickets(cfg: dict[str, Any]) -> None:
    events = fetch_events(cfg)
    tickets = [
        event
        for event in events
        if isinstance(event.get("MyTicket"), dict) and str(event.get("State", "")).upper() != "ENDED"
    ]
    tickets.sort(key=lambda event: str(event.get("StartTime") or "9999"))
    if not tickets:
        send_telegram(cfg, "Bạn hiện không có vé cho sự kiện sắp tới.")
        return

    send_telegram(cfg, f"Bạn có {len(tickets)} vé cho sự kiện sắp tới:")
    for event in tickets:
        send_telegram(cfg, format_ticket(event))


def format_current_ticket_event(event: dict[str, Any]) -> str:
    capacity = event.get("Capacity")
    registered = event.get("Registered")
    remaining = event.get("Remaining")
    state_text = first_value(event, "StateText", "State")
    my_ticket = event.get("MyTicket") if isinstance(event.get("MyTicket"), dict) else None
    lines = [
        "🎫 SỰ KIỆN ĐẶT VÉ HIỆN TẠI",
        f"Sự kiện: {first_value(event, 'Title', 'Name')}",
        f"Thời gian: {first_value(event, 'StartTime')} – {first_value(event, 'EndTime')}",
        f"Địa điểm: {first_value(event, 'Location', 'Place')}",
    ]
    group_name = first_value(event, "GroupName", default="")
    if group_name:
        lines.append(f"Nhóm: {group_name}")
    lines.extend(
        [
            f"Số chỗ: {registered if registered is not None else '-'} / {capacity if capacity is not None else '-'}",
            f"Còn lại: {remaining if remaining is not None else '-'}",
            f"Trạng thái: {state_text}",
            f"Đăng ký: {'Đang mở' if event.get('IsOpen') else 'Đã đóng'}",
            (
                f"Vé của tôi: {my_ticket.get('TicketCode') or 'Đã có vé'}"
                if my_ticket
                else "Vé của tôi: Chưa có"
            ),
            "Trang đặt vé: https://ctsv.hust.edu.vn/dat-ve",
        ]
    )
    return "\n".join(lines)


def send_current_ticket_events(cfg: dict[str, Any]) -> None:
    events = fetch_events(cfg)
    current = [event for event in events if str(event.get("State", "")).upper() != "ENDED"]
    current.sort(key=lambda event: str(event.get("StartTime") or "9999"))
    if not current:
        send_telegram(cfg, "Hiện web không hiển thị sự kiện đặt vé sắp tới nào.")
        return

    send_telegram(cfg, f"Web hiện có {len(current)} sự kiện đặt vé sắp tới:")
    for event in current:
        send_telegram(cfg, format_current_ticket_event(event))


def training_missing(cfg: dict[str, Any]) -> list[str]:
    missing: list[str] = []
    if not cfg["training_token_code"]:
        missing.append("TRAINING_TOKEN_CODE")
    if not cfg["training_auth_token"]:
        missing.append("TRAINING_AUTH_TOKEN")
    if not str(cfg["extra_payload"].get("UserName") or "").strip():
        missing.append("UserName trong EXTRA_PAYLOAD")
    return missing


def post_training_api(cfg: dict[str, Any], endpoint: str, data: dict[str, Any] | None = None) -> Any:
    missing = training_missing(cfg)
    if missing:
        raise RuntimeError("Chua cau hinh: " + ", ".join(missing))

    payload = dict(data or {})
    payload["TokenCode"] = cfg["training_token_code"]
    payload["UserName"] = str(cfg["extra_payload"]["UserName"])
    auth_token = cfg["training_auth_token"]
    if auth_token.lower().startswith("bearer "):
        auth_token = auth_token[7:].strip()
    request = urllib.request.Request(
        TRAINING_API_BASE + endpoint.lstrip("/"),
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers={
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {auth_token}",
            "Origin": "https://ctsv.hust.edu.vn",
            "Referer": "https://ctsv.hust.edu.vn/",
        },
    )
    result = request_json(request)
    if not isinstance(result, dict):
        raise RuntimeError("API diem ren luyen tra ve du lieu khong hop le.")
    if result.get("RespCode") not in (None, 0):
        raise RuntimeError(str(result.get("RespText") or "API diem ren luyen tu choi yeu cau."))
    return result


def fetch_training_data(cfg: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    semester = cfg["training_semester"]
    if not semester:
        semester_result = post_training_api(cfg, "Point/GetSemester")
        semesters = semester_result.get("SemesterLst", [])
        valid = [item for item in semesters if isinstance(item, dict) and item.get("Scode")]
        if not valid:
            raise RuntimeError("Khong tim thay hoc ky diem ren luyen.")
        semester = str(valid[-1]["Scode"])

    user_code = str(cfg["extra_payload"]["UserName"])
    result = post_training_api(
        cfg,
        "Criteria/GetCriteriaTypeDetails",
        {"UserCode": user_code, "Semester": semester},
    )
    details = result.get("CriteriaTypeDetailsLst", [])
    return semester, [item for item in details if isinstance(item, dict)]


def number(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def display_number(value: Any) -> str:
    numeric = number(value)
    return str(int(numeric)) if numeric.is_integer() else str(numeric)


def iter_training_criteria(details: list[dict[str, Any]]):
    for criteria_type in details:
        type_name = str(criteria_type.get("CTName") or "Nhóm điểm")
        for group in criteria_type.get("CriteriaGroupDetailsLst", []) or []:
            if not isinstance(group, dict):
                continue
            group_name = str(group.get("CGName") or type_name)
            for criterion in group.get("UserCriteriaDetailsLst", []) or []:
                if isinstance(criterion, dict):
                    yield type_name, group_name, criterion


def criterion_evidence(criterion: dict[str, Any]) -> list[str]:
    evidence: list[str] = []
    description = str(criterion.get("UCDes") or "").strip()
    if description:
        evidence.append(description)
    for activity in criterion.get("UserCriteriaActivityLst", []) or []:
        if not isinstance(activity, dict):
            continue
        name = first_value(activity, "AName", "Title", "Name", default="")
        if name and name not in evidence:
            evidence.append(name)
    return evidence


def send_telegram_chunks(cfg: dict[str, Any], heading: str, entries: list[str]) -> None:
    chunks: list[str] = []
    current = heading
    for entry in entries:
        addition = "\n\n" + entry
        if len(current) + len(addition) > 3500:
            chunks.append(current)
            current = heading + " (tiếp)" + addition
        else:
            current += addition
    chunks.append(current)
    for chunk in chunks:
        send_telegram(cfg, chunk)


def send_training_summary(cfg: dict[str, Any], *, preview: bool = False) -> None:
    semester, details = fetch_training_data(cfg)
    student_total = sum(number(item.get("CTPoint")) for item in details)
    teacher_total = sum(number(item.get("TCTPoint")) for item in details)
    scored = []
    evidence_count = 0
    for _type_name, _group_name, criterion in iter_training_criteria(details):
        if number(criterion.get("UCPoint")) > 0 or number(criterion.get("TCPoint")) > 0:
            scored.append(criterion)
        if criterion_evidence(criterion):
            evidence_count += 1

    title = "🧮 XEM TRƯỚC CHẤM ĐIỂM RÈN LUYỆN" if preview else "📊 ĐIỂM RÈN LUYỆN"
    lines = [
        title,
        f"Học kỳ: {semester}",
        f"Tổng điểm SV tự chấm: {display_number(student_total)}",
        f"Tổng điểm GV chấm: {display_number(teacher_total)}",
        f"Số mục đã có điểm: {len(scored)}",
        f"Số mục có minh chứng: {evidence_count}",
    ]
    if preview:
        lines.append("Đây chỉ là bản xem trước; bot không thay đổi hoặc lưu điểm lên hệ thống.")
    send_telegram(cfg, "\n".join(lines))


def send_scored_training_items(cfg: dict[str, Any]) -> None:
    semester, details = fetch_training_data(cfg)
    entries: list[str] = []
    for _type_name, group_name, criterion in iter_training_criteria(details):
        student_point = number(criterion.get("UCPoint"))
        teacher_point = number(criterion.get("TCPoint"))
        if student_point <= 0 and teacher_point <= 0:
            continue
        name = first_value(criterion, "CName", "Name")
        maximum = display_number(criterion.get("CMaxPoint"))
        evidence = criterion_evidence(criterion)
        entry = (
            f"• {name}\n"
            f"  Nhóm: {group_name}\n"
            f"  SV: {display_number(student_point)} · GV: {display_number(teacher_point)} · Tối đa: {maximum}"
        )
        if evidence:
            entry += f"\n  Minh chứng: {'; '.join(evidence)}"
        entries.append(entry)
    if not entries:
        send_telegram(cfg, f"Học kỳ {semester} chưa có mục rèn luyện nào được chấm điểm.")
        return
    send_telegram_chunks(cfg, f"✅ CÁC MỤC ĐÃ CÓ ĐIỂM — {semester} ({len(entries)} mục)", entries)


def send_training_evidence(cfg: dict[str, Any]) -> None:
    semester, details = fetch_training_data(cfg)
    entries: list[str] = []
    for _type_name, group_name, criterion in iter_training_criteria(details):
        evidence = criterion_evidence(criterion)
        if not evidence:
            continue
        name = first_value(criterion, "CName", "Name")
        entries.append(
            f"• {name}\n"
            f"  Nhóm: {group_name}\n"
            f"  Điểm SV: {display_number(criterion.get('UCPoint'))}\n"
            f"  Minh chứng: {'; '.join(evidence)}"
        )
    if not entries:
        send_telegram(cfg, f"Học kỳ {semester} chưa có mục nào kèm minh chứng.")
        return
    send_telegram_chunks(cfg, f"📎 MINH CHỨNG RÈN LUYỆN — {semester} ({len(entries)} mục)", entries)


def training_ready_or_notify(cfg: dict[str, Any]) -> bool:
    missing = training_missing(cfg)
    if not missing:
        return True
    send_telegram(
        cfg,
        "Chưa cấu hình phần điểm rèn luyện. Cần điền TRAINING_TOKEN_CODE và "
        "TRAINING_AUTH_TOKEN ở đầu file watcher.",
    )
    return False


def process_telegram_commands(cfg: dict[str, Any], offset: int | None) -> int | None:
    updates = get_telegram_updates(cfg, offset)
    next_offset = offset
    for update in updates:
        update_id = update.get("update_id")
        if isinstance(update_id, int):
            next_offset = max(next_offset or 0, update_id + 1)

        message = update.get("message")
        if not isinstance(message, dict):
            continue
        chat = message.get("chat")
        if not isinstance(chat, dict) or str(chat.get("id")) != cfg["telegram_chat_id"]:
            continue
        text = message.get("text")
        if not isinstance(text, str):
            continue

        command = normalize_command(text)
        if command in {"kiem tra dat ve", "/ve", "/ticket"}:
            send_upcoming_tickets(cfg)
        elif command in {
            "kiem tra ve hien tai",
            "kiem tra su kien dat ve",
            "kiem tra su kien",
            "/hientai",
            "/sukien",
            "/events",
        }:
            send_current_ticket_events(cfg)
        elif command in {"kiem tra diem ren luyen", "diem ren luyen", "/drl"}:
            if training_ready_or_notify(cfg):
                send_training_summary(cfg)
        elif command in {
            "kiem tra muc co diem",
            "kiem tra cac loai diem ren luyen",
            "cac muc da co diem",
            "/diem",
        }:
            if training_ready_or_notify(cfg):
                send_scored_training_items(cfg)
        elif command in {"kiem tra minh chung", "minh chung ren luyen", "/minhchung"}:
            if training_ready_or_notify(cfg):
                send_training_evidence(cfg)
        elif command in {"cham diem ren luyen", "/chamdiem"}:
            if training_ready_or_notify(cfg):
                send_training_summary(cfg, preview=True)
        elif command == "/start":
            send_telegram(
                cfg,
                "Bot đang hoạt động.\n"
                "• Gửi “Kiểm tra đặt vé” hoặc /ve để xem vé bạn đã sở hữu.\n"
                "• Gửi “Kiểm tra vé hiện tại” hoặc /hientai để xem các sự kiện đang hiển thị trên web.\n"
                "• /drl: tổng điểm rèn luyện.\n"
                "• /diem: các mục đã có điểm.\n"
                "• /minhchung: các mục đã có minh chứng.\n"
                "• /chamdiem: xem trước chấm điểm, không lưu lên hệ thống.",
            )

    if next_offset is not None and next_offset != offset:
        save_telegram_offset(next_offset)
    return next_offset


def load_event_state() -> dict[str, Any] | None:
    if not STATE_FILE.exists():
        return None
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        ids = {str(item) for item in data.get("seen_ids", [])}
        snapshots = data.get("event_snapshots", {})
        if not isinstance(snapshots, dict):
            snapshots = {}
        return {"seen_ids": ids, "event_snapshots": snapshots}
    except (OSError, json.JSONDecodeError, AttributeError) as exc:
        raise RuntimeError(f"Khong doc duoc file trang thai {STATE_FILE}: {exc}") from exc


def save_event_state(ids: set[str], snapshots: dict[str, dict[str, Any]]) -> None:
    existing = load_event_state()
    if (
        existing is not None
        and existing["seen_ids"] == ids
        and existing["event_snapshots"] == snapshots
    ):
        return
    data = {
        "seen_ids": sorted(ids),
        "event_snapshots": snapshots,
        "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = STATE_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(STATE_FILE)


def check_once(cfg: dict[str, Any]) -> None:
    events = fetch_events(cfg)
    valid_events = [(eid, event) for event in events if (eid := event_id(event)) is not None]
    current_ids = {eid for eid, _ in valid_events}
    current_snapshots = {eid: event_snapshot(event) for eid, event in valid_events}
    state = load_event_state()

    if state is None:
        save_event_state(current_ids, current_snapshots)
        print(f"[BASELINE] Đã lưu {len(current_ids)} sự kiện hiện tại. Không gửi sự kiện cũ.")
        return

    seen = state["seen_ids"]
    old_snapshots = state["event_snapshots"]
    new_events = [
        (eid, event)
        for eid, event in valid_events
        if eid not in seen and str(event.get("State", "")).upper() != "ENDED"
    ]
    changed_events: list[tuple[str, dict[str, Any], str]] = []
    for eid, event in valid_events:
        if eid not in seen or eid not in old_snapshots:
            continue
        reason = availability_change(old_snapshots[eid], current_snapshots[eid])
        if reason:
            changed_events.append((eid, event, reason))

    if new_events:
        print(f"[NEW] Tìm thấy {len(new_events)} sự kiện đặt vé mới.")
    for eid, event in new_events:
        send_telegram(cfg, format_event(event))
        print(f"[SENT] Đã gửi sự kiện ID={eid}.")

    for eid, event, reason in changed_events:
        send_telegram(cfg, format_availability_alert(event, reason))
        print(f"[SENT] Đã gửi thay đổi vé ID={eid}: {reason}.")

    merged_snapshots = dict(old_snapshots)
    merged_snapshots.update(current_snapshots)
    save_event_state(seen | current_ids, merged_snapshots)
    if not new_events and not changed_events:
        print(
            f"[OK] {datetime.now():%Y-%m-%d %H:%M:%S} - "
            f"Không có vé mới hoặc thay đổi chỗ ({len(events)} sự kiện)."
        )


def print_check(cfg: dict[str, Any]) -> None:
    print("Trạng thái cấu hình:")
    print(f"  BKNEXUS_TOKEN: {'đã có' if cfg['bknexus_token'] else 'CHƯA CÓ'}")
    print(f"  TELEGRAM_BOT_TOKEN: {'đã có' if cfg['telegram_bot_token'] else 'CHƯA CÓ'}")
    print(f"  TELEGRAM_CHAT_ID: {'đã có' if cfg['telegram_chat_id'] else 'CHƯA CÓ'}")
    print(f"  TRAINING_TOKEN_CODE: {'đã có' if cfg['training_token_code'] else 'CHƯA CÓ'}")
    print(f"  TRAINING_AUTH_TOKEN: {'đã có' if cfg['training_auth_token'] else 'CHƯA CÓ'}")
    print(f"  TRAINING_SEMESTER: {cfg['training_semester'] or 'tự động chọn mới nhất'}")
    print(f"  TOKEN_FIELD: {cfg['token_field']}")
    print(f"  PAYLOAD_MODE: {cfg['payload_mode']}")
    print(f"  Chu kỳ kiểm tra: {cfg['poll_seconds']} giây")
    print(f"  File trạng thái: {STATE_FILE}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Theo dõi sự kiện CTSV HUST và báo qua Telegram.")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--check", action="store_true", help="kiểm tra cấu hình, không gọi mạng")
    group.add_argument("--test-telegram", action="store_true", help="gửi một tin nhắn thử")
    group.add_argument("--test-event", action="store_true", help="gửi một sự kiện giả lập qua Telegram")
    group.add_argument(
        "--test-ticket-alert",
        action="store_true",
        help="gửi thông báo giả lập khi vé có chỗ trở lại",
    )
    group.add_argument("--once", action="store_true", help="gọi CTSV một lần rồi thoát")
    args = parser.parse_args()

    try:
        cfg = config()
        if args.check:
            print_check(cfg)
            return 0

        if args.test_telegram:
            require_settings(cfg, need_api=False, need_telegram=True)
            send_telegram(cfg, "✅ iCTSV Web Watcher đã kết nối Telegram thành công.")
            print("[OK] Đã gửi tin nhắn test Telegram.")
            return 0

        if args.test_event:
            require_settings(cfg, need_api=False, need_telegram=True)
            sample_event = {
                "Id": "TEST-NOT-A-REAL-EVENT",
                "Title": "Sự kiện kiểm tra iCTSV Watcher",
                "Location": "Đây là thông báo giả lập",
                "StartTime": "2026-09-25 19:00",
                "EndTime": "2026-09-25 20:00",
                "Remaining": 42,
                "Capacity": 100,
                "IsOpen": True,
            }
            send_telegram(cfg, format_event(sample_event))
            print("[OK] Đã gửi sự kiện giả lập. Baseline không bị thay đổi.")
            return 0

        if args.test_ticket_alert:
            require_settings(cfg, need_api=False, need_telegram=True)
            sample_event = {
                "Title": "Sự kiện kiểm tra vé CTSV",
                "Location": "Thông báo giả lập, không phải vé thật",
                "StartTime": "2026-09-26 08:00",
                "EndTime": "2026-09-26 17:00",
                "Remaining": 1,
                "Capacity": 30,
                "IsOpen": True,
            }
            send_telegram(cfg, format_availability_alert(sample_event, "Vừa có chỗ trống trở lại"))
            print("[OK] Đã gửi cảnh báo vé giả lập. Baseline không bị thay đổi.")
            return 0

        require_settings(cfg, need_api=True, need_telegram=True)
        if args.once:
            check_once(cfg)
            return 0

    except (RuntimeError, ValueError, OSError) as exc:
        print(f"[LỖI] {exc}", file=sys.stderr)
        return 1

    stopped = False

    def stop(_signum: int, _frame: Any) -> None:
        nonlocal stopped
        stopped = True

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    telegram_offset = load_telegram_offset()
    print(f"[START] Đang theo dõi mỗi {cfg['poll_seconds']} giây. Nhấn Ctrl+C để dừng.")
    print('[BOT] Lệnh Telegram: “Kiểm tra đặt vé” (/ve) hoặc “Kiểm tra vé hiện tại” (/hientai).')
    print("[BOT] Điểm rèn luyện: /drl, /diem, /minhchung, /chamdiem (chỉ xem trước).")

    while not stopped:
        try:
            check_once(cfg)
        except Exception as exc:  # Giữ watcher tiếp tục chạy khi mạng/API lỗi tạm thời.
            print(f"[LỖI] {datetime.now():%Y-%m-%d %H:%M:%S} - {exc}", file=sys.stderr)

        deadline = time.monotonic() + cfg["poll_seconds"]
        while not stopped and time.monotonic() < deadline:
            try:
                telegram_offset = process_telegram_commands(cfg, telegram_offset)
            except Exception as exc:
                print(f"[LỖI TELEGRAM] {datetime.now():%Y-%m-%d %H:%M:%S} - {exc}", file=sys.stderr)
                time.sleep(3)

    print("\n[STOP] Đã dừng watcher.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
