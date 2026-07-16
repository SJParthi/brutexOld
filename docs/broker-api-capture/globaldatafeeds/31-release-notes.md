# GlobalDataFeeds — Release Notes

Scope: Capture of the Release Notes page on globaldatafeeds.in, covering API, NimbleDataPro, NimbleExcel and NinjaTraderClient release entries.

Source URLs:
- https://globaldatafeeds.in/release-notes/

Capture notes:
- The page presents release notes under four product tabs: **API**, **NimbleDataPro**, **NimbleExcel**, **NinjaTraderClient**. In the server-rendered HTML the tab panels are output sequentially without explicit tab headings, so entries below are preserved in the exact order they appear; the apparent tab groupings are annotated. No pagination was observed on this page (single page).
- Page metadata: last modified 2026-03-16T12:12:34+00:00 (per meta article:modified_time).

---

## Release Notes

> Source: https://globaldatafeeds.in/release-notes/ — captured 2026-07-15

Product tabs on page: API | NimbleDataPro | NimbleExcel | NinjaTraderClient

### (Tab group 1 — API)

**29th October 2025**

*New Features*
- Added new BFO and NFO pre-open logic.

**10th October 2025**

*New Features*
- Added SubscribeExchangeSnapshot functionality.

**11th September 2025**

*New Features*
- Added GetVolumeShockers functionality.

**03rd September 2025**

*New Features*
- Introduced SubscribeSnapshotGreeks and GetSnapshotGreeks APIs.

**20th September 2025**

*New Features*
- Enhanced GetHistoryGreeks by introducing periodicity and period support.

**20th August 2025**

*New Features*
- Enhanced GetHolidays and GainersLosers by adding missing filters.

**11th July 2025**

*Bug Fixes*
- Resolved key-related issue in GetInstruments.

**01st July 2025**

*New Features*
- Enhanced GetHolidays function by adding empty string handling for all exchanges.

**22nd August 2024**

*New Features*
- Introduced newly launched GetHistoryGreeks function
- GetHistoryGreeks - fetches the history data of Greeks for NFO exchange.

**25th September 2024**

*New Features*
- Introduced newly launched functions
- SubscribeRealtimeOptionChain – fetches data of Options automatically at periodic intervals.
- SubscribeRealtimeOptionChainGreeks - fetches data of OptionGreeks automatically at periodic intervals.
- SubscribeRealtimeGainersLosers - function is designed to subscribe and return data of the top gainers and top losers in a specific exchange.
- GetLastGainersLosers - function is designed to get and return data of last top gainers and top losers in a specific exchange.

*Notes*
- Available for WebSockets API – will be available in DotNet and COM APIs soon

**02nd February 2024**

*New Features*
- Added csv methods to api clients:
- string GetCsv()
- string[] GetCsvLines
- void SaveToCsv(string path, bool append)

*Notes*
- Response of all API functions can also be fetched in CSV format.

### (Tab group 2 — NimbleDataPro)

**22nd May 2025**
- Introduced 3-day backfill support.
- Removed SubscribeSnapshot and implemented a message box notification when the limitation is reached.

**28th April 2025**
- NCX has been added.

**08th April 2024**
- Fix memory leak

**5th April 2024**
- Fix license transfer dialog inconsistency

**28th March 2024**
- Fix license transfer dialog inconsistency

**21st March 2024**
- Fix UI on resize and Add NA on no limits response

**13th March 2024**
- Change Exchanges overall handling

**11th March 2024**
- Change Exchanges RT updates

**07th March 2024**
- Same symbol exchange changes

**05th March 2024**
- Fix Symbol Search crash
- Fix infinite WAIT for history load

**21st February 2024**
- Added BSE,BSE_DEBT,BSE_IDX,BFO exchanges handling

### (Tab group 3 — NimbleExcel)

**30th October 2025**
- Added
  - SubscribeSnapshotGreeks
  - GetSnapshotGreeks
  - GetHistoryGreeks
  - GetVolumeShockers
  - GetInstrumentsOnSearch

**20th August 2025**
- Added missing Holidays and GainersLosers/Instruments filters for stream connectors.

**21st July 2025**
- Disabled unsubscribe when 'fake' realtime updates occur.

**22nd August 2024**
- Fix memory leak

**13th August 2024**
- Add Nimble_GetHistoryGreeks

**05th August 2024**
- Fix SubscribeRealtimeOptionChain
- SubscribeRealtimeOptionChainGreeks
- Nimble_SubscribeRealtimeGainersLosers

**30th May 2024**
- Add History from to request

**17th May 2024**
- Add Nimble_SubscribeRealtimeOptionChain
- Nimble_SubscribeRealtimeOptionChainGreeks
- Nimble_SubscribeRealtimeGainersLosers
- Nimble_GetLastGainersLosers

**20th February 2024**
- Fix snapshot missing Day periodicity and snapshot missing data if requesting BackAdjusted symbol

**13th March 2024**
- Simplify Continue , add Enable on check setting
- Add symbol limit display
- Fixes and updates

**13th March 2024**
- Simplify Continue , add Enable on check setting
- Add symbol limit display
- Fixes and updates

**13th March 2024**
- Simplify Continue , add Enable on check setting
- Add symbol limit display
- Fixes and updates

### (Tab group 4 — NinjaTraderClient, versioned entries)

**V.009 – 08th April 2024**
- Fix memory leak

**V.008 – 5th April 2024**
- Fix license transfer dialog inconsistency

**V.007 – 28th March 2024**
- Fix license transfer dialog inconsistency

**V.006 – 21st March 2024**
- Fix UI on resize and Add NA on no limits response

**V.005 – 13th March 2024**
- Change Exchanges overall handling

**V.004 – 11th March 2024**
- Change Exchanges RT updates

**V.003 – 07th March 2024**
- Same symbol exchange changes

**V.002 – 05th March 2024**
- Fix Symbol Search crash
- Fix infinite WAIT for history load

**V.000 – 21st February 2024**
- Added BSE,BSE_DEBT,BSE_IDX,BFO exchanges handling

---

Related: the Fundamental Data API documentation site maintains its own release notes page at https://docs.globaldatafeeds.in/release-notes-926014m0 (not captured in this file; see 28-fundamental-data-api.md site index).
