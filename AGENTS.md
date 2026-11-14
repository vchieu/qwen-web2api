# AGENTS.md — qwen-web2api

Proxy Qwen Chat (chat.qwen.ai) → API tương thích OpenAI. Project chạy
**bridge-only**: Python không bao giờ gọi chat.qwen.ai trực tiếp. Hai server
cùng process: API công khai ở `CONFIG["port"]` (8082) và bridge nội bộ ở
`CONFIG["bridge_port"]` (8083). Extension trong tab chat.qwen.ai tự bấm UI,
bắt SSE; Python chỉ xếp job và stream kết quả về client.

## Chạy & kiểm tra

```powershell
python -m qwen_web2api                 # luôn bridge mode
python -m qwen_web2api --port 8081     # override port API
python -m unittest test_qwen_web2api -v
python -m pytest test_qwen_web2api.py -q
node --check extension/bridge.js       # cú pháp content script
```

Cần mở https://chat.qwen.ai (đã đăng nhập), giữ tab mở và bật extension
(`extension/` load unpacked). Không có tab/worker → request trả 503.

## Bản đồ file

| File | Vai trò |
| --- | --- |
| `qwen_web2api/server.py` | HTTP OpenAI-compatible: `/v1/chat/completions`, `/v1/responses`, `/v1/models`, auth, CORS, SSE, `_run_turn` (tool retry/nudge) |
| `qwen_web2api/qwen.py` | `QwenError` + logger `log()`, `debug_log()`, `is_debug()`, `get_recent_log()` dùng chung; không còn transport/token |
| `qwen_web2api/bridge.py` | Server 8083 cho extension: `/poll`, `/event`, `/status`, `/debug`, `/requeue`, `/file/*`; queue job, stream event, `known_models()`, `temporary_chat_enabled()` |
| `qwen_web2api/tools.py` | Flatten messages → prompt, chèn `# Tool Use`, parse fence `tool_call`, truncate theo byte |
| `qwen_web2api/models.py` | Alias model + `@think=0/1` |
| `qwen_web2api/config.py` | `DEFAULT_CONFIG` + `CONFIG` (global mutable), load/find config |
| `qwen_web2api/__main__.py` | CLI, `_guard_bind` tự sinh API key khi bind ra ngoài loopback |
| `extension/bridge.js` | MV3 content script (MAIN world): poll job, chọn temporary/model trong UI, gửi prompt, bắt native `tool_call`, upload file prompt dài |
| `test_qwen_web2api.py` | Toàn bộ unit test (1 file, stdlib `unittest`) |

Luồng chat: `do_POST` → `_handle_chat` / `_handle_responses` →
`prepare_large_prompt` → `messages_to_prompt` → `_run_turn` →
`bridge.generate` / `bridge.generate_stream` → `parse_tool_calls` → JSON hoặc SSE.

## Config chính

- `bridge_port` (8083), `bridge_worker_ttl` (20s), `bridge_job_timeout` (600s),
  `bridge_poll_wait` (20s).
- `temporary_chat`: `true` → mọi job chạy ở `/?temporary-chat=true`.
  Chấp nhận cả spelling `temporary-chat` (hyphen) trong config file.
- `bridge_select_model` (default `true`): `false` → extension không đụng model
  picker; request vẫn chạy với model UI đang chọn.
- `attachment_prompt`: text gửi kèm khi prompt dài được paste/attach thành document
  (mặc định `"Please read the text and action"`) để tránh Qwen báo "The current content is empty, please regenerate."
  Khi prompt dài có nhiều tin nhắn và lượt cuối là user (<= 130000 ký tự), lượt cuối đó (kèm tool definitions / response_format nếu có)
  được render tự động làm attachment_prompt điền vào composer, còn attachment_prompt cấu hình đóng vai trò fallback.
- `default_model`, `strict_models`, `thinking`, `retry_attempts`,
  `retry_delay_sec`, `max_body_bytes`, `prompt_max_bytes`,
  `tool_retry_on_miss`, `tool_retry_attempts`, `log_requests`, `log_file`.
