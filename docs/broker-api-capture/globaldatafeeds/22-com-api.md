# GlobalDataFeeds APIs — COM API

COM API documentation: how to connect (endpoint, port, API key, flow of operations, important notes) and where to download the COM API DLLs & documentation.

Source URLs captured in this file:

- https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/com-api-documentation/how-to-connect-using-com-api/
- https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/com-api-documentation/download/

---

## How To Connect Using COM API

> Source: https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/com-api-documentation/how-to-connect-using-com-api/ — last updated 2024-07-10T12:27:01 — captured 2026-07-15

### Introduction

To connect using COM API, you will need following information :

– "endpoint" to connect to
– "port number" to connect to
– "API Key" received from our team to access data

The flow of operations will be as follows :

1. Make a connection to the endpoint:port
2. Send Authentication Request using API Key
3. Once authentication is successful, send all other data requests
4. If connection is lost for any reasons, follow same steps from 1 to 3 as above.

### Important Information

- – COM API allows only 1 active session with the server with 1 API Key. If you create another session with same key, previous session will be invalidated with response "*Access Denied. Key already in use by other session.*"

  – COM API Sends data as object with predefined structure.

  – Sometimes, API sends [diagnostic messages](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/documentation-support/diagnostic-api-responses/) – instead of actual data requested. For example, when API Key is expired / invalid, etc.. Your application should be able to handle these messages.

---

## Download (COM API)

> Source: https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/com-api-documentation/download/ — last updated 2024-07-10T12:26:51 — captured 2026-07-15

Content Moved.

To download documentation of COM API as well as to download COM API DLLs (x86 & x64), please [Click Here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/api-code-samples/download-code-samples-2/).
