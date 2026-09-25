# Chạy iCTSV Ticket Watcher 24/7 trên Render

## Trước khi tải mã lên GitHub

1. Thu hồi token Telegram đã từng xuất hiện trong ảnh/chat bằng BotFather, rồi tạo token mới.
2. Đăng xuất/đăng nhập lại CTSV để làm mới token CTSV nếu token đã từng bị lộ.
3. Trong `ictsv_web_watcher.py`, để trống toàn bộ giá trị token và Chat ID ở phần cấu hình đầu file.
4. Không tải `.env` hoặc các file `.ictsv_*_state.json` lên GitHub.

## Tạo dịch vụ

1. Tạo một repository GitHub riêng tư chỉ gồm các file trong thư mục này.
2. Vào Render Dashboard, chọn **New > Blueprint** và nối repository.
3. Render đọc `render.yaml` và tạo một background worker cùng ổ đĩa lưu trạng thái.
4. Điền các biến bí mật khi Render yêu cầu:

   - `BKNEXUS_TOKEN`: token CTSV mới.
   - `TELEGRAM_BOT_TOKEN`: token bot Telegram mới.
   - `TELEGRAM_CHAT_ID`: Chat ID của bạn.
   - `BKNEXUS_EXTRA_PAYLOAD`: JSON chứa các trường phụ của request, ví dụ `{"UserName":"MSSV_CUA_BAN"}`.

5. Deploy và mở **Logs**. Khi thấy `[START]` và một dòng `[OK]` hoặc `[BASELINE]`, bot đã chạy.

## Bot sẽ báo khi nào?

- Một sự kiện đặt vé sắp tới mới xuất hiện.
- Một sự kiện cũ chuyển từ đóng sang mở đăng ký.
- Sự kiện hết chỗ có chỗ trống trở lại.
- Số chỗ trống tăng, thường do có người hủy vé.

Lần chạy đầu tiên chỉ tạo baseline, không gửi lại toàn bộ sự kiện đang có.

Bạn có thể kiểm tra mẫu thông báo trước khi deploy bằng lệnh:

```bash
python3 ictsv_web_watcher.py --test-ticket-alert
```
