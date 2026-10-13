# Lab 25B — Report

**Họ tên / nhóm:** Nguyễn Hoàng Anh  **Commit:** 52b282b  **Điểm tự động (`make grade`):** 84.0 / 88

> Mọi nhận định phải có số. Dán số từ `reports/gameday/*.json`, `reports/workflows.json`, `reports/slo.json`.

## 1. Kết quả Game Day (naive vs của bạn)

| Băng | Client | availability | p95 (s) | amplification | retry_ratio | cost/turn | recovery (s) | PASS? |
|---|---|---:|---:|---:|---:|---:|---:|:---:|
| G1 | naive | 0.7194 | 7.157 | 7.738 | 6.738 | 2.163 | 4.0 | FAIL |
| G1 | yours | 1.0000 | 4.776 | 1.062 | 0.002 | 2.101 | 0.0 | PASS |
| G2 | naive | 0.4560 | 15.034 | 9.904 | 8.904 | 4.627 | 155.0 | FAIL |
| G2 | yours | 1.0000 | 11.217 | 1.044 | 0.005 | 2.129 | 0.0 | PASS |
| G3 | naive | 1.0000 | 4.768 | 1.164 | 0.164 | 2.328 | 0.0 | FAIL |
| G3 | yours | 1.0000 | 4.718 | 1.065 | 0.035 | 2.146 | 0.0 | PASS |
| G4 | naive | 1.0000 | 13.764 | 1.004 | 0.004 | 2.008 | 0.0 | FAIL |
| G4 | yours | 1.0000 | 5.684 | 1.013 | 0.002 | 2.069 | 0.0 | PASS |
| G5 | naive | 0.3699 | 5.359 | 17.030 | 16.030 | 2.124 | null | FAIL |
| G5 | yours | 0.8179 | 5.477 | 1.003 | 0.002 | 2.504 | null | PASS |
| G6 | naive | 0.5170 | 4.836 | 12.344 | 11.344 | 2.009 | 0.0 | FAIL |
| G6 | yours | 1.0000 | 5.958 | 1.053 | 0.019 | 2.203 | 0.0 | PASS |

## 2. Cấu hình client và lý do

| Tham số | Giá trị | Vì sao (dựa trên băng nào, số liệu nào) |
|---|---:|---|
| attempt_timeout_s | 8.0 | Dựa trên G4 và G2: p99 lúc bình thường là ~3.0s, nhưng khi degraded hoặc slow region (G4), provider mất 11–13s. Đặt 8.0s giúp cắt các request treo vô vọng trên provider chậm trước khi hết call deadline (30s), chừa đủ thời gian failover sang `alpha-eu` và `beta`. |
| max_attempts_per_provider | 2 | Dựa trên G1 và G6: Cho phép tối đa 1 lần retry trên cùng một provider để khắc phục các lỗi chập chờn nhất thời (`api_error` 500). Nếu vẫn lỗi, failover sang provider khác thay vì tiếp tục dồn tải vào node đang hỏng. |
| backoff_base_s / cap_s | 0.25 / 4.0 | Dựa trên G1: Base 0.25s kết hợp Full Jitter giúp giãn đều các retry sau khi bị `rate_limit_exceeded` (429), triệt tiêu hiện tượng dồn tải đồng pha (retry storm). Cap 4.0s bảo đảm không lãng phí thời gian chờ khi turn deadline chỉ có 30s. |
| retry_budget_ratio | 0.1 | Dựa trên G2 và G3: Giới hạn số lần retry không vượt quá 10% tổng số first attempts. Ngăn chặn khuếch đại tải khi hệ thống quá tải diện rộng, giúp hạ amplification từ 9.904 (naive) xuống 1.044 (yours). |
| breaker.window_size / min_calls | 20 / 10 | Dựa trên G6 và G2: Window 20 cuộc gọi đủ lớn để đánh giá thống kê chính xác, min_calls 10 cuộc gọi ngăn breaker kích hoạt sớm khi lưu lượng còn thưa thớt ở đầu kịch bản. |
| breaker.failure_rate_threshold | 0.5 | Dựa trên G6: Khi 50% cuộc gọi thất bại (service unavailable hoặc timeout), breaker chuyển sang OPEN để cô lập provider hỏng, bảo vệ hệ thống và chuyển hướng tải tức thì. |
| breaker.slow_call_s | 5.0 | Dựa trên G4: Latency bình thường của alpha là ~1.2s. Khi region chậm lại quá 5.0s, breaker ghi nhận là slow call và ngắt mạch khi tỷ lệ slow vượt ngưỡng (0.6), giúp p95 giảm từ 13.764s (naive) xuống 5.684s. |
| breaker.open_duration_s | 10.0 | Dựa trên G2: Giữ trạng thái OPEN trong 10 giây để provider quá tải có khoảng lặng hạ nhiệt hàng đợi, sau đó mới thử nghiệm thăm dò qua HALF-OPEN. |
| breaker.half_open_max_probes | 2 | Dựa trên G2: Chỉ cho phép 2 request thăm dò đi qua ở trạng thái HALF-OPEN. Nếu thành công mới phục hồi CLOSED, tránh xả ồ ạt tải làm provider sập lại. |

