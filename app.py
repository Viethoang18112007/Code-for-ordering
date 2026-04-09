"""
Group order MVP — Flask + SQLite.
Chạy: py app.py  →  http://127.0.0.1:5000 (hoặc http://<IP-LAN>:5000 để chia sẻ trong mạng)

URL công khai (thay https://example.com bằng domain thật nơi bạn host app):
- / — xem danh sách nhóm đang mở, chọn để tham gia.
- /creator — form thông tin cá nhân + tạo phiên; sau đó /creator/session/… để quản lý.
- /joiner/(mã) — người tham gia; /joiner/(mã)/p/(id) — trang cá nhân.
- Đặt PUBLIC_BASE_URL nếu có domain cố định; hoặc link_base trên từng phiên.
"""

from __future__ import annotations

import json
import os
import secrets
import string
import sqlite3
import uuid
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from urllib.parse import quote

from flask import (
    Flask,
    abort,
    flash,
    g,
    redirect,
    render_template,
    request,
    session as flask_session,
    url_for,
)

APP_DIR = Path(__file__).resolve().parent
INSTANCE = APP_DIR / "instance"
DB_PATH = INSTANCE / "group_order.db"

ADMIN_PASSWORD = os.environ.get("GROUP_ORDER_ADMIN_PASSWORD", "demo123")
SECRET_KEY = os.environ.get("FLASK_SECRET_KEY", "dev-change-me-group-order")
# Base URL dùng khi tạo link chia sẻ (không có dấu / cuối). Ưu tiên thấp hơn link_base_url từng phiên.
PUBLIC_BASE_URL = (os.environ.get("PUBLIC_BASE_URL") or os.environ.get("GROUP_ORDER_PUBLIC_BASE_URL") or "").strip().rstrip("/")

# Mã trong URL công khai: đúng 13 ký tự chữ/số ASCII (a-z A-Z 0-9), ví dụ https://example.com/joiner/Ab3xYz9...
ORDER_CODE_LENGTH = 13
_ORDER_CHARSET = string.ascii_letters + string.digits

app = Flask(__name__)
app.secret_key = SECRET_KEY


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        INSTANCE.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        g.db = conn
    return g.db


