# TrueData — Limits, Errors, Maintenance & Official FAQ

> Sources (all fetched 2026-07-15):
> - https://feedback.truedata.in/knowledge-base/article/requestscalls-limit-via-historical-and-real-time-api
> - https://feedback.truedata.in/knowledge-base/article/historical-data-availability-through-rest-api
> - https://feedback.truedata.in/knowledge-base/article/historical-real-time-data-availability-through-market-data-api
> - https://feedback.truedata.in/knowledge-base/article/user-already-connected-error-solution-is-force-logout
> - https://feedback.truedata.in/knowledge-base/article/maintenance-timings-for-truedata-market-data-api
> - https://feedback.truedata.in/knowledge-base/article/how-can-we-get-multiple-symbol-data-in-one-go-for-historical-api-call
> - https://feedback.truedata.in/knowledge-base/article/want-to-200-symbol-historical-data-in-one-go-like-live-streaming
> - https://feedback.truedata.in/knowledge-base/article/using-threading-at-clients-end-market-data-api
> - https://feedback.truedata.in/knowledge-base/article/at-330-pm-the-data-i-e-for-high-previous-close-etc-values-are-coming-same-as-ltp
> - https://feedback.truedata.in/knowledge-base/faqs/market-data-api (FAQ index)

## 1. Rate limits — Historical REST API (official)

| Data type | Per second | Per minute | Per hour |
|---|---|---|---|
| Tick Data | 5 | 300 | 18000 |
| Minutes Bar Data | 10 | 600 | 18000 |

## 2. Real-time WebSocket controls (official, verbatim)

"For Real Time Data through WebSocket API, there is Connection & Symbols control.

- Connection Control >> You can Connect only from 1 place at a time (1 login instance)
- Symbols Control >> addSymbol / removeSymbol / getMarketStatus / logout - all combined - As per your Plan's symbols limit"

"For the Real-Time API, there is no such limit. Symbols once added would continue to flow in a streaming manner."

## 3. Historical data availability windows

- Tick Data — Last 5 Trading Days
- Bar Data (1/2/3/5/10/15/30/60 Min) — Last 6 Months
- Daily Bars — 10+ Years
- Extended History planned as a paid Add-on ("We are soon going to be enabling Extended History").

## 4. Known errors & remedies

### "User Already Connected"

Cause: a session is already logged in elsewhere (only 1 login instance allowed). Remedy (official):

```
https://api.truedata.in/logoutRequest?user=xxx&password=yyy&port=8082
```

Fire the URL, wait ~5 minutes, then log in again.

### "No complete bhavcopy found for requested date. Last available for <yyyy-mm-dd hh:mm:ss>."

Returned by getBhavCopy when the segment's bhavcopy hasn't arrived yet; re-request with the provided date if you want the last available one.

### OHLC == LTP at 3:30 PM

Not an error: the 15:30 bar can contain at most one tick (market closes 15:30:00), so O=H=L=C. There may be no 15:30 bar at all.

### Rate-limit responses

Historical over-limit calls are throttled server-side; the official Python lib "handles rate limiting gracefully" (truedata-ws 4.2.3+). Keep within §1 limits.

## 5. Maintenance windows

| Schedule | Time | What |
|---|---|---|
| Trading weekdays | 07:30–08:00 | TrueData WebSocket API server maintenance |
| Saturday & Sunday | 07:30–10:30 | TrueData WebSocket API server maintenance |

Ad-hoc REST maintenance (e.g. mock-data ingestion days) is announced on Telegram (https://t.me/truedata_ws_api).

## 6. Official Market-Data-API FAQ list (feedback.truedata.in)

Complete FAQ set with answers captured in this archive:

1. **Can I get multiple symbol data in one go for Historical API call?** — No; "Historical data needs to be requested and can be delivered 1 by 1. Currently, there is no method to send you the history of multiple symbols at the same time. However, as we move to REST protocol for history, we may consider adding this for a bunch of symbols."
2. **Need 200 Symbol Historical data in one go like Live Streaming** — Use 1-min/5-min bar streaming: at the end of every minute you get the last bar for all 200+ symbols "in a streaming fashion and without calling the server … within 1 second of the completion of the bar"; save to a DB to stay current. Bar streaming must be activated from the backend. Possible combinations: Tick / 1 min / 5 min / Tick+1 min / Tick+5 min / Tick+1 min+5 min.
3. **How to Fetch Real-Time Data on the Sandbox page?** — wstest.truedata.in → Select RT (8086) → enter username/password → Add Symbols → type symbols in Request → Send Text → data streams in the log.
4. **What are the fields included in Real-Time Tick Streaming?** — Symbol ID, Date-Time, LTP, LTQ, ATP, TTQ, Open, High, Low, Prev Close, OI, Prev Open Int Close, Day's Turnover, Special Tag ("OHL"/"H"/"L"/""), Tick Sequence No, Bid, Bid Qty, Ask, Ask Qty. (Full table: 03-realtime-websocket.md §5.)
5. **What are the fields included in Real-Time 1 Minute Bar Data Streaming?** — Symbol ID, Date-Time, bar_open, bar_high, bar_low, bar_close (LTP), bar volume, OI.
6. **At 3:30 PM OHLC == LTP** — see §4 above (single-tick bar).
7. **Request/Call Limit** — see §1/§2.
8. **Using Threading at Client's End** — "Yes, you should use threads in your own code while dealing with the multiple symbols feed."
9. **Historical Data Availability Through REST API** — see §3.
10. **How to get EOD Data in Sandbox** — login on wstest.truedata.in → symbol → Interval=EOD → duration → Get. (Python: use `bar_size='eod'`.)
11. **Full Market Feed Replay is Now Live** — replay.truedata.in:8082; see 03-realtime-websocket.md §10.
12. **User Already Connected Error: Solution is Force Logout** — see §4.

## 7. Practical client-side rules collected from official docs

- Wait ~1 s after `start_live_data` before reading touchline data.
- Insert `time.sleep(0.05)`–`0.1` in polling loops ("important otherwise cpu will overthrottle").
- Don't block callback functions (they run on the WS thread).
- Re-enable the production URL (`push.truedata.in`) before live market after using replay.
- Upgrade libraries via `pip install --upgrade truedata_ws` / `pip install --upgrade truedata`; join the Telegram channel for maintenance/issue notices — it is TrueData's "first channel for information transfer".
- Historical Bid-Ask and greeks/bar streams are account entitlements — request activation via apisupport@truedata.in.

## 8. Support channels

- Raise a ticket: https://feedback.truedata.in/ticket/add
- API forum (WebSocket API category): https://feedback.truedata.in/topics/all/status/all/category/30/sort/new/page/1
- Knowledge base: https://feedback.truedata.in/knowledge-base
- Telegram: https://t.me/truedata_ws_api
- Phone: +91-7304-22-44-66; email: apisupport@truedata.in / support@truedata.in