Sơ đồ quyết định của `on_error` (ASCII hoặc bảng: loại lỗi → ghi breaker? → retry? → failover? → cooldown?):

```
                    [ Provider Trả Về Lỗi ]
                               |
              +----------------+----------------+
              |                                 |
      [ 400 Bad Request ]             [ 429 Rate Limit / Quota ]
              |                                 |
     Không ghi Breaker                 +--------+--------+
     Không Retry                       |                 |
     GIVE UP (Client Error)      (Quota Hết)       (Rate Limit)
                                       |                 |
                                Ghi Breaker       Không ghi Breaker
                                Cooldown 300s     Đợi Retry-After + Jitter
                                FAILOVER          RETRY (nếu còn budget)
                                                  Hết budget -> FAILOVER
              +---------------------------------+
              |
      [ 5xx / Timeout / Overloaded ]
              |
       Ghi vào Circuit Breaker
              |
     +--------+--------+
     |                 |
(Overload / 503 / Timeout)   (500 Transient API Error)
     |                               |
  FAILOVER ngay               Attempts < 2 & Còn Budget?
                                     |
                             +-------+-------+
                             |               |
                           (Có)            (Không)
                             |               |
                         RETRY cùng      FAILOVER sang
                         Provider        Provider tiếp theo
```

## 3. Ba thí nghiệm (mỗi cái 3–5 câu + số)

1. **Retry budget là lớp phòng thủ cuối.** Giả lập breaker cấu hình sai (`breaker.min_calls: 1000000000`) và đặt
   `max_attempts_per_provider: 5`. Chạy G2, G3 hai lần: (a) giữ retry budget; (b) tắt budget
   (`retry_budget_ratio: 100`, `retry_budget_max_tokens: 1.0e9`, `retry_budget_min_per_s: 1.0e6`).
   - Kết quả G2: Khi giữ budget (a), `amplification` là 1.378, `retry_ratio` là 0.058, `recovery_s` = 0.0s, và chỉ có 4 request bị rớt vì deadline (`give_up_reasons: {'deadline': 4}`). Khi tắt budget (b), `amplification` vọt lên 1.675, `retry_ratio` tăng gần 9 lần lên 0.495, `recovery_s` kéo dài 25.0s, và số request chết vì deadline tăng vọt lên 79.
   - Kết quả G3: Khi tắt budget, `retry_ratio` tăng từ 0.068 lên 0.159.
   - Khi breaker hoạt động đúng, nó ngắt provider hỏng nên budget ít khi cạn; tuy nhiên vẫn bắt buộc phải có cả hai vì breaker chỉ bảo vệ theo provider cục bộ, trong khi retry budget bảo vệ toàn hệ thống trước các lỗi diện rộng hoặc khi breaker bị cấu hình sai / lỗi logic, đóng vai trò chốt chặn an toàn cuối cùng ngăn chặn bão retry.