- `debug` (default `false`): bật/tắt log debug chi tiết cho cả Python proxy và browser extension
  (chấp nhận cả alias `debug_log` hoặc `verbose_log` trong config file). Khi bật, `/status` trả về
  `debug: true`, extension kích hoạt log console chi tiết và forward event `debug` về Python,
  đồng thời Python in thông tin chẩn đoán cho từng turn, queue và lifecycle của job.
  Khi `debug: true`, endpoint `GET /debug` (port 8083) trả về snapshot JSON an toàn gồm config, bridge state
  và 100 dòng log gần nhất (`recent_log`) từ cả server lẫn bridge để kiểm tra real-time.

## Temporary chat & chọn model (extension)

1. Poll loop đọc `GET /status`. Nếu `temporary_chat` khác URL hiện tại:
   - có job: tăng `job.mode_retries`, `POST /requeue {job}` (best-effort) rồi
     `location.assign(...)`; job cũ vẫn nằm trong `_jobs` của Python nên tab
     mới poll lại đúng `job_id`.
   - không có job: navigate luôn (`idleModeCheck`).
   - quá 2 lần retry: gửi `warn`, tiếp tục ở chế độ hiện tại.
   - idle navigation cap 2 lần/60 giây (lưu ở `sessionStorage`) để tránh reload
     lặp; poll loop dừng khi đang navigate.
2. Trước khi gửi, `selectModel(job)` tìm model picker theo `job.model`
   (bỏ `@think=`, so id/name đã chuẩn hoá). Click trigger + option; không tìm
   thấy/không đổi được thì gửi `warn` (Python log) và chạy với model UI hiện
   tại. Lỗi chọn model **không** làm fail request.
3. `refreshModels()` đọc `GET /api/v2/models` trong tab (throttle 10 phút) và
   gửi kèm `models` trong body `/poll`; Python merge vào `/v1/models`
   (`bridge.known_models()`).
4. Parser: thinking thật từ upstream stream dưới dạng
   `phase="thinking_summary"` với `extra.summary_thought` /
   `extra.summary_title`; hỗ trợ cả mảng cumulative (Qwen thật) lẫn disjoint
   block (`status:"finished"`). `_seenTitles` đảm bảo mỗi step title in đậm
   **một lần duy nhất**. `on_reasoning_delta` stream thinking trực tiếp về
   client qua SSE (kể cả khi `tools_active`). Send timeout 90s (`SEND_TIMEOUT_MS`)
   để điền UI và chờ completion response bắt đầu; timeout được huỷ ngay khi
   bắt được response, chuyển sang watchdog 3 phút idle (`STREAM_IDLE_TIMEOUT_MS`)
   reset theo từng chunk để không bao giờ bị timeout khi Qwen suy nghĩ lâu.
   Ngoài ra vẫn nhận `reasoning_content` / `phase=thinking|think|reasoning` nếu
   Qwen đổi lại shape. Delta lạ được gửi `warn` (phase + keys) một lần và 20
   dòng SSE gần nhất chứa think/reason/summary in bằng `__qwenUIBridgeDebug()`.
   Khi Qwen UI chuyển text paste thành document card, composer bị rỗng —
   bridge điền `attachment_prompt` (lượt cuối của user nếu tách được, hoặc fallback mặc định `"Please read the text and action"`)
   trước khi bấm Send để tránh lỗi "The current content is empty, please regenerate.";
   nếu Qwen vẫn trả về câu lỗi đó, bridge ném lỗi ngay để Python retry.
   Bridge hook cả `window.fetch` lẫn `XMLHttpRequest` để bắt SSE khi app đổi
   transport. Trước mỗi job, bridge bấm New Chat (`NEW_CHAT_EACH_JOB = true`)
   để tránh lẫn context giữa các request.
