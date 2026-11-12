# AGENTS.md — qwen-web2api

Proxy Qwen Chat (chat.qwen.ai) → API tương thích OpenAI. Hai server chạy cùng
process: API công khai ở `CONFIG["port"]` (8082) và bridge nội bộ ở
`CONFIG["bridge_port"]` (8083). Upstream thật được gọi từ trong tab
chat.qwen.ai qua extension (baxia chặn request Python thuần).

## Chạy & kiểm tra

```powershell
python -m qwen_web2api              # bridge mode (mặc định)
python -m qwen_web2api --no-bridge  # gọi thẳng upstream từ Python
python -m unittest test_qwen_web2api -v
python -m pytest test_qwen_web2api.py -q
```

## Bản đồ file

| File | Vai trò |
| --- | --- |
| `qwen_web2api/server.py` | HTTP OpenAI-compatible: `/v1/chat/completions`, `/v1/responses`, `/v1/models`, auth, CORS, SSE, `_run_turn` (tool retry/nudge) |
| `qwen_web2api/qwen.py` | Upstream client: payload, SSE parse, token refresh, curl_cffi/urllib transport, anti-bot error |
| `qwen_web2api/bridge.py` | Server 8083 cho extension: `/poll`, `/event`, `/status`, `/file/*`; thay `qwen.generate*` khi bật bridge |
| `qwen_web2api/tools.py` | Flatten messages → prompt, chèn `# Tool Use`, parse fence `tool_call`, truncate theo byte |
| `qwen_web2api/models.py` | Alias model + `@think=0/1` |
| `qwen_web2api/config.py` | `DEFAULT_CONFIG` + `CONFIG` (global mutable), load/find config |
| `qwen_web2api/__main__.py` | CLI, `_guard_bind` tự sinh API key khi bind ra ngoài loopback |
| `extension/bridge.js` | MV3 content script: poll job, fetch completions trong tab, bắt native `tool_call`, upload file prompt dài |
| `test_qwen_web2api.py` | Toàn bộ unit test (1 file, stdlib `unittest`) |

Luồng chat: `do_POST` → `_handle_chat` / `_handle_responses` →
`prepare_large_prompt` → `messages_to_prompt` → `_run_turn` →
`generate`/`generate_stream` (bridge hoặc direct) → `parse_tool_calls` →
JSON hoặc SSE.

## Quy ước code

- Python 3.10+, ưu tiên stdlib. `curl_cffi` là optional (impersonate Chrome).
  Không thêm dependency mới nếu stdlib làm được.
- Giữ nguyên style + ngôn ngữ comment của file đang sửa (Việt/Anh đều có).
- Không đổi shape response/stream OpenAI-compatible nếu không có lý do rõ.
- `CONFIG` là global mutable: test phải snapshot/restore (xem `_Sandbox`).
- `log()` chỉ ghi khi `CONFIG["log_requests"]`; test tắt log.
- Không commit `token.json`, cookie, `bx-ua`/`bx-umidtoken`/`bx-v` thật.
- Tool protocol: model trả fence ` ```tool_call `; mọi tham số nằm trong
  object `"arguments"`, không nằm cạnh `"name"`.

## Quy ước test

- **Một file duy nhất**: `test_qwen_web2api.py`. Chỉ tách khi vượt ~1500 dòng
  và phải hỏi trước — mục đích là copy vào chat AI được nguyên khối.
- Dùng `unittest` (pytest vẫn collect được). Không network, không browser,
  không token: mock `qwen._request`, `qwen.create_chat`, `bridge._pending`;
  riêng endpoint bridge thì dựng HTTP server loopback thật ở port 0.
- Test đụng global state kế thừa `_Sandbox` để tự restore `CONFIG` + cache
  (`_cred_cache`, `_models_cache`, `_api_ver`, `bridge._jobs`, ...).
- Sửa hành vi thì sửa test tương ứng. Không nới assertion chỉ để pass nếu
  hành vi đang sai — báo lại thay vì che bug.

## Khi thiếu ngữ cảnh thì XIN FILE, đừng đoán

1. Đọc `AGENTS.md` + `test_qwen_web2api.py` trước.
2. Cần chi tiết gì thì yêu cầu đúng file:
   - payload/SSE/token refresh/transport → `qwen_web2api/qwen.py`
   - prompt/tool parse/truncate → `qwen_web2api/tools.py`
   - route/stream/SSE response/auth → `qwen_web2api/server.py`
   - extension poll/upload/native tool_call → `extension/bridge.js`
   - job queue/timeout/file endpoint → `qwen_web2api/bridge.py`
   - alias model → `qwen_web2api/models.py`
   - config mặc định → `qwen_web2api/config.py`
3. Không suy đoán shape payload upstream; trích từ code hoặc capture thật.
4. Nếu buộc phải làm khi thiếu file, nêu rõ giả định trong câu trả lời.

## Điểm dễ vỡ

- `server.py` truyền `file_info=` vào `generate`/`generate_stream`, nhưng
  `qwen.generate*` chưa nhận tham số này → chế độ `--no-bridge` có thể
  `TypeError`; chỉ bridge mode xử lý prompt dài. Sửa thì thêm test.
- Prompt render > `MAX_PROMPT_CHARS` (130000) được ghi file tạm
  `qwen_prompt_*.txt`; bridge chỉ serve file đó trong tempdir.
- Cache global: `qwen._cred_cache`, `qwen._models_cache`, `qwen._api_ver`,
  `qwen._refresh_path`, `bridge._jobs`.
- `_api_ver` nhớ version API (v1/v2) sau lần gọi thành công đầu tiên.
- Trên Windows, handler trả lỗi trước khi đọc body có thể reset connection;
  test cho case 403 gửi body rỗng.