2. **`attempt_timeout_s: 30`.** Chạy G6 (và G4 để đối chứng). Nhìn **availability** và `give_up_reasons`
   trước, rồi mới nhìn p95. Lưu ý: `latency_p95_s` chỉ tính trên *lượt thành công*, nên khi nhiều lượt treo 30 s
   rồi hết hạn, p95 có thể *giảm* trong khi availability tụt (survivorship bias).
   - Ở G6: Khi timeout tăng từ 4.0s lên 30.0s, availability sụt giảm nghiêm trọng từ 1.0000 (100%) xuống 0.9488 (94.88%), với 122 request thất bại do hết hạn (`give_up_reasons: {'deadline': 107, 'no_provider_available': 15}`). Tuy nhiên, `latency_p95_s` trên các request thành công lại *giảm* từ 5.958s xuống 5.362s. Đây là hiện tượng *survivorship bias*: các request bị treo 30s đều thất bại và bị loại khỏi mẫu tính p95, chỉ còn các request may mắn route sớm sang beta/gamma được tính.
   - Ở G4: Availability giữ nguyên 1.0000 và p95 gần như không đổi (5.684s vs 5.608s). Lý do G4 không bị ảnh hưởng là vì G4 chỉ chậm ở mức ~12s, circuit breaker phát hiện slow call và chủ động failover sang `alpha-eu` ngay sau ngưỡng slow_call (5s), trước khi timeout 30s bị chạm tới.
   - Timeout "đúng" phải được thiết lập dựa trên: (p99 latency lúc bình thường) + (thời gian tối thiểu cần thiết để kịp failover sang fallback provider trước khi hết turn deadline 30s của người dùng), tuyệt đối không đặt bằng hoặc lớn hơn turn deadline.

3. **Bỏ lọc tool calling khỏi `ladder()`.** Chạy G6. Giải thích con số `incompatible_routes` bằng ngôn ngữ của
   một product manager.
   - Khi bỏ điều kiện lọc `supports_tools`, availability của G6 tụt từ 1.0000 xuống 0.8977 (thất thoát hơn 10% doanh thu), và ghi nhận chính xác 244 trường hợp `incompatible_routes` (`give_up_reasons: {'incompatible_model_output': 244}`).
   - Dưới góc nhìn Product Manager: Con số 244 đại diện cho 244 khách hàng VIP đang thực hiện tác vụ thanh toán/kiểm tra đơn hàng (yêu cầu gọi tool) nhưng hệ thống lại điều hướng nhầm sang mô hình giá rẻ `gamma-mini` (vốn không có khả năng hiểu và gọi tool). Hậu quả là mô hình trả về văn bản rác vô nghĩa thay vì thao tác nghiệp vụ, làm giao dịch đổ vỡ trực tiếp, gây ức chế tột độ cho khách hàng và làm tổn hại nghiêm trọng uy tín thương hiệu.

## 4. Workflow hoàn tiền

| Executor | completion | crashes | duplicate refunds | duplicate emails | lost refunds | overpaid | wasted LLM / crash |
|---|---:|---:|---:|---:|---:|---:|---:|
| naive | 1.0 | 81 | 128 | 81 | 0 | 1496700 | 1.123 |
| yours | 1.0 | 66 | 0 | 0 | 0 | 0 | 0.303 |

Trả lời: vì sao chỉ idempotency key là **chưa đủ**, và chỉ checkpoint cũng **chưa đủ**? Điều gì xảy ra nếu ghi
checkpoint *trước* khi gọi payment?
- Vì sao chỉ idempotency key là **chưa đủ**: Idempotency key bảo đảm bên thứ ba (cổng thanh toán, dịch vụ email) không xử lý trùng giao dịch. Tuy nhiên, nếu thiếu checkpoint, worker mới khi resume sẽ buộc phải chạy lại từ bước đầu tiên (gọi LLM phân loại request, truy vấn DB), dẫn đến lãng phí tài nguyên và chi phí token rất lớn (`wasted_llm_per_crash` = 1.123 ở naive).
- Vì sao chỉ checkpoint cũng **chưa đủ**: Nếu worker gặp sự cố chết đột ngột ngay sau khi cổng thanh toán đã trừ tiền nhưng trước khi executor kịp ghi checkpoint vào `DurableStore`, worker mới resume sẽ thấy chưa có checkpoint và gọi lại lệnh hoàn tiền. Nếu không có idempotency key ổn định, ngân hàng sẽ hoàn tiền lần thứ hai, gây thất thoát tài chính nghiêm trọng (naive bị overpaid tới 1,496,700 cents).
- Điều gì xảy ra nếu ghi checkpoint *trước* khi gọi payment: Nếu ghi checkpoint trước khi gọi thanh toán và worker bị crash ngay lúc đó (hoặc cổng thanh toán gặp lỗi mạng), lần resume tiếp theo hệ thống sẽ lầm tưởng tiền đã được hoàn và bỏ qua bước này. Hậu quả là khách hàng bị mất tiền hoàn (`lost_refunds > 0`), vi phạm pháp lý và cam kết dịch vụ.