5. Chẩn đoán & bắt lỗi UI (Diagnostics):
   - **Temporary chat thiếu New Chat**: `tryNewChat(job)` kiểm tra nút New Chat; nếu không tìm thấy
     hoặc bỏ qua nút `href="/"` trong temporary mode khi DOM đã có tin nhắn cũ, gửi cảnh báo nguy cơ lẫn context.
   - **Thiếu paste `attachment_prompt`**: sau khi paste/upload file, nếu composer bị rỗng, bridge thử điền lại;
     nếu vẫn rỗng trước khi gửi, bridge gửi cảnh báo `warn` ngay.
   - **Có prompt nhưng thiếu file đính kèm**: trước khi bấm Send cho job prompt lớn, bridge kiểm tra card/chip
     attachment trong DOM (`findActiveAttachmentElements`); nếu composer có text nhưng thiếu file đính kèm,
     bridge gửi cảnh báo `warn` lập tức.
   - **Phát hiện browser reload làm mất file**: tiến trình job được lưu theo từng bước vào `sessionStorage`
     (`qwenUIBridgeActiveJob`). Nếu tab tự reload hoặc người dùng reload trong khi đang xử lý (nhất là sau khi
     đã upload file), tab mới khởi động sẽ phát hiện ngay, gửi cảnh báo `tab_reloaded` qua `/event` (hỗ trợ cả
     `beforeunload` + `navigator.sendBeacon`) để Python ghi nhận nguyên nhân request thất bại.
   - **Job mồ côi (orphan jobs)**: Python `/poll` tự động phát hiện và drop job hết hạn TTL hoặc job có caller
     đã ngắt kết nối (`managed=True` nhưng không còn trong `_jobs`), không chuyển job chết cho extension.

6. Chẩn đoán gửi prompt (bridge_fix_patch): hook `window.fetch` còn đọc body request
   completion của chính app (`inspectCompletionRequest`) và gửi event `debug`/`warn` kèm
   số attachment thật — đây là bằng chứng chính xác file prompt dài có tới Qwen hay không;
   heuristic DOM `findActiveAttachmentElements` có thể báo sai nên chỉ warn 1 lần/job.
   `findSendButton` bỏ qua nút Stop. `selectModel` đọc text nhìn thấy của model picker
   (`triggerLabel`) thay vì chỉ aria-label. Mỗi job gửi event debug
   `temporary evidence: {url,dom,text,cached}` để thấy cache `pageLoadedAsTemporary` có lệch
   URL/DOM không. Cache chủ ý KHÔNG bị reset mù quáng: reset khi URL mất `?temporary-chat=true`
   có thể gây reload lặp nếu Qwen tự xoá param. `tools.py` rule (B) bảo model đưa final answer
   qua completion tool (vd `attempt_completion`) khi client yêu cầu mỗi reply phải dùng tool.

7. `isTemporaryPage(strict)`: khi config muốn chế độ thường (`temporary_chat=false`) chỉ tin
   URL `?temporary-chat=true`/cache, không tin DOM/text (nút toggle "temporary chat" luôn hiện
   ở chế độ thường → false positive → `location.assign("/")` lặp = tab reload liên tục).
   Muốn temporary vẫn dùng heuristic lỏng (app tự xoá param vẫn không reload lặp).

8. Giới hạn file prompt: Qwen chỉ chuyển text paste thành file đính kèm khi ≤ 131072 ký tự.
   `prepare_large_prompt` cắt file lịch sử/prompt đầy đủ bằng `_render_fit` (`FILE_MAX_CHARS`=130000,
   giữ đầu + tin mới nhất, chèn `[...truncated...]`); `messages_to_prompt(max_bytes=...)` ép trần
   byte; CRLF được chuẩn hoá về LF (Windows text mode từng ghi `\r\r\n` làm phình file). File vượt
   giới hạn thì paste không thành attachment, composer bị thay bằng companion prompt và MẤT lịch sử.
   bridge.js gửi `warn` nếu `file_info.size` > 131072.
   - **Job sau bị đứng ở temporary chat**: `runJob` gọi `waitUiIdle` trước mỗi job — nếu Qwen UI vẫn đang generate
     (nút Stop; job trước settle sớm do native `tool_call`/`role=function`) thì bridge bấm Stop rồi mới gõ prompt mới.
     `submitPrompt` dùng vòng lặp `sendPrompt` (chờ nút Send, thử Enter, không click lại khi `job.attached`);
     `findSendButton` bỏ qua nút Stop.
   - **Prompt lớn không thành attachment**: `attachLongPrompt` thử lần lượt paste-text → paste-file → `input[type=file]`
     → drop-file, mỗi bước kiểm tra card; cả bốn thất bại và text thô > 125000 ký tự thì ném lỗi (Python retry)
     thay vì gửi text vượt giới hạn. Job có file dùng `SEND_TIMEOUT_FILE_MS` (240s).
   - **tab_reloaded**: Python `generate_stream` fail nhanh (502 → retry) khi extension báo `tab_reloaded`;
     `tab_reloading` (beforeunload) vẫn chỉ là cảnh báo.
   - `findModelTrigger` không còn chọn nhầm nút profile ("QwenUser… Free").