@app.teardown_appcontext
def close_db(_exc=None) -> None:
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def init_db() -> None:
    db = get_db()
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY,
            slug TEXT NOT NULL UNIQUE,
            title TEXT NOT NULL DEFAULT '',
            brand TEXT NOT NULL DEFAULT '',
            address TEXT NOT NULL DEFAULT '',
            scheduled_at TEXT,
            deadline_at TEXT,
            status TEXT NOT NULL DEFAULT 'open',
            proof_note TEXT,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS participants (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
            display_name TEXT NOT NULL,
            items_json TEXT NOT NULL DEFAULT '[]',
            paid_at TEXT,
            agreed_at TEXT,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_participants_session
        ON participants(session_id);
        """
    )
    db.commit()
    _ensure_session_columns(db)


@app.before_request
def before_request() -> None:
    if not app.config.get("_db_ready"):
        init_db()
        app.config["_db_ready"] = True
    get_db()


def admin_ok() -> bool:
    return bool(flask_session.get("admin"))


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not admin_ok():
            flash("Cần đăng nhập admin.", "error")
            return redirect(url_for("admin_login"))
        return view(*args, **kwargs)

    return wrapped


def is_standard_order_code(slug: str) -> bool:
    """True nếu slug là mã đặt hàng chuẩn (13 ký tự chữ/số)."""
    if len(slug) != ORDER_CODE_LENGTH:
        return False
    return all(c in _ORDER_CHARSET for c in slug)


def join_public_path(slug: str) -> str:
    """Đường dẫn tới trang tham gia: /joiner/(mã hoặc slug)."""
    return f"/joiner/{quote(slug, safe='-_.~')}"


def participant_path(slug: str, participant_id: str) -> str:
    return f"{join_public_path(slug)}/p/{participant_id}"


def generate_unique_order_code(db: sqlite3.Connection) -> str:
    for _ in range(128):
        code = "".join(secrets.choice(_ORDER_CHARSET) for _ in range(ORDER_CODE_LENGTH))
        exists = db.execute("SELECT 1 FROM sessions WHERE slug = ?", (code,)).fetchone()
        if not exists:
            return code
    raise RuntimeError("Không tạo được mã đặt hàng duy nhất")


def _normalize_link_base(raw: str | None) -> str | None:
    s = (raw or "").strip()
    if not s:
        return None
    return s.rstrip("/")


def public_base_for_session(sess: dict | sqlite3.Row | None) -> str:
    """Base URL for shared links (no trailing slash)."""
    if sess is not None:
        row = dict(sess) if not isinstance(sess, dict) else sess
        lb = row.get("link_base_url")
        if lb is not None and str(lb).strip():
            return str(lb).strip().rstrip("/")
    if PUBLIC_BASE_URL:
        return PUBLIC_BASE_URL
    return request.url_root.rstrip("/")


def _ensure_session_columns(db: sqlite3.Connection) -> None:
    cols = {r[1] for r in db.execute("PRAGMA table_info(sessions)").fetchall()}
    if "host_token" not in cols:
        db.execute("ALTER TABLE sessions ADD COLUMN host_token TEXT")
    if "link_base_url" not in cols:
        db.execute("ALTER TABLE sessions ADD COLUMN link_base_url TEXT")
    if "creator_name" not in cols:
        db.execute("ALTER TABLE sessions ADD COLUMN creator_name TEXT NOT NULL DEFAULT ''")
    if "creator_phone" not in cols:
        db.execute("ALTER TABLE sessions ADD COLUMN creator_phone TEXT NOT NULL DEFAULT ''")
    db.commit()
    pcols = {r[1] for r in db.execute("PRAGMA table_info(participants)").fetchall()}
    if "phone" not in pcols:
        db.execute("ALTER TABLE participants ADD COLUMN phone TEXT")
    db.commit()
    for row in db.execute(
        "SELECT id FROM sessions WHERE host_token IS NULL OR host_token = ''"
    ).fetchall():
        db.execute(
            "UPDATE sessions SET host_token = ? WHERE id = ?",
            (secrets.token_urlsafe(16), row["id"]),
        )
    db.commit()


def parse_items(raw: str) -> list[dict]:
    try:
        data = json.loads(raw or "[]")
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    out = []
    for row in data:
        if not isinstance(row, dict):
            continue
        name = str(row.get("name", "")).strip()
        qty = row.get("qty", 1)
        unit = row.get("unit_price", 0)
        try:
            qty_i = int(qty)
        except (TypeError, ValueError):
            qty_i = 1
        try:
            unit_f = float(unit)
        except (TypeError, ValueError):
            unit_f = 0.0
        if qty_i < 1:
            qty_i = 1
        if name:
            out.append({"name": name, "qty": qty_i, "unit_price": round(unit_f, 2)})
    return out


def items_total(items: list[dict]) -> float:
    t = 0.0
    for it in items:
        t += float(it["qty"]) * float(it["unit_price"])
    return round(t, 2)


def build_session_manage_view_context(session_id: str) -> dict | None:
    db = get_db()
    srow = db.execute(
        "SELECT * FROM sessions WHERE id = ?", (session_id,)
    ).fetchone()
    if not srow:
        return None
    sess = dict(srow)

    parts = db.execute(
        "SELECT * FROM participants WHERE session_id = ? ORDER BY datetime(created_at)",
        (session_id,),
    ).fetchall()

    participants: list[dict] = []
    group_total = 0.0
    for p in parts:
        items = parse_items(p["items_json"])
        sub = items_total(items)
        group_total += sub
        participants.append(
            {
                "id": p["id"],
                "display_name": p["display_name"],
                "phone": (p["phone"] or "").strip(),
                "line_items": items,
                "subtotal": sub,
                "paid_at": p["paid_at"],
                "agreed_at": p["agreed_at"],
            }
        )

    eligible = [p for p in participants if p["subtotal"] > 0]
    all_agreed = bool(eligible) and all(
        bool(p["paid_at"]) and bool(p["agreed_at"]) for p in eligible
    )

    join_url = public_base_for_session(sess) + join_public_path(sess["slug"])

    return {
        "sess": sess,
        "participants": participants,
        "group_total": round(group_total, 2),
        "join_url": join_url,
        "all_agreed": all_agreed,
    }


def handle_session_manage_post(db: sqlite3.Connection, session_id: str) -> bool:
    if request.method != "POST":
        return False
    action = request.form.get("action") or ""
    if not action:
        return False
    if action == "set_link_base":
        norm = _normalize_link_base(request.form.get("link_base_url"))
        if norm is None:
            db.execute(
                "UPDATE sessions SET link_base_url = NULL WHERE id = ?",
                (session_id,),
            )
        else:
            db.execute(
                "UPDATE sessions SET link_base_url = ? WHERE id = ?",
                (norm, session_id),
            )
        db.commit()
        flash("Đã cập nhật địa chỉ link chia sẻ.", "ok")
        return True
    if action == "lock":
        db.execute(
            "UPDATE sessions SET status = 'locked' WHERE id = ? AND status = 'open'",
            (session_id,),
        )
        db.commit()
        flash("Đã khóa phiên — người tham gia không chỉnh sửa món được nữa.", "ok")
        return True
    if action == "unlock":
        db.execute(
            "UPDATE sessions SET status = 'open' WHERE id = ? AND status = 'locked'",
            (session_id,),
        )
        db.commit()
        flash("Đã mở khóa phiên.", "ok")
        return True
    if action == "placed":
        proof = (request.form.get("proof_note") or "").strip()
        db.execute(
            """
            UPDATE sessions SET status = 'placed', proof_note = ?
            WHERE id = ? AND status IN ('open', 'locked')
            """,
            (proof, session_id),
        )
        db.commit()
        flash("Đã đánh dấu đã đặt đơn (kèm ghi chú / bằng chứng).", "ok")
        return True
    if action == "cancel":
        db.execute(
            "UPDATE sessions SET status = 'cancelled' WHERE id = ?",
            (session_id,),
        )
        db.commit()
        flash("Đã hủy phiên.", "ok")
        return True
    return False


@app.route("/")
def home():
    """Trang chính: danh sách nhóm đang mở / khóa để người dùng chọn tham gia."""
    db = get_db()
    rows = db.execute(
        """
        SELECT * FROM sessions
        WHERE status IN ('open', 'locked')
        ORDER BY datetime(created_at) DESC
        LIMIT 100
        """
    ).fetchall()
    orders = []
    for r in rows:
        rd = dict(r)
        base = public_base_for_session(rd)
        slug = rd["slug"]
        orders.append(
            {
                "title": rd["title"],
                "brand": rd["brand"],
                "status": rd["status"],
                "slug": slug,
                "creator_name": (rd.get("creator_name") or "").strip(),
                "join_url": base + join_public_path(slug),
            }
        )
    return render_template("home.html", orders=orders)


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        pwd = (request.form.get("password") or "").strip()
        if pwd == ADMIN_PASSWORD:
            flask_session["admin"] = True
            return redirect(url_for("admin_dashboard"))
        flash("Sai mật khẩu.", "error")
    return render_template("admin_login.html")


@app.route("/admin/logout", methods=["POST"])
def admin_logout():
    flask_session.pop("admin", None)
    return redirect(url_for("admin_login"))


@app.route("/admin")
@admin_required
def admin_dashboard():
    db = get_db()
    rows = db.execute(
        "SELECT * FROM sessions ORDER BY datetime(created_at) DESC"
    ).fetchall()
    sessions_list = []
    for r in rows:
        rd = dict(r)
        base = public_base_for_session(rd)
        sessions_list.append(
            {
                "id": r["id"],
                "slug": r["slug"],
                "title": r["title"],
                "brand": r["brand"],
                "status": r["status"],
                "created_at": r["created_at"],
                "join_url": base + join_public_path(r["slug"]),
            }
        )
    return render_template("admin_dashboard.html", sessions=sessions_list)


@app.route("/creator", methods=["GET", "POST"])
def creator():
    """Form thông tin cá nhân + tạo phiên — link quản lý dạng /creator/session/…"""
    if request.method == "POST":
        creator_name = (request.form.get("creator_name") or "").strip()
        creator_phone = (request.form.get("creator_phone") or "").strip()
        if len(creator_name) < 1:
            flash("Nhập họ tên hoặc tên hiển thị của bạn.", "error")
            return render_template("creator.html")
        title = (request.form.get("title") or "").strip() or "Gom đơn"
        brand = (request.form.get("brand") or "").strip() or "Foodpanda / Keeta"
        address = (request.form.get("address") or "").strip()
        scheduled = (request.form.get("scheduled_at") or "").strip() or None
        deadline = (request.form.get("deadline_at") or "").strip() or None
        db = get_db()
        slug = generate_unique_order_code(db)
        sid = str(uuid.uuid4())
        host_token = secrets.token_urlsafe(16)
        link_base = _normalize_link_base(request.form.get("link_base_url"))
        try:
            db.execute(
                """
                INSERT INTO sessions (
                    id, slug, title, brand, address, scheduled_at, deadline_at,
                    status, proof_note, created_at, host_token, link_base_url,
                    creator_name, creator_phone
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, 'open', '', ?, ?, ?, ?, ?)
                """,
                (
                    sid,
                    slug,
                    title,
                    brand,
                    address,
                    scheduled,
                    deadline,
                    utc_now_iso(),
                    host_token,
                    link_base,
                    creator_name,
                    creator_phone,
                ),
            )
            db.commit()
        except sqlite3.IntegrityError:
            flash("Trùng mã (hiếm) — thử gửi form lại.", "error")
            return render_template("creator.html")
        return redirect(
            url_for("creator_session", session_id=sid, host_token=host_token)
        )
    return render_template("creator.html")


@app.route("/creator/session/<session_id>/<host_token>", methods=["GET", "POST"])
def creator_session(session_id: str, host_token: str):
    db = get_db()
    ctx = build_session_manage_view_context(session_id)
    if not ctx:
        abort(404)
    stored = ctx["sess"].get("host_token") or ""
    if not stored or not secrets.compare_digest(str(stored), str(host_token)):
        abort(404)
    if request.method == "POST" and handle_session_manage_post(db, session_id):
        return redirect(
            url_for("creator_session", session_id=session_id, host_token=host_token)
        )
    return render_template(
        "session_manage.html",
        **ctx,
        back_url=url_for("home"),
        back_label="← Trang chủ",
        manage_heading="Thao tác người tạo phiên",
    )


@app.route("/session/new", methods=["GET", "POST"])
def legacy_session_new():
    return redirect(url_for("creator"), code=302)


@app.route("/host/<session_id>/<host_token>", methods=["GET", "POST"])
def legacy_host_session(session_id: str, host_token: str):
    return redirect(
        url_for("creator_session", session_id=session_id, host_token=host_token),
        code=301,
    )


@app.route("/admin/session/new", methods=["GET", "POST"])
def admin_session_new_redirect():
    return redirect(url_for("creator"), code=302)


@app.route("/admin/session/<session_id>", methods=["GET", "POST"])
@admin_required
def admin_session_detail(session_id: str):
    db = get_db()
    ctx = build_session_manage_view_context(session_id)
    if not ctx:
        abort(404)
    if request.method == "POST" and handle_session_manage_post(db, session_id):
        return redirect(url_for("admin_session_detail", session_id=session_id))
    return render_template(
        "session_manage.html",
        **ctx,
        back_url=url_for("admin_dashboard"),
        back_label="← Các phiên",
        manage_heading="Thao tác admin",
    )


@app.route("/joiner/<slug>")
def joiner_landing(slug: str):
    db = get_db()
    srow = db.execute("SELECT * FROM sessions WHERE slug = ?", (slug,)).fetchone()
    if not srow:
        abort(404)
    if srow["status"] == "cancelled":
        return render_template("user_message.html", title="Phiên đã hủy", message="Phiên gom đơn này không còn hoạt động.")
    slug_val = srow["slug"]
    return render_template(
        "user_landing.html",
        sess=dict(srow),
        join_action=f"{join_public_path(slug_val)}/join",
    )


@app.route("/joiner/<slug>/join", methods=["POST"])
def joiner_join(slug: str):
    db = get_db()
    srow = db.execute("SELECT * FROM sessions WHERE slug = ?", (slug,)).fetchone()
    if not srow:
        abort(404)
    if srow["status"] in ("cancelled", "placed"):
        flash("Không thể tham gia phiên này.", "error")
        return redirect(join_public_path(slug))
    name = (request.form.get("display_name") or "").strip()
    phone = (request.form.get("phone") or "").strip() or None
    if len(name) < 1:
        flash("Nhập tên hiển thị.", "error")
        return redirect(join_public_path(slug))
    pid = str(uuid.uuid4())
    db.execute(
        """
        INSERT INTO participants (id, session_id, display_name, items_json, paid_at, agreed_at, created_at, phone)
        VALUES (?, ?, ?, '[]', NULL, NULL, ?, ?)
        """,
        (pid, srow["id"], name, utc_now_iso(), phone),
    )
    db.commit()
    return redirect(participant_path(slug, pid))


@app.route("/joiner/<slug>/p/<participant_id>", methods=["GET", "POST"])
def joiner_participant(slug: str, participant_id: str):
    db = get_db()
    srow = db.execute("SELECT * FROM sessions WHERE slug = ?", (slug,)).fetchone()
    if not srow:
        abort(404)
    prow = db.execute(
        "SELECT * FROM participants WHERE id = ? AND session_id = ?",
        (participant_id, srow["id"]),
    ).fetchone()
    if not prow:
        abort(404)

    sess = dict(srow)
    status = sess["status"]

    if request.method == "POST":
        action = request.form.get("action") or ""
        if status in ("cancelled", "placed"):
            flash("Phiên đã đóng, không chỉnh sửa được.", "error")
            return redirect(participant_path(slug, participant_id))

        if action == "add_item":
            if status == "locked":
                flash("Phiên đã khóa — không thêm món mới.", "error")
            else:
                n = (request.form.get("item_name") or "").strip()
                qty = request.form.get("qty") or "1"
                price = request.form.get("unit_price") or "0"
                items = parse_items(prow["items_json"])
                try:
                    q = int(qty)
                except (TypeError, ValueError):
                    q = 1
                try:
                    up = float(str(price).replace(",", "."))
                except (TypeError, ValueError):
                    up = 0.0
                if n and q >= 1:
                    items.append({"name": n, "qty": q, "unit_price": round(up, 2)})
                    db.execute(
                        "UPDATE participants SET items_json = ? WHERE id = ?",
                        (json.dumps(items, ensure_ascii=False), participant_id),
                    )
                    db.commit()
        elif action == "remove_item":
            if status == "locked":
                flash("Phiên đã khóa — không xóa món.", "error")
            else:
                idx = request.form.get("index")
                items = parse_items(prow["items_json"])
                try:
                    i = int(idx)
                    if 0 <= i < len(items):
                        items.pop(i)
                        db.execute(
                            "UPDATE participants SET items_json = ? WHERE id = ?",
                            (json.dumps(items, ensure_ascii=False), participant_id),
                        )
                        db.commit()
                except (TypeError, ValueError):
                    pass
        elif action == "mark_paid":
            if not prow["paid_at"]:
                db.execute(
                    "UPDATE participants SET paid_at = ? WHERE id = ?",
                    (utc_now_iso(), participant_id),
                )
                db.commit()
                flash("Đã ghi nhận bạn đã chuyển tiền (demo).", "ok")
        elif action == "agree":
            items = parse_items(prow["items_json"])
            if items_total(items) <= 0:
                flash("Thêm ít nhất một món có giá trước khi đồng ý.", "error")
            elif not prow["paid_at"]:
                flash("Cần xác nhận đã chuyển tiền trước.", "error")
            elif prow["agreed_at"]:
                pass
            else:
                db.execute(
                    "UPDATE participants SET agreed_at = ? WHERE id = ?",
                    (utc_now_iso(), participant_id),
                )
                db.commit()
                flash("Bạn đã đồng ý với tổng tiền (ước tính) của phiên.", "ok")
        return redirect(participant_path(slug, participant_id))

    prow = db.execute(
        "SELECT * FROM participants WHERE id = ? AND session_id = ?",
        (participant_id, srow["id"]),
    ).fetchone()
    my_items = parse_items(prow["items_json"])
    my_total = items_total(my_items)

    others = db.execute(
        "SELECT * FROM participants WHERE session_id = ? ORDER BY datetime(created_at)",
        (srow["id"],),
    ).fetchall()
    group_total = 0.0
    roster = []
    for o in others:
        o_items = parse_items(o["items_json"])
        sub = items_total(o_items)
        group_total += sub
        roster.append(
            {
                "display_name": o["display_name"],
                "is_me": o["id"] == participant_id,
                "subtotal": sub,
                "paid": bool(o["paid_at"]),
                "agreed": bool(o["agreed_at"]),
                "item_count": len(o_items),
            }
        )

    share_url = public_base_for_session(sess) + participant_path(slug, participant_id)

    return render_template(
        "user_participant.html",
        sess=sess,
        participant=dict(prow),
        my_items=my_items,
        my_total=my_total,
        group_total=round(group_total, 2),
        roster=roster,
        share_url=share_url,
    )


@app.route("/j/<slug>")
def legacy_j_landing(slug: str):
    return redirect(url_for("joiner_landing", slug=slug), code=301)


@app.route("/j/<slug>/join", methods=["POST"])
def legacy_j_join(slug: str):
    return joiner_join(slug)


@app.route("/j/<slug>/p/<participant_id>", methods=["GET", "POST"])
def legacy_j_participant(slug: str, participant_id: str):
    return joiner_participant(slug, participant_id)


@app.route("/<order_code>", methods=["GET"])
def legacy_root_order_code(order_code: str):
    """Chuyển URL cũ /Mã13kýTự sang /joiner/Mã."""
    if not is_standard_order_code(order_code):
        abort(404)
    return redirect(url_for("joiner_landing", slug=order_code), code=301)


@app.route("/<order_code>/join", methods=["POST"])
def legacy_root_join(order_code: str):
    if not is_standard_order_code(order_code):
        abort(404)
    return joiner_join(order_code)


@app.route("/<order_code>/p/<participant_id>", methods=["GET", "POST"])
def legacy_root_participant(order_code: str, participant_id: str):
    if not is_standard_order_code(order_code):
        abort(404)
    return redirect(
        url_for("joiner_participant", slug=order_code, participant_id=participant_id),
        code=301,
    )


def main():
    # init chạy ở request đầu tiên (before_request); gọi sớm để lỗi hiện ngay khi khởi động
    with app.app_context():
        init_db()
        app.config["_db_ready"] = True
    # Cho phép truy cập từ máy khác trong LAN khi chạy thử
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")), debug=True)


if __name__ == "__main__":
    main()