## 5. Alerting & error budget

| Sự cố | Mong đợi | Naive alerter | Burn-rate alerter | Độ trễ phát hiện |
|---|---|---|---|---:|
| checkout_bug | page | page (delay 1m) | page | 27 min (max 35 min) |
| provider_outage | page | page (delay 0m) | page | 4 min (max 8 min) |
| deploy_blip | none | page (FALSE PAGE) | none (không page) | - |
| slow_leak | ticket | none (BỎ SÓT) | ticket | 1337 min (max 1800 min) |
| degraded_evening | page | page (delay 214m) | page | 291 min (max 330 min) |
| night_spike | none | page (FALSE PAGE) | none (không page) | - |
| small_bump | none | page (FALSE PAGE) | none (không page) | - |

Error budget 30 ngày: đã tiêu 42.95%, còn 57.05%. Có đóng băng release không? **Không đóng băng release** (`freeze_releases: false` vì budget còn lại dương, > 0%).

Vì sao `small_bump` (20 phút ở 12% lỗi) **không** nên đánh thức ai lúc 11 giờ trưa?
- Trong 20 phút với 12% lỗi, lượng budget bị đốt cháy chỉ tương đương $20 \times (0.12 - 0.005) \approx 2.3$ phút lỗi, chiếm chưa đầy 1% tổng ngân sách lỗi của cả tháng ($0.01$). Sự cố tự giới hạn và tự phục hồi nhanh chóng mà không gây tổn hại đáng kể đến SLO 99.5% của tháng.
- Nếu gửi alert page, kỹ sư on-call sẽ bị đánh thức và gián đoạn công việc không cần thiết, dẫn đến hội chứng mệt mỏi vì cảnh báo (alert fatigue). Multi-window burn-rate alerter với cửa sổ dài 1 giờ yêu cầu sự cố phải tiêu thụ ít nhất 2% ngân sách lỗi thì mới kích hoạt page, nhờ đó đã triệt tiêu hoàn toàn false page này.

## 6. Postmortem — Băng G2 với client naive (blameless)

*Dùng `make plot` (hình `reports/gameday/G2_overload_storm.png`) và CSV `reports/gameday/naive_G2_overload_storm_timeseries.csv`.*

**Tóm tắt (3 câu):**
1. Vào giây ảo $t=120$, provider chính `alpha` bị mất 75% năng lực xử lý (mất phần lớn cụm GPU phục vụ).
2. Do `NaiveClient` không có cơ chế backoff có jitter, circuit breaker hay giới hạn retry, các client đã liên tục dồn dập gửi lại request lỗi, đẩy tải lên tới 163 attempts/s và làm tràn nghẽn hoàn toàn hàng đợi của provider.
3. Dù `alpha` đã phục hồi 100% tài nguyên tại $t=300$, hệ thống rơi vào trạng thái hỏng hóc siêu bền (metastable failure) kéo dài thêm 155 giây do hàng đợi ngập tràn request hết hạn mà client đã bỏ, và chỉ hồi phục tại $t=455$.

