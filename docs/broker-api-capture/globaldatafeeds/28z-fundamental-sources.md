# SOURCES — Fundamental Data API & News API gap-fill (2026-07-15)

Manifest of every URL fetched in the gap-fill run that produced files `28a`–`28i`, `28x1`–`28x3` and `29a`. All pages were retrieved via the sites' raw-markdown export (`<page-url>.md` per the LLMs.txt convention). Status legend: **ok** = captured verbatim · **failed** = both `.md` and HTML forms returned an empty body (nothing usable to archive; not fabricated).

Fetch totals: 123 unique URLs attempted (57 endpoint/guide + 61 schema + 5 news), 119 ok, 4 failed. 3 additional HTML-fallback retries were made for the failed FinRatio pages (also empty).

## docs.globaldatafeeds.in — guides & WS API (file 28a)

| URL (.md form) | Status | Archived in |
|---|---|---|
| /code-samples-api-trial-923683m0.md | ok | 28a |
| /type-of-corporate-data-available-1142925m0.md | ok | 28a |
| /subscribecorporateannouncements-2174523m0.md | ok | 28a |
| /subscribefinancialresults-2181121m0.md | ok | 28a |

## docs.globaldatafeeds.in — Corporate Data APIs (RESTful) (files 28a–28d)

| URL (.md form) | Status | Archived in |
|---|---|---|
| /getcorporateannouncementscategories-16005463e0.md | ok | 28a |
| /getcorporateactionscategories-16356140e0.md | ok | 28a |
| /getcorporateactions-15575576e0.md | ok | 28a |
| /getresultscalendar-15575583e0.md | ok | 28a |
| /getfinancialresultsitems-15575584e0.md | ok | 28b |
| /getfinancialresults-15575585e0.md | ok | 28b |
| /getfinancialratios-27153098e0.md | ok | 28b |
| /getfinratioeod-19453261e0.md | **failed** (empty body; HTML also empty — page retired, functionality covered by GetFinancialRatios type=EOD) | — |
| /getfinratioquarterly-19498759e0.md | **failed** (empty body; HTML also empty — covered by GetFinancialRatios type=Quarter) | — |
| /getfinratiosnapshot-19800091e0.md | **failed** (empty body; HTML also empty — covered by GetFinancialRatios type=Realtime) | — |
| /getsectoralclassification-15575605e0.md | ok | 28b |
| /getsectors-21797542e0.md | ok | 28b |
| /getmei-21801200e0.md | ok | 28b |
| /getindustries-21955709e0.md | ok | 28b |
| /getbasicindustries-21955934e0.md | ok | 28b |
| /getshpitems-15575577e0.md | ok | 28c |
| /getshp-15575578e0.md | ok | 28c |
| /getshpadvanced-23196898e0.md | ok | 28c |
| /getscripmcap-15575589e0.md | ok | 28c |
| /getexchangemcap-17991613e0.md | ok | 28c |
| /getannualreports-31420321e0.md | ok | 28c |
| /getannualreports-15575602e0.md | **failed** (empty body — older URL superseded by /getannualreports-31420321e0) | — |
| /getcompanydata-15575603e0.md | ok | 28d |
| /getbulkdeals-15575586e0.md | ok | 28d |
| /getblockdeals-15575587e0.md | ok | 28d |
| /getdeliveryvolumes-15575588e0.md | ok | 28d |
| /getfiidii-37773825e0.md | ok | 28d |

## docs.globaldatafeeds.in — Other Data APIs (files 28e–28g)

| URL (.md form) | Status | Archived in |
|---|---|---|
| /index-constituents-933202m0.md | ok | 28e |
| /geteodstats-16005369e0.md | ok (spec marks endpoint deprecated) | 28e |
| /getbhavcopycm-15575591e0.md | ok | 28e |
| /getbhavcopyfo-15575592e0.md | ok | 28e |
| /getfuturesandoptions-15575593e0.md | ok | 28e |
| /getindexdetail-15575594e0.md | ok | 28e |
| /getcircuitfilterdetails-15575595e0.md | ok | 28e |
| /getstatisticsgroupabcompanies-15575596e0.md | ok | 28f |
| /gettop5gainerslosers-15575597e0.md | ok | 28f |
| /getindexhighlights-15575598e0.md | ok | 28f |
| /gettotaltradehighlights-15575599e0.md | ok | 28g |
| /getscripgrouptradehighlights-15575600e0.md | ok | 28g |
| /getturnoverdetailsoftop15scripsofagroup-15575601e0.md | ok | 28g |

## docs.globaldatafeeds.in — EOD Statistics + Helper APIs (file 28h)

| URL (.md form) | Status | Archived in |
|---|---|---|
| /getserieschange-16934245e0.md | ok | 28h |
| /getbannedsecurities-16945669e0.md | ok | 28h |
| /-getdeliverable-16953847e0.md | ok | 28h |
| /getvolatality-17039702e0.md | ok | 28h |
| /getstatsmcap-17991704e0.md | ok | 28h |
| /getnewhl-18721671e0.md | ok | 28h |
| /getcircuitbreakers-19111099e0.md | ok | 28h |
| /getserverinfo-15575607e0.md | ok | 28h |
| /getlimitation-15575606e0.md | ok | 28h |
| /getinstruments-17992355e0.md | ok | 28h |

## docs.globaldatafeeds.in — Reference (file 28i)

| URL (.md form) | Status | Archived in |
|---|---|---|
| /glossary-923501m0.md | ok (190-row response-parameter glossary) | 28i |
| /release-notes-926014m0.md | ok | 28i |
| /faqs-2131262m0.md | ok | 28i |