## Kiểm tra UI thủ công

1. Load extension, mở tab chat.qwen.ai đã đăng nhập, chạy server.
2. `curl http://127.0.0.1:8083/status` → có `temporary_chat`, `select_model`,
   `models`; sau vài giây `models` > 0.
3. Chat với `temporary_chat: true` → URL tab phải có `?temporary-chat=true`
   (tab có thể reload một lần).
4. Gửi request `"model": "qwen3.8-max"` → model picker đổi trước khi gửi; nếu
   UI đổi cấu trúc, xem log `[bridge] extension warn: ...`.
5. Console tab: `__qwenUIBridgeDebug()` in temporary page, known models,
   model trigger và text của trigger.

## Quy ước code

- Python 3.10+, ưu tiên stdlib. Không thêm dependency mới nếu stdlib làm được.
  `curl_cffi`/request trực tiếp đã bị bỏ.
- Giữ nguyên style + ngôn ngữ comment của file đang sửa (Việt/Anh đều có).
- Không đổi shape response/stream OpenAI-compatible nếu không có lý do rõ.
- `CONFIG` là global mutable: test phải snapshot/restore (xem `_Sandbox`).
- `log()` chỉ ghi khi `CONFIG["log_requests"]`; test tắt log.
- Không commit cookie/token thật hay capture request.
- Tool protocol: model trả fence ` ```tool_call `; parser hỗ trợ chuẩn OpenAI
  `{"name": "...", "arguments": {...}}` và tự động sửa các lỗi cú pháp phổ biến của Qwen
  (shorthand `{"<tool_name>", "arguments": ...}`, single-key dict `{"<tool_name>": ...}`,
  trailing comma, single quotes). Khi response có cả prose và tool_call, prose được giữ lại
  ở `content` để client hiển thị cho user thay vì bị xóa hay chuyển sang reasoning.
- `extension/bridge.js` là một content script MV3 duy nhất, chạy MAIN world
  (phải override được `window.fetch`). Không thêm file/build step. Mọi lỗi UI
  mới phải `warn` (hoặc log) rồi chạy tiếp, không throw làm hỏng request.
- Thinking toggle vẫn do UI quyết định; `job.think` hiện không điều khiển UI.

## Quy ước test

- **Một file duy nhất**: `test_qwen_web2api.py`. Chỉ tách khi vượt ~1500 dòng
  và phải hỏi trước — mục đích là copy vào chat AI được nguyên khối.
- Dùng `unittest` (pytest vẫn collect được). Không network, không browser,
  không token: mock `bridge._pending`, `bridge.generate`, `bridge.known_models`;
  riêng endpoint bridge thì dựng HTTP server loopback thật ở port 0.
- Test đụng global state kế thừa `_Sandbox` để tự restore `CONFIG` +
  `bridge._jobs`, `bridge._pending`, `bridge._models`, `bridge._last_seen`.
- Sửa hành vi thì sửa test tương ứng. Không nới assertion chỉ để pass nếu
  hành vi đang sai — báo lại thay vì che bug.
- **`test_thinking_trace.js`**: scratch file chạy bằng Node.js để debug
  thủ công parser thinking của `extension/bridge.js` (inject `handleData`
  vào vm sandbox, chạy payload SSE giả). **Không phải test suite**, không
  chạy trong CI; giữ lại để tái dùng khi cần debug lại SSE shape upstream.
  Chạy: `node test_thinking_trace.js`

## Quy ước bảo trì tài liệu

Khi thay đổi source, **cập nhật AGENTS.md ngay trong cùng commit/PR** nếu
chạm vào bất kỳ điều nào dưới đây:

| Thay đổi | Phần cần cập nhật |
| --- | --- |
| Thêm/xoá/đổi tên file chính | **Bản đồ file** |
| Thêm/đổi config key hoặc default | **Config chính** |
| Thêm endpoint bridge (`/poll`, `/event`…) | **Bản đồ file** (bridge.py) |
| Đổi luồng xử lý chat (pipeline) | **Luồng chat** (dòng ngay sau Bản đồ file) |
| Đổi hành vi extension (timeout, hook, model pick…) | **Temporary chat & chọn model** |
| Thêm global state mới (`_xxx`) | **Điểm dễ vỡ** + **Quy ước test** (`_Sandbox`) |
| Thêm file test hoặc script debug | **Quy ước test** |
| Phát hiện điểm dễ vỡ mới | **Điểm dễ vỡ** |

> Nếu thay đổi nhỏ (rename biến nội bộ, refactor không đổi hành vi),
> không cần cập nhật AGENTS.md — chỉ cập nhật khi **interface hoặc hành vi
> quan sát được từ bên ngoài** thay đổi.

## Khi thiếu ngữ cảnh thì XIN FILE, đừng đoán

1. Đọc `AGENTS.md` + `test_qwen_web2api.py` trước.
2. Cần chi tiết gì thì yêu cầu đúng file:
   - route/stream/SSE response/auth → `qwen_web2api/server.py`
   - job queue/timeout/requeue/file endpoint/models feed → `qwen_web2api/bridge.py`
   - prompt/tool parse/truncate → `qwen_web2api/tools.py`
   - extension poll/temporary/model picker/upload/native tool_call → `extension/bridge.js`
   - alias model → `qwen_web2api/models.py`
   - config mặc định → `qwen_web2api/config.py`
   - `QwenError`/`log` → `qwen_web2api/qwen.py`
3. Không suy đoán shape SSE upstream; trích từ `bridge.js` hoặc capture thật.
4. Nếu buộc phải làm khi thiếu file, nêu rõ giả định trong câu trả lời.

## Điểm dễ vỡ

- `extension/bridge.js` hardcode `http://127.0.0.1:8083`; đổi `bridge_port`
  phải sửa cả trong script.
- Model-picker DOM là heuristic, chưa có test UI thật: Qwen đổi layout là
  phải chỉnh `findModelTrigger` / `selectModel`.
- Temporary-mode switch có thể `location.assign` → mất JS đang chạy; job được
  `/requeue` trước đó và `_jobs` phía Python không bị xoá, nhưng nếu tab
  không reload thì request sẽ timeout.
- Prompt render > `MAX_PROMPT_CHARS` (130000): nếu có history và lượt cuối là user (<= 130000),
  history được ghi vào file tạm `qwen_prompt_*.txt` còn lượt cuối được điền vào composer làm prompt;
  nếu không thể tách, toàn bộ ghi file và composer dùng fallback `attachment_prompt`.
  Bridge chỉ serve file đó trong tempdir.
- Global state: `bridge._pending`, `bridge._jobs`, `bridge._models`,
  `bridge._last_seen`, `qwen._recent_log`.
- Tab reload giữa chừng (crash hoặc do app): extension ghi nhận trạng thái qua `sessionStorage`
  để cảnh báo nguyên nhân mất file đã upload; Python drop job orphan khi client ngắt kết nối.
- Trên Windows, handler trả lỗi trước khi đọc body có thể reset connection;
  test cho case 403 gửi body rỗng.
