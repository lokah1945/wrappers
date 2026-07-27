# Fix NOUS - implement SEMUA temuan, minimal-diff

Scope: HANYA /root/wrapper/nous/. JANGAN: restart service, run server, git commit/push, edit di luar nous/, sentuh nous/.env (boleh nous/.env.example).

Baca dulu suggested-fix di:
- audit_report/parts/2026-07-27_part_nous_nvidia.md section 1 (kerjakan SEMUA N-* HIGH/MED/LOW)
- audit_report/parts/2026-07-27_part_transparency.md section 1 (N1-N36)
- audit_report/parts/2026-07-27_part_latency.md (F3, F8)
- audit_report/parts/2026-07-27_part_infra_runtime.md (F6 JSONDecodeError)

## A. Stabilitas (semua N-* section 1)
1. N-01: try/finally post_nous network errors (aiohttp.ClientError, TimeoutError) -> in-flight slot selalu release, client dapat 502 shaped bukan 500 mentah.
2. N-02: bound _RESPONSE_STORE cap 200 FIFO seperti nvidia responses_compat._bounded_store (baca nvidia-python/src/responses_compat.py baris 28-51).
3. N-03: /dashboard wajib auth + hapus embed bearer token dari HTML.
4. Kerjakan SEMUA N-* lain sesuai suggested fix: N-05 TimeoutError conflation, N-07 GeneratorExit-safe finalization, N-11 SEC3 max-tokens cap + per-IP rate limit di /v1/messages dan /v1/responses, N-13 .env hot-reload, dan semua MED/LOW.
5. Infra F6: catch JSONDecodeError body request di semua POST handler -> 400 shaped, bukan traceback 500.

## B. Latency
6. F3: pindahkan await tulis SQLite (record_model_result / MODEL_STORE.record_status_async / record_error_async) ke asyncio.create_task fire-and-forget dengan logging exception.
7. F8: cache token OAuth AUTH_PATH di memory; re-read hanya saat auth gagal atau mtime file berubah.

## C. Transparansi (part transparency section 1) - prinsip: transparan by default
8. N3: STOP hapus n/logprobs/logit_bias/user/frequency_penalty/presence_penalty di /v1/chat/completions - forward semua. Jika upstream verifiably reject, drop hanya via env NOUS_DROP_PARAMS (comma list, default kosong).
9. N19/N20: hapus floor max_tokens >=1024 dan JANGAN override nilai eksplisit client di /v1/responses dan /v1/messages. Default 4096 hanya bila field absen DAN upstream membutuhkannya.
10. N21: perluas passthrough param di /v1/responses dan /v1/messages: forward juga top_k, stop/stop_sequences (mapped), stream_options, parallel_tool_calls, seed bila ada.
11. N28: stop_reason=tool_use HANYA bila finish_reason upstream memang tool_calls; selain itu map faithful.
12. N29: DSML delta suppression tetap (proteksi tool-leak) tapi buat switchable via env NOUS_DSML_FILTER (default on) dan dokumentasikan.
13. N31: echo string model yang DIKIRIM CLIENT di respons saat alias di-resolve (resolved id tetap dipakai internal).
14. PERTAHANKAN: heartbeat, synthetic [DONE] saat EOF abnormal, error normalization, alias resolution (dilisensikan kontrak).
15. Dokumentasikan semua env flag baru di komentar atas file dan nous/.env.example.

## Verifikasi
- python3 -m py_compile nous/wrapper_nous.py setelah tiap batch edit.
- Jangan start server. Boleh import-test dengan env dummy jika aman (kalau import memicu side effect network, cukup py_compile + baca ulang).

## Output
Tabel markdown: finding ID -> baris berubah -> apa yang dilakukan -> status verifikasi. Sebutkan yang sengaja di-skip + alasan.

## B. Latency
6. F3: pindahkan await tulis SQLite (record_model_result / MODEL_STORE.record_status_async / record_error_async) ke asyncio.create_task fire-and-forget d
