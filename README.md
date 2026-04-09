# Group Order — bản chạy thử

Web gom đơn ăn: **admin** tạo phiên và lấy link gửi mọi người; **người dùng** mở link, thêm món, bấm đã chuyển tiền (demo), bấm đồng ý.

## Chạy

```bash
cd group-order-web
py -m pip install -r requirements.txt
py app.py
```

Mở trình duyệt:

- **Admin:** http://127.0.0.1:5000/admin/login  
  - Mật khẩu mặc định: `demo123` (đổi bằng biến môi trường `GROUP_ORDER_ADMIN_PASSWORD`).
- **Người dùng:** link dạng `http://127.0.0.1:5000/j/<mã-phiên>` (copy trong trang admin).

Gửi link cho bạn bè: thay `127.0.0.1:5000` bằng IP máy bạn trong mạng LAN (ví dụ `http://192.168.1.5:5000/j/...`) nếu cùng Wi‑Fi.

## Biến môi trường (tùy chọn)

- `GROUP_ORDER_ADMIN_PASSWORD` — mật khẩu admin  
- `FLASK_SECRET_KEY` — chuỗi bí mật session (nên đặt khi dùng thật)

Dữ liệu lưu file SQLite: `instance/group_order.db`.