**Ảnh hưởng:**
- 1,285 lượt người dùng thất bại hoàn toàn (availability chỉ đạt 45.60%, tức chỉ có 1077/2362 turns thành công).
- Thời gian gián đoạn kéo dài 335 giây ảo (từ $t=120$ đến $t=455$), trong đó có tới 155 giây xảy ra sau khi hạ tầng provider đã bình phục 100%.
- Tiêu tốn 54.4% lượng request lỗi trong đợt tải, vượt quá gấp nhiều lần mức độ cho phép của error budget nếu tính theo khung thời gian xảy ra sự cố.

**Timeline** (giây ảo — lấy từ CSV):

| t | Sự kiện | Bằng chứng (số) |
|---:|---|---|
| 115 | Hệ thống hoạt động bình thường trước sự cố | calls_ok = 11, calls_done = 11, attempts_alpha = 9 (100% success) |
| 120 | alpha mất 75% năng lực | calls_ok = 7, calls_done = 7, attempts_alpha = 5 (bắt đầu nghẽn) |
| 150 | Hàng đợi tràn hoàn toàn, bão retry bùng phát | attempts_alpha vọt lên 163 attempts/s, calls_ok = 0 |
| 180 | Client bắt đầu chạm ngưỡng call deadline (30s) | attempts_alpha = 138 attempts/s, calls_ok = 0 |
| 240 | Tải tiếp tục duy trì ở mức cực hạn | attempts_alpha = 105 attempts/s, calls_ok = 0 |
| 300 | alpha hồi phục hoàn toàn năng lực xử lý (100%) | attempts_alpha = 115 attempts/s, calls_ok = 0 (hệ thống chưa hồi phục) |
| 350 | Metastable failure tự duy trì | attempts_alpha = 17 attempts/s, calls_ok = 0 (vẫn ngập request quá hạn) |
| 400 | Hàng đợi quá hạn bắt đầu được xử lý vãn | attempts_alpha = 51 attempts/s, calls_ok = 0 |
| 455 | Hàng đợi xả hết rác, hệ thống hồi phục hoàn toàn | calls_ok = 4, calls_done = 5, attempts_alpha = 13 (recovery_s = 155.0s) |

**Trigger vs root cause:**
- **Trigger:** Provider `alpha` mất 75% năng lực phần cứng tại $t=120$.
- **Root cause:** Thiếu toàn diện các cơ chế tự vệ phân tán ở phía client:
  1. Không có Circuit Breaker để ngắt tải khi provider sập và chuyển hướng sang `alpha-eu` hay `beta`.
  2. Retry 3 tầng không có Exponential Backoff và Full Jitter dẫn đến hiện tượng cộng hưởng tải đồng pha.
  3. Không có Retry Budget để kìm hãm số lượng retry, khiến hệ thống tự khuếch đại tải lên gấp gần 10 lần (`amplification` = 9.904).
- **Vòng lặp metastable tự duy trì:** Client gửi request $\rightarrow$ Hàng đợi provider đầy $\rightarrow$ Request nằm chờ quá turn deadline 30s nên client từ bỏ và gửi retry mới $\rightarrow$ Provider tại $t > 300$ dù đã khỏe lại nhưng vẫn bận rộn xử lý các request cũ đã bị client bỏ (lãng phí 100% năng lực CPU/GPU) $\rightarrow$ Request mới tiếp tục bị xếp hàng và quá hạn. Vòng lặp chỉ kết thúc khi client giảm tốc độ gửi và hàng đợi rác được dọn sạch tại $t=455$.

**Điều gì giúp / điều gì làm tệ hơn:**
- *Làm tệ hơn:* Retry mù quáng không backoff; tiếp tục xử lý các request đã quá hạn mà không có deadline propagation / cancellation token.
- *Giúp ích:* `turn_deadline_s` (30s) giúp client cuối cùng cũng phải give up thay vì retry vô tận, cho phép lưu lượng giảm dần sau $t=300$.

**Action items** (cụ thể, có owner, đo được):