## docs.globaldatafeeds.in — Schemas (files 28x1–28x3)

All 61 schema pages fetched **ok**. Each is an OpenAPI 3.0.1 fragment (`paths: {}`, schema under `components.schemas`).

| URL (.md form) | Archived in |
|---|---|
| /annualreportitem-5999210d0.md | 28x1 |
| /annualreports-5999211d0.md | 28x1 |
| /bulkdealitem-5999212d0.md | 28x1 |
| /bulkdeals-5999213d0.md | 28x1 |
| /bulkdealsrange-5999214d0.md | 28x1 |
| /cgfactsresponse-5999215d0.md | 28x1 |
| /companydata-5999216d0.md | 28x1 |
| /companydataitem-5999217d0.md | 28x1 |
| /contactdetails-5999218d0.md | 28x1 |
| /contactdetailsitem-5999219d0.md | 28x1 |
| /corporateactions-5999220d0.md | 28x1 |
| /corporateactionsitem-5999221d0.md | 28x1 |
| /corporateannouncement-5999222d0.md | 28x1 |
| /corporateannouncementitem-5999223d0.md | 28x1 |
| /datesymbolqueryitem-5999224d0.md | 28x1 |
| /datesymbolqueryresponse-5999225d0.md | 28x1 |
| /datetimeformat-5999226d0.md | 28x1 |
| /deliveryvolumes-5999227d0.md | 28x1 |
| /deliveryvolumesitem-5999228d0.md | 28x1 |
| /deliveryvolumesrange-5999229d0.md | 28x1 |
| /finresultperiod-5999230d0.md | 28x2 |
| /finresultsfact-5999231d0.md | 28x2 |
| /finresultsqueryitem-5999232d0.md | 28x2 |
| /finresultsqueryresponse-5999233d0.md | 28x2 |
| /finresultsresponse-5999234d0.md | 28x2 |
| /finresultstype-5999235d0.md | 28x2 |
| /gainerslosersrequest-5999236d0.md | 28x2 |
| /genericxbrlfact-5999237d0.md | 28x2 |
| /marketcaphistory-5999238d0.md | 28x2 |
| /marketcapitem-5999239d0.md | 28x2 |
| /natureoffinreport-5999240d0.md | 28x2 |
| /qebhavcopycm-5999241d0.md | 28x2 |
| /qebhavcopycmresult-5999242d0.md | 28x2 |
| /qebhavcopyfo-5999243d0.md | 28x2 |
| /qebhavcopyforesult-5999244d0.md | 28x2 |
| /qecircuitfilterdetails-5999245d0.md | 28x2 |
| /qecircuitfilterdetailsresult-5999246d0.md | 28x2 |
| /qeequityderivativesms-5999247d0.md | 28x3 |
| /qeequityderivativesmsresult-5999248d0.md | 28x3 |
| /qeindexdetail-5999249d0.md | 28x3 |
| /qeindexdetailresult-5999250d0.md | 28x3 |
| /qeindexhighlight-5999251d0.md | 28x3 |
| /qeindexhighlightresult-5999252d0.md | 28x3 |
| /qescripgrouptradehighlight-5999253d0.md | 28x3 |
| /qescripgrouptradehighlightresult-5999254d0.md | 28x3 |
| /qestatisticsgroupabcompanies-5999255d0.md | 28x3 |
| /qestatisticsgroupabcompaniesresult-5999256d0.md | 28x3 |
| /qetop15turnoverdetails-5999257d0.md | 28x3 |
| /qetop15turnoverdetailsresult-5999258d0.md | 28x3 |
| /qetopgainerslosers-5999259d0.md | 28x3 |
| /qetopgainerslosersresult-5999260d0.md | 28x3 |
| /qetotaltradehighlight-5999261d0.md | 28x3 |
| /qetotaltradehighlightresult-5999262d0.md | 28x3 |
| /responseformat-5999263d0.md | 28x3 |
| /resultcalendar-5999264d0.md | 28x3 |
| /resultcalendaritem-5999265d0.md | 28x3 |
| /resultcalendarrange-5999266d0.md | 28x3 |
| /sectoralclassification-5999267d0.md | 28x3 |
| /sectoralclassificationitem-5999268d0.md | 28x3 |
| /shpfactsresponse-5999269d0.md | 28x3 |
| /votingfactsresponse-5999270d0.md | 28x3 |

## newsdocs.globaldatafeeds.in — remaining pages (file 29a)

| URL (.md form) | Status | Archived in |
|---|---|---|
| /-api-trial-1759051m0.md | ok | 29a |
| /list-of-apis-1759052m0.md | ok | 29a |
| /multi-company-news-31553453e0.md | ok | 29a |
| /general-news-31553455e0.md | ok | 29a |
| /glossary-1759055m0.md | ok | 29a |

## Notes

- With this run, every page listed in the site-structure indexes of `28-fundamental-data-api.md` and `29-news-api.md` is either archived verbatim or explicitly recorded above as failed/empty at source.
- APIs referenced in the "Type of Corporate Data Available" table without their own doc pages anywhere on the site (GetCorporateAnnouncementAttachment, GetVotingItems, GetVoting, GetCorporateGovernanceItems, GetCorporateGovernance, GetConsolidatedPledge) have no public endpoint pages to capture; likewise GetFinancialResultsAdvanced (mentioned only in Release Notes). Their response shapes are partly covered by the schema pages (CgFactsResponse, VotingFactsResponse, GenericXbrlFact).
- The three failed FinRatio pages and the older GetAnnualReports URL return HTTP 200 with an empty body from the Apidog host; they are still listed in the older nav snapshot in file 28 but are no longer published.
