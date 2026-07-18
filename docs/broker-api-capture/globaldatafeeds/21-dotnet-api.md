# GlobalDataFeeds APIs — DotNet API

DotNet API documentation: how to connect (endpoint, port, API key, flow of operations, important notes) and where to download the DotNet API DLLs & documentation.

Source URLs captured in this file:

- https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/dotnet-api-documentation/how-to-connect-using-dotnet-api/
- https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/dotnet-api-documentation/download-2/

---

## How To Connect Using DotNet API

> Source: https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/dotnet-api-documentation/how-to-connect-using-dotnet-api/ — last updated 2024-07-10T12:27:11 — captured 2026-07-15

### Introduction

To connect using DotNet API, you will need following information :

– "endpoint" to connect to
– "port number" to connect to
– "API Key" received from our team to access data

The flow of operations will be as follows :

1. Make a connection to the endpoint:port
2. Send Authentication Request using API Key
3. Once authentication is successful, send all other data requests
4. If connection is lost for any reasons, follow same steps from 1 to 3 as above.

### Important Information

- – DotNet API allows only 1 active session with the server with 1 API Key. If you create another session with same key, previous session will be invalidated with response "*Access Denied. Key already in use by other session.*"

  – Dotnet API Sends data as object with predefined structure. By using Json.Net library, it is possible to convert this response in JSON format using just one function call.

  – Sometimes, API sends [diagnostic messages](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/documentation-support/diagnostic-api-responses/) – instead of actual data requested. For example, when API Key is expired / invalid, etc.. Your application should be able to handle these messages.

---

## Download (DotNet API)

> Source: https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/dotnet-api-documentation/download-2/ — last updated 2024-07-10T12:27:08 — captured 2026-07-15

Content Moved.

To download documentation of DotNet API as well as to download DotNet API DLLs (x86 & x64), please [Click Here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/api-code-samples/download-code-samples-2/).