| # | Hành động | Loại (prevent / detect / mitigate) | Owner | Cách kiểm chứng |
|---|---|---|---|---|
| 1 | Triển khai Sliding-window Circuit Breaker tự động ngắt mạch khi failure rate $\ge 50\%$ và failover sang fallback | Mitigate | Core Reliability Team | Availability $\ge 99\%$ và recovery_s = 0.0s trên G2 |
| 2 | Bổ sung Retry Budget (tối đa 10% first attempts) và Exponential Backoff với Full Jitter | Prevent | Client SDK Team | Amplification $\le 1.1$ trên toàn bộ testsuite Game Day |
| 3 | Triển khai Multi-window Multi-burn-rate Alerting với SLO 99.5% phát hiện fast-burn trong 30 phút | Detect | SRE Monitoring Team | 0 false page trên bộ dữ liệu 30 ngày trong CI |

## 7. Một điểm yếu còn lại

- **Điểm yếu còn lại của ResilientClient:**
  Trong kịch bản G2 và G4, khi provider chính `alpha` bị nghẽn hoặc chậm nhẹ, cơ chế failover tĩnh (`alpha -> alpha-eu -> beta`) giúp duy trì availability 100%, nhưng đẩy chi phí tăng cao (`cost/good_turn` từ 2.0 lên 2.13-2.50) vì `beta` có chi phí cao hơn 40% (`cost_per_call = 1.4`). Đồng thời, latency p95 của G2 vẫn chạm mức 11.2s do nhiều request phải chờ hết `attempt_timeout_s` (8.0s) trên alpha trước khi failover.
- **Hướng khắc phục:**
  Triển khai kỹ thuật **Hedged Requests (Speculative Retry)**: Thay vì chờ hết 8s timeout, client theo dõi phân phối latency và tự động bắn một request thăm dò song song sang `alpha-eu` nếu request đầu tiên chưa phản hồi sau ngưỡng p90 (khoảng 3.0s). Đồng thời, thay thế ladder tĩnh bằng cơ chế **Power of Two Choices (P2C) Load Balancing** dựa trên số lượng request đang xử lý (inflight requests) và tỷ lệ chi phí, giúp tối ưu hóa cả p95 độ trễ lẫn chi phí vận hành.

## 8. Cache (Part G)

Số từ `reports/cache/*.json` (`make cache`).

| Client | availability | faq_availability | cache_hit_ratio | stale_served | wrong_answers | outdated_answers | cost/turn | PASS? |
|---|---:|---:|---:|---:|---:|---:|---:|:---:|
| resilient (không cache) | 0.8373 | 0.8424 | 0.000 | 0 | 0 | 0 | 1.386 | FAIL |
| cached (của bạn) | 0.9124 | 0.9645 | 0.652 | 157 | 0 | 3 | 0.915 | PASS |

| Tham số `cache:` | Giá trị | Vì sao (số liệu nào) |
|---|---:|---|
| similarity_threshold | 0.90 | Điểm cân bằng tối ưu: Tại 0.80 phát sinh 175 câu trả lời sai do nhầm ngữ nghĩa; tại 0.97 hit ratio tụt xuống 57.9%. Giá trị 0.90 đạt hit ratio 65.2% và 0 câu trả lời sai (`wrong_answers = 0`). |
| ttl_s | 60 | Khống chế thông tin lỗi thời: Đặt 60s giữ số câu trả lời quá hạn ở mức an toàn (`outdated_answers = 3` $\le 5$), trong khi đặt 300s làm số câu lỗi thời tăng vọt lên 95. |
| max_stale_s | 300 | Cứu vãn độ sẵn sàng khi sự cố: Cho phép phục vụ dữ liệu stale trong 300s giúp cứu được 157 lượt hỏi FAQ trong đợt outage của C1, đưa `faq_availability` từ 87.33% lên 96.45%. |

Thí nghiệm (mỗi cái 2–4 câu + số):

1. **Ngưỡng.** Chạy C1 với `similarity_threshold` = 0,80 / giá trị của bạn (0.90) / 0,97. Ghi hit ratio, `wrong_answers`, cost.
   - Tại ngưỡng 0.80: `cache_hit_ratio` = 0.659, nhưng phát sinh tới 175 câu trả lời sai (`wrong_answers: 175`), toàn bộ đều do nhận nhầm câu hỏi (`wrong_answers_by_reason: {'other_question': 175}`), chi phí bị đội lên 0.978 và `faq_availability` rơi xuống 86.05%.
   - Tại ngưỡng 0.90: `cache_hit_ratio` = 0.652, `wrong_answers` = 0, chi phí tối ưu nhất ở mức 0.915, `faq_availability` đạt 96.45%.
   - Tại ngưỡng 0.97: `wrong_answers` = 0 nhưng ngưỡng quá khắt khe khiến `cache_hit_ratio` tụt xuống 0.579, chi phí tăng lên 0.965.
   - Kết luận: Ngưỡng 0.80 quá lỏng lẻo làm ghép nhầm các câu hỏi khác nhau nhưng có embedding gần nhau; ngưỡng 0.90 là ranh giới chuẩn xác tuyệt đối loại bỏ hoàn toàn false positive.

2. **TTL.** Chạy với `ttl_s` = 30 / 60 / 300. Vì sao webhook không giúp được gì cho `outdated_answers`, và TTL là thứ duy nhất giới hạn nó?
   - Tại TTL = 30s: `outdated_answers` = 2, `hit_ratio` = 0.592.
   - Tại TTL = 60s: `outdated_answers` = 3, `hit_ratio` = 0.652.
   - Tại TTL = 300s: `outdated_answers` tăng vọt lên 95 (vượt xa ngưỡng cho phép 5 của rubric), dù `hit_ratio` đạt 0.731.
   - Webhook không giúp được gì cho `outdated_answers` vì webhook chỉ được backend phát đi khi tài liệu nguồn đã được xuất bản hoàn tất; trong khoảng thời gian backend đang sửa đổi nội dung hoặc khi kết nối webhook bị trễ/mất gói, client không hề nhận được tín hiệu invalidate. Do đó, TTL chính là chốt chặn thời gian thực duy nhất giới hạn tuổi thọ của dữ liệu và ép cache phải tự làm mới.

3. **Stale-if-error.** Chạy với `max_stale_s: 0` vs `300`. `faq_availability` thay đổi thế nào?
   - Khi tắt stale (`max_stale_s: 0`): `stale_served` = 0, `faq_availability` tụt thẳng xuống 87.33% (FAIL tiêu chí $\ge 95\%$), và tổng availability tụt xuống 85.65%.
   - Khi bật stale (`max_stale_s: 300`): Cache đã phục vụ thành công 157 lượt trả lời stale (`stale_served = 157`), kéo `faq_availability` phục hồi lên 96.45% và tổng availability lên 91.24%.

Trả lời:

- Vì sao câu hỏi cá nhân ("Đơn #48213 của tôi đang ở đâu?") **không** được cache, kể cả khi cache theo từng user?
  1. Trạng thái đơn hàng biến đổi liên tục theo thời gian thực (đang xử lý $\rightarrow$ đã xuất kho $\rightarrow$ đang giao). Nếu cache, khách hàng sẽ nhận thông tin trạng thái cũ sai lệch, gây bức xúc và khiếu nại.
  2. Câu hỏi chứa dữ liệu định danh cá nhân (PII, mã vận đơn, số điện thoại). Việc lưu trữ các thông tin này trong Semantic Cache tiềm ẩn rủi ro rò rỉ dữ liệu qua phân tích embedding và vi phạm các quy định bảo mật (GDPR/Bảo vệ dữ liệu người dùng).

- Trong lúc mọi provider sập, dashboard vẫn xanh nhờ cache. Bạn sẽ alert dựa trên số nào để on-call vẫn biết có sự cố?
  - Không được dựa vào user-facing availability hay mã phản hồi 200 ở gateway vì cache đang che giấu lỗi.
  - Phải alert dựa trên **Upstream Provider Error Rate / Failure Rate** (tỷ lệ lỗi thực tế từ các cuộc gọi backend LLM) và **Tỷ lệ phục vụ dữ liệu Stale** (`stale_served / total_requests`). Khi tỷ lệ stale_served tăng bất thường hoặc upstream error rate vượt ngưỡng SLO, hệ thống giám sát phải lập tức phát cảnh báo page on-call để xử lý hạ tầng trước khi hết thời gian `max_stale_s` (300s) khiến toàn bộ cache cạn kiệt và sụp đổ.

