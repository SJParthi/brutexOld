# GlobalDataFeeds — Fundamental Data API — Reference (Glossary, Release Notes, FAQs)

Gap-fill capture (2026-07-15) of docs.globaldatafeeds.in reference pages indexed in `28-fundamental-data-api.md` but not previously archived. Fetched verbatim via `<url>.md` (Apidog raw-markdown export). The Glossary and Release Notes pages embed raw HTML tables in their markdown source; preserved as served (HTML kept intact for fidelity). The FAQ page uses Apidog `<Accordion>` components; preserved as served.

Pages in this file:
1. Glossary — /glossary-923501m0
2. Release Notes — /release-notes-926014m0
3. FAQ's — /faqs-2131262m0

---

## Glossary

> Source: https://docs.globaldatafeeds.in/glossary-923501m0.md — captured 2026-07-15

<html>
<body>
<h2>Request Parameters</h2>
    <table>
        <thead>
            <tr>
                <th>Sr. No</th>
                <th>Parameter</th>
                <th>Description</th>
            </tr>
        </thead>
        <tbody>
            <tr><td>1</td><td>accessKey</td><td>Authentication key for API access.</td></tr>
            <tr><td>2</td><td>date</td><td>Specific date for data retrieval. Date in Epoch format eg: 1740493802, String eg:02-25-2025 21:00:02</td></tr>
            <tr><td>3</td><td>detailed</td><td>Detailed Information: True/ False</td></tr>
            <tr><td>4</td><td>dTFormat</td><td>Date and time format specification. Available values : Epoch eg: 1740493802, String eg:02-25-2025 21:00:02</td></tr>
            <tr><td>5</td><td>exchange</td><td>Market exchange identifier. Example: BSE</td></tr>
            <tr><td>6</td><td>format</td><td>Desired output format. Available values : Json, Xml, Csv, CsvContent</td></tr>
            <tr><td>7</td><td>from</td><td>Starting timestamp for called data. Expressed as Unix Time.</td></tr>
            <tr><td>8</td><td>fromYear</td><td>Starting year for data extraction. Example: 2024</td></tr>
            <tr><td>9</td><td>groups</td><td>Company groups. Example: A+B, A,B</td></tr>
            <tr><td>10</td><td>instrumentIdentifier</td><td>Example: RELIANCE</td></tr>
            <tr><td>11</td><td>instrumentIdentifiers</td><td>Example: RELIANCE+ABB</td></tr>
            <tr><td>12</td><td>natureOfReports</td><td>Type or classification of reports requested. Available values : Standalone, Consolidated</td></tr>
            <tr><td>13</td><td>periods</td><td>Time intervals for data. Available values : QM, M12, QJ, QS, M6, QD, M9</td></tr>
            <tr><td>14</td><td>Range</td><td>The time span for financial data result retrieval</td></tr>
            <tr><td>15</td><td>to</td><td>Ending timestamp for called data. Expressed as Unix Time.</td></tr>
            <tr><td>16</td><td>toYear</td><td>Ending year for data extraction. Example: 2025</td></tr>
            <tr><td>17</td><td>types</td><td>Classification or type of data.</td></tr>
            <tr><td>18</td><td>year</td><td>Specific year for data retrieval. Example: 2024</td></tr>
        </tbody>
    </table>
</body>
</html>
<!DOCTYPE html>
<html>
<head>
    <h2>Response Parameters</h2>
</head>
<body>
    <table>
        <tr>
            <th>Sr. No.</th>
            <th>Parameter</th>
            <th>Description</th>
        </tr>
        <tr><td>1</td><td>ActionId</td><td>Unique identifier for the corporate action.</td></tr>
        <tr><td>2</td><td>Advances</td><td>Stocks had a price increase compared to their previous closing price on given date.</td></tr>
        <tr><td>3</td><td>AnnouncementDate</td><td>Date of the corporate announcement.</td></tr>
        <tr><td>4</td><td>AnnouncementUrl</td><td>URL for the official announcement.</td></tr>
        <tr><td>5</td><td>AnnounceType</td><td>Type of corporate announcement (e.g., dividend, bonus issue).</td></tr>
        <tr><td>6</td><td>AttachmentName</td><td>Name of the file attached to the announcement.</td></tr>
        <tr><td>7</td><td>AttachmentUrl</td><td>URL to download the attachment.</td></tr>
        <tr><td>8</td><td>BaseChange</td><td>This represents the change in price compared to the base price.</td></tr>
        <tr><td>9</td><td>BasePrice</td><td>Reference price used for calculating price variations, margins, and other trading parameters for the contract.</td></tr>
        <tr><td>10</td><td>BcRdFlag</td><td>Indicates whether the record date or book closure is applicable.</td></tr>
        <tr><td>11</td><td>BcRdFrom</td><td>Start date for book closure or record date.</td></tr>
        <tr><td>12</td><td>BcRdTo</td><td>End date for book closure or record date.</td></tr>
        <tr><td>13</td><td>BizDt</td><td>Business date related to the corporate action.</td></tr>
        <tr><td>14</td><td>Change</td><td>Change in price.</td></tr>
        <tr><td>15</td><td>ChangePct</td><td>Percentage change in price.</td></tr>
        <tr><td>16</td><td>CktFlag</td><td>This means the stock has hit the Lower Circuit Limit on the given date.</td></tr>
        <tr><td>17</td><td>ClientName</td><td>Name of the client associated with the data.</td></tr>
        <tr><td>18</td><td>Close</td><td>The last traded price of the contract at market close on the given date.</td></tr>
        <tr><td>19</td><td>ClosePrice</td><td>Final traded price of the contract at the market close on the given date.</td></tr>
        <tr><td>20</td><td>Closing</td><td>Last traded price of a security or contract at the end of the official trading session for the day.</td></tr>
        <tr><td>21</td><td>CompanyName</td><td>Name of the company issuing the corporate action.</td></tr>
        <tr><td>22</td><td>ContextName</td><td>Context or category of the corporate action.</td></tr>
        <tr><td>23</td><td>ContractsNo</td><td>Total number of derivative contracts traded for the specified instrument on the given date.</td></tr>
        <tr><td>24</td><td>CorporateActionType</td><td>Specific type of corporate action (e.g., merger, rights issue).</td></tr>
        <tr><td>25</td><td>CriticalNews</td><td>Indicates if the news is critical for stakeholders.</td></tr>
        <tr><td>26</td><td>CumTrdVal</td><td>Total value of traded contracts (Volume × Price).</td></tr>
        <tr><td>27</td><td>CumTrdVol</td><td>Total contracts traded so far in the session.</td></tr>
        <tr><td>28</td><td>CurrentClosing</td><td>Latest closing price of the stock on the given trading day.</td></tr>
        <tr><td>29</td><td>Date</td><td>Date.</td></tr>
        <tr><td>30</td><td>DealType</td><td>Type of financial deal (e.g., M&A, stake sale).</td></tr>
        <tr><td>31</td><td>Decimals</td><td>Number of decimal places used in price or quantity values.</td></tr>
        <tr><td>32</td><td>Declines</td><td>Number of stocks within Group A and B companies that have experienced a decrease in their stock price.</td></tr>
        <tr><td>33</td><td>DeliveryQty</td><td>Quantity of shares delivered in settlement.</td></tr>
        <tr><td>34</td><td>DeliveryQtyPct</td><td>Percentage of total shares delivered.</td></tr>
        <tr><td>35</td><td>DeliveryValue</td><td>Total value of shares delivered.</td></tr>
        <tr><td>36</td><td>Description</td><td>Detailed description.</td></tr>
        <tr><td>37</td><td>Descriptor</td><td>Label or tag (e.g., Board Meeting Rescheduled).</td></tr>
        <tr><td>38</td><td>DescriptorId</td><td>Unique ID for the descriptor.</td></tr>
        <tr><td>39</td><td>Year</td><td>Calendar year for the data. Example: 2024.</td></tr>
        <tr><td>40</td><td>Expiry</td><td>Expiry date of an Scrip/ Instrument.</td></tr>
        <tr><td>41</td><td>ExpiryDate</td><td>Last trading date of a futures or options contract.</td></tr>
        <tr><td>42</td><td>FileStatus</td><td>Status of the received file (e.g., processed, pending).</td></tr>
        <tr><td>43</td><td>FillingDate</td><td>Date when the corporate filing was submitted.</td></tr>
        <tr><td>44</td><td>FinInstrmActlXpryDt</td><td>Actual expiry date of the financial instrument.</td></tr>
        <tr><td>45</td><td>FinInstrmId</td><td>Unique identifier for the financial instrument.</td></tr>
        <tr><td>46</td><td>FinInstrmNm</td><td>Name of the financial instrument.</td></tr>
        <tr><td>47</td><td>FinInstrmTp</td><td>Type of financial instrument (e.g., FUTSTK).</td></tr>
        <tr><td>48</td><td>From</td><td>Start date of event.</td></tr>
        <tr><td>49</td><td>GrossDate</td><td>Date related to gross settlements.</td></tr>
        <tr><td>50</td><td>HeadLine</td><td>Headline of the event.</td></tr>
        <tr><td>51</td><td>High</td><td>High Price</td></tr>
        <tr><td>52</td><td>HighPrice</td><td>Highest price recorded for the Scrip/ Instrument.</td></tr>
        <tr><td>53</td><td>IndexCode</td><td>Unique identifier for a stock market index.</td></tr>
        <tr><td>54</td><td>IndexName</td><td>Name of the stock market index.</td></tr>
        <tr><td>55</td><td>IndustryName</td><td>Industry classification of the company.</td></tr>
        <tr><td>56</td><td>InstrumentIdentifier</td><td>Symbol Name of the financial instrument.</td></tr>
        <tr><td>57</td><td>InstrumentName</td><td>Name of the financial instrument.</td></tr>
        <tr><td>58</td><td>ISIN</td><td>International Securities Identification Number.</td></tr>
        <tr><td>59</td><td>IsLoser</td><td>Indicates whether the stock is a top loser for the day.</td></tr>
        <tr><td>60</td><td>IssuesTraded</td><td>Number of securities traded in the session.</td></tr>
        <tr><td>61</td><td>Key</td><td>A unique identifier or reference key for data entries.</td></tr>
        <tr><td>62</td><td>LastDay</td><td>The previous trading session's date.</td></tr>
        <tr><td>63</td><td>LastPrice</td><td>The latest traded price of the security.</td></tr>
        <tr><td>64</td><td>ListedStatus</td><td>Indicates whether the security is currently listed on the exchange.</td></tr>
        <tr><td>65</td><td>Low</td><td>The lowest price reached during the session.</td></tr>
        <tr><td>66</td><td>LowPrice</td><td>Another reference for the session's lowest price.</td></tr>
        <tr><td>67</td><td>LTP</td><td>The most recent traded price of the stock.</td></tr>
        <tr><td>68</td><td>Ltq</td><td>The quantity traded in the last transaction.</td></tr>
        <tr><td>69</td><td>MarketCap</td><td>Total market value of a company's outstanding shares.</td></tr>
        <tr><td>70</td><td>MeetingDate</td><td>The scheduled date for a corporate meeting.</td></tr>
        <tr><td>71</td><td>MeetingType</td><td>Specifies the type of meeting (AGM, EGM, etc.).</td></tr>
        <tr><td>72</td><td>ModifyDate</td><td>The date when the record was last updated.</td></tr>
        <tr><td>73</td><td>MonthAgo</td><td>Reference date one month before the current date.</td></tr>
        <tr><td>74</td><td>NatureOfReport</td><td>Describes the type of corporate report (e.g., financial, governance).</td></tr>
        <tr><td>75</td><td>NdEndDate</td><td>End date of a specific corporate event or period.</td></tr>
        <tr><td>76</td><td>NdStartDate</td><td>Start date of a specific corporate event or period.</td></tr>
        <tr><td>77</td><td>NewBrdLotQty</td><td>The revised board lot quantity for trading.</td></tr>
        <tr><td>78</td><td>NewsBody</td><td>Detailed content of a corporate news report.</td></tr>
        <tr><td>79</td><td>NewsSubject</td><td>Headline or title summarizing the corporate news.</td></tr>
        <tr><td>80</td><td>NoofsharesheldwithNBF<br>Cascollateralagainst<br>lending_NonPromotershares</td><td>Number of non-promoter shares held as collateral with NBFCs for lending purposes.</td></tr>
        <tr><td>81</td><td>NoofsharesheldwithNBF<br>Cascollateralagainst<br>lending_Promotershares</td><td>Number of promoter <br>shares held as collateral with <br>NBFCs for lending purposes.</td></tr>
        <tr><td>82</td><td>Noofsharespledgedin<br>thedepositorysystem_<br>DNoofsharespledged</td><td>Total number of shares pledged<br> in the depository system.</td></tr>
        <tr><td>83</td><td>Noofsharespledgedinthedepository<br>system_ETotalnoofdematshares</td><td>Total number of dematerialized <br>(demat) shares in the <br>depository system.</td></tr>
        <tr><td>84</td><td>Noofsharespledgedinthe<br>depositorysystem_FPerDE</td><td>Percentage of pledged shares relative to total demat shares.</td></tr>
        <tr><td>85</td><td>Noofsharespledgedinthe<br>depositorysystem_ValuesPromoter</td><td>Value of pledged shares belonging <br>to promoters in monetary terms.</td></tr>
        <tr><td>86</td><td>OI</td><td>Open Interest, the total number of outstanding derivative contracts.</td></tr>
        <tr><td>87</td><td>OiChng</td><td>Change in Open Interest compared to the previous value.</td></tr>
        <tr><td>88</td><td>OiQty</td><td>Quantity of open interest.</td></tr>
        <tr><td>89</td><td>Opening</td><td>The opening price of a stock or security.</td></tr>
        <tr><td>90</td><td>OpenPrice</td><td>The initial price at which a security is traded when the market opens.</td></tr>
        <tr><td>91</td><td>OptionType</td><td>Specifies the type of option contract (Call/Put).</td></tr>
        <tr><td>92</td><td>PaymentDate</td><td>The date when a payment is made or due.</td></tr>
        <tr><td>93</td><td>PercentageFluctation</td><td>Percentage change in price over a given period.</td></tr>
        <tr><td>94</td><td>PerChange</td><td>Percentage change in value compared to the previous period.</td></tr>
        <tr><td>95</td><td>PrevClose</td><td>The last recorded closing price of the security.</td></tr>
        <tr><td>96</td><td>PrevCloseChangePct</td><td>Percentage change from the previous close.</td></tr>
        <tr><td>97</td><td>PrevCloseChangePts</td><td>Points change from the previous close.</td></tr>
        <tr><td>98</td><td>PrevClosePrice</td><td>Closing price of the previous trading session.</td></tr>
        <tr><td>99</td><td>PreviousClosing</td><td>Another term for the last closing price.</td></tr>
        <tr><td>100</td><td>Price</td><td>The current price of the security.</td></tr>
        <tr><td>101</td><td>Product</td><td>Identifies the financial product or instrument.</td></tr>
        <tr><td>102</td><td>ProductId</td><td>Unique identifier for a specific product.</td></tr>
        <tr><td>103</td><td>ProductType</td><td>Specifies the category of the product.</td></tr>
        <tr><td>104</td><td>PromoterSharesEncumberedas<br>oflastquarter_NoofShares</td><td>Number of encumbered promoter shares as of the last quarter.</td></tr>
        <tr><td>105</td><td>PromoterSharesEncumberedas<br>oflastquarter_Perofpromotershares</td><td>Percentage of promoter shares that are encumbered.</td></tr>
        <tr><td>106</td><td>PromoterSharesEncumberedas<br>oflastquarter_Peroftotalshares</td><td>Percentage of total shares that are encumbered by promoters.</td></tr>
        <tr><td>107</td><td>PromoterSharesEncumberedas<br>oflastquarter_Values</td><td>Value of encumbered promoter shares.</td></tr>
        <tr><td>108</td><td>ProvXdate</td><td>Provisional expiry date for contracts.</td></tr>
        <tr><td>109</td><td>PurposeCode</td><td>Code representing the purpose of a transaction.</td></tr>
        <tr><td>110</td><td>Qty</td><td>Quantity of traded or held shares.</td></tr>
        <tr><td>111</td><td>QtyPct</td><td>Percentage representation of the traded or held quantity.</td></tr>
        <tr><td>112</td><td>Quantity</td><td>Total number of units involved in a trade.</td></tr>
        <tr><td>113</td><td>RatioAmount</td><td>The ratio value in a corporate action like bonus or rights issue.</td></tr>
        <tr><td>114</td><td>ReceivedAt</td><td>Timestamp indicating when data was received.</td></tr>
        <tr><td>115</td><td>ReferenceSrno</td><td>Serial number for reference purposes.</td></tr>
        <tr><td>116</td><td>ResultPeriod</td><td>The financial period for which results are declared.</td></tr>
        <tr><td>117</td><td>ResultsDate</td><td>Date on which financial results are announced.</td></tr>
        <tr><td>118</td><td>ResultsType</td><td>Type of financial result (e.g., quarterly, annual).</td></tr>
        <tr><td>119</td><td>SavedAt</td><td>Timestamp when the record was saved.</td></tr>
        <tr><td>120</td><td>ScripCode</td><td>Unique code identifying a stock/security.</td></tr>
        <tr><td>121</td><td>ScripFaceValue</td><td>The nominal value of a share as per company records.</td></tr>
        <tr><td>122</td><td>ScripGroup</td><td>Category or group classification of the scrip.</td></tr>
        <tr><td>123</td><td>ScripId</td><td>Unique identifier for a scrip.</td></tr>
        <tr><td>124</td><td>ScripName</td><td>Name of the stock/security.</td></tr>
        <tr><td>125</td><td>ScripsNo</td><td>Number of scrips in a transaction or record.</td></tr>
        <tr><td>126</td><td>SctySrs</td><td>Security series code.</td></tr>
        <tr><td>127</td><td>SeriesCode</td><td>Code for identifying the series of a security.</td></tr>
        <tr><td>128</td><td>SeriesId</td><td>Unique identifier for a series.</td></tr>
        <tr><td>129</td><td>SettPrice</td><td>Settlement price for the security.</td></tr>
        <tr><td>130</td><td>Sgmt</td><td>Segment of the market in which the security trades.</td></tr>
        <tr><td>131</td><td>SharesNo</td><td>Number of shares.</td></tr>
        <tr><td>132</td><td>SharesVol</td><td>Volume of shares traded.</td></tr>
        <tr><td>133</td><td>Src</td><td>Source of data or transaction.</td></tr>
        <tr><td>134</td><td>SsnId</td><td>Session identifier.</td></tr>
        <tr><td>135</td><td>StrikePrice</td><td>The agreed-upon price in an options contract.</td></tr>
        <tr><td>136</td><td>SttlmPric</td><td>Settlement price for derivatives.</td></tr>
        <tr><td>137</td><td>Symbol</td><td>Trading symbol for the security.</td></tr>
        <tr><td>138</td><td>To</td><td>Represents the upper value in a range or transaction.</td></tr>
        <tr><td>139</td><td>TotalIssuedShares</td><td>Total number of shares issued by a company.</td></tr>
        <tr><td>140</td><td>TotalPromoterHoldingPer</td><td>Percentage of shares held by promoters.</td></tr>
        <tr><td>141</td><td>TotalPromoterHoldingShares</td><td>Total number of shares held by promoters.</td></tr>
        <tr><td>142</td><td>TotalPublicHolding</td><td>Total number of shares held by the public.</td></tr>
        <tr><td>143</td><td>TotalTradesNo</td><td>Total number of trades executed.</td></tr>
        <tr><td>144</td><td>TotTurnoverPct</td><td>Percentage change in total turnover.</td></tr>
        <tr><td>145</td><td>TradDt</td><td>Trade date.</td></tr>
        <tr><td>146</td><td>TradeDate</td><td>Date on which a trade was executed.</td></tr>
        <tr><td>147</td><td>TradedQty</td><td>Quantity of shares traded.</td></tr>
        <tr><td>148</td><td>TradesNo</td><td>Number of trades executed.</td></tr>
        <tr><td>149</td><td>TtlTrfVal</td><td>Total transfer value of shares.</td></tr>
        <tr><td>150</td><td>TTQ</td><td>Total traded quantity.</td></tr>
        <tr><td>151</td><td>Turnover</td><td>Total transaction value in monetary terms.</td></tr>
        <tr><td>152</td><td>TurnoverRs</td><td>Total turnover in Indian Rupees.</td></tr>
        <tr><td>153</td><td>TurnoverRsCr</td><td>Total turnover in crores.</td></tr>
        <tr><td>154</td><td>Unchanged</td><td>Number of securities with no price change.</td></tr>
        <tr><td>155</td><td>UnderlyingPrice</td><td>The price of the underlying asset in a derivative.</td></tr>
        <tr><td>156</td><td>UnitId</td><td>Unique identifier for a trading unit.</td></tr>
        <tr><td>157</td><td>Value</td><td>Total value of traded securities.</td></tr>
        <tr><td>158</td><td>ValueRsCr</td><td>Total value of securities in crores.</td></tr>
        <tr><td>159</td><td>VolShares</td><td>Volume of shares traded.</td></tr>
        <tr><td>160</td><td>WeekAgo</td><td>Data value recorded a week ago.</td></tr>
        <tr><td>161</td><td>YearAgo</td><td>Data value recorded a year ago.</td></tr>
        <tr><td>162</td><td>Date</td><td>Date of the record or transaction.</td></tr>
        <tr><td>163</td><td>DealDate</td><td>Date on which a deal or agreement was made.</td></tr>
        <tr><td>164</td><td>GrossDate</td><td>Date representing the gross transaction value.</td></tr>
        <tr><td>165</td><td>Identifier</td><td>Unique reference code or ID.</td></tr>
        <tr><td>166</td><td>IndexName</td><td>Name of the stock market index.</td></tr>
        <tr><td>167</td><td>Instrument</td><td>The financial instrument involved in trading. Example: RELIANCE.</td></tr>
        <tr><td>168</td><td>InstrumentName</td><td>Name of the financial instrument. Example: RELIANCE.</td></tr>
        <tr><td>169</td><td>Label</td><td>Descriptive label for a security or record.</td></tr>
        <tr><td>170</td><td>ScripGroupCode</td><td>Code representing the scrip’s group category.</td></tr>
        <tr><td>171</td><td>TradeHighlights</td><td>Key highlights or observations from trading activity.</td></tr>
<tr><td>172</td><td>Disclosuresmadebypromoters<br>ontheirencumberedsharesunderR<br>egulation31ofSASTsincethe<br>lastquarter</td>
            <td>Disclosures made by company promoters about their pledged shares under Regulation 31 of SEBI's SAST regulations since the previous quarter.</td></tr>
<tr><td>173</td><td>AllotedFrom</td><td>Source or category from which the sectoral data is assigned.</td></tr>
  <tr><td>174</td><td>MeiCode</td><td>Code representing the Macro Economic Indicator linked to the company.</td></tr>
  <tr><td>175</td><td>MacroEconomicIndicator</td><td>A broader economic category under which the company falls.</td></tr>
  <tr><td>176</td><td>SectCode</td><td>Unique identifier for the sector.</td></tr>
  <tr><td>177</td><td>Sector</td><td>The primary business sector of the company.</td></tr>
  <tr><td>178</td><td>IndustryCode</td><td>Unique identifier for the industry.</td></tr>
  <tr><td>179</td><td>Industry</td><td>The industry classification within the sector.</td></tr>
  <tr><td>180</td><td>BasicIndustryCode</td><td>Unique code for the basic industry group.</td></tr>
  <tr><td>181</td><td>BasicIndustry</td><td>The most granular industry classification for the company.</td></tr>
  <tr><td>182</td><td>Address</td><td>Official registered address of the company.</td></tr>
  <tr><td>183</td><td>ContactDetails</td><td>Phone or other contact numbers of the company.</td></tr>
  <tr><td>184</td><td>Email</td><td>Official email address of the company.</td></tr>
  <tr><td>185</td><td>CompanySecretaryName</td><td>Name of the company secretary responsible for compliance.</td></tr>
  <tr><td>186</td><td>Designation</td><td>Official designation of the company secretary.</td></tr>
  <tr><td>187</td><td>RtaName</td><td>Name of the Registrar and Transfer Agent handling share registry services.</td></tr>
  <tr><td>188</td><td>RtaEmail</td><td>Official email address of the Registrar and Transfer Agent.</td></tr>
  <tr><td>189</td><td>RtaContact</td><td>Contact number of the Registrar and Transfer Agent.</td></tr>
  <tr><td>190</td><td>Rn</td><td>Internal or reference number associated with the data record.</td></tr>

    </table>
</body>
</html>

---

## Release Notes

> Source: https://docs.globaldatafeeds.in/release-notes-926014m0.md — captured 2026-07-15
> (Source page title: "📢Release Notes". Table reproduced 1:1 from the embedded HTML; dates are dd-mm-yy.)

| Date | Description |
|---|---|
| 15-12-25 | 30 Custom Financial Ratios are now available in the Financial Results module |
| 10-11-25 | 10 Custom Financial Ratios are now available in the Financial Results module |
| 23-10-25 | NSE -Added Sectoral Classification |
| 03-10-25 | 30 Custom Financial Ratios are now available in the Financial Results module |
| 03-10-25 | GetFinancialResultsAdvanced functionality has been added to the Financial Results module |
| 26-09-25 | The Share Holding Pattern module has been enhanced with the addition of GetSHPAdvanced |
| 15-09-25 | Added GetSectors, GetMei, GetIndustries, and GetBasicIndustries functionalities to the Sector Classification module |
| 20-07-25 | 52 Custom Financial Ratios are now available in the Financial Results module |
| 13-06-25 | NewHL function - Companies which have hit new High / Low band |
| 09-06-25 | Corporate Announcement attachment |
| 02-06-25 | Exchange Market Capitalisation and Sripwise Market Capitalisation |
| 23-05-25 | Deliverables and Volatility Data |
| 15-05-25 | Related Party Transactions added in Financial results |
| 08-05-25 | BSE - Added Company Data |
| 08-05-25 | BSE - Added Sectoral Classification |
| 25-04-25 | BSE - Added Corporate Action Categories |
| 20-04-25 | Top Gainers Losers of Index Stocks |
| 17-04-25 | BSE - Added GetCorporateAnnouncementsCategories function |
| 17-04-25 | NSE - GetPERatio (Price Per Earning Ratio) |
| 17-04-25 | NSE - GetSeriesChange |
| 07-04-25 | NSE - added GetEODStats (contains following - OHLCV, Previous Close, no. of trades, deliverable quantity, deliverable quantity percentage, Series Change notification, band hit notification, Volatility, total no. of shares, marketcap ) |
| 03-04-25 | from/to in all functions now honour hour/min part (earlier filtering used to work on Date basis only) |
| 02-04-25 | NSE - added index constituents offering |
| 02-04-25 | BSE - added index constituents offering |
| 02-04-25 | NSE - Added BulkDeals & BlockDeals |
| 21-03-25 | BSE - Added BlockDeals |
| 17-03-25 | Soft launch - started disseminating following from BSE : Corporate Announcements, Corporate Actions, Corporate Governance, Financial Results Calendar, Financial Results, Share Holding Pattern, Voting Results, Consolidated Pledge of Promotors, Bulk Deals, Delivery Volumes, Market Cap, Companies hitting upper/lower circuit - and more |

---

## FAQ's

> Source: https://docs.globaldatafeeds.in/faqs-2131262m0.md — captured 2026-07-15
> (Source uses Apidog `<AccordionGroup>`/`<Accordion>` components; preserved verbatim.)

<!-- GENERAL OVERVIEW -->

## General Overview

<AccordionGroup>

  <Accordion title="What is Global Datafeeds?">
  Global Datafeeds provides APIs for Indian stock market fundamental and corporate data.
  </Accordion>

  <Accordion title="What is the Fundamental Data API?">
  The Fundamental Data API provides realtime and historical corporate information for companies listed on Indian stock exchanges. It includes data such as corporate announcements, financial results, shareholding patterns, market capitalization, annual reports, and more.
  </Accordion>

  <Accordion title="Who should use these APIs?">
  Developers, analysts, traders, fintech platforms, and research teams.
  </Accordion>

</AccordionGroup>



<!-- AUTHENTICATION -->

## Authentication

<AccordionGroup>

  <Accordion title="How do I authenticate API requests?">
  Include your AccessKey as a query parameter in every API request. Requests without a valid AccessKey will be rejected.
  </Accordion>

  <Accordion title="How can I regenerate or reset my AccessKey?">
  Please contact the Global Datafeeds support team to regenerate or reset your AccessKey.
  </Accordion>

  <Accordion title="What is the frequency of data updates?">
  The data update frequency varies by API endpoint. Please refer to the [List of APIs](https://docs.globaldatafeeds.in/list-of-apis-923685m0) section for endpoint-specific update intervals.
  </Accordion>

  <Accordion title="What response formats are supported?">
  JSON, XML, CSV, and CSVContent formats are supported.
  </Accordion>

</AccordionGroup>



<!-- CORPORATE & FUNDAMENTAL APIs -->

## Corporate & Fundamental APIs

<AccordionGroup>

  <Accordion title="What corporate data is available?">
  Corporate Announcements, corporate actions, financial ratios, shareholding patterns, annual reports, financial results, and more.
  </Accordion>

  <Accordion title="How many days of historical backfill data are available?">
  Historical backfill data is available for the last 30 days. Please refer to the [Corporate Data Availability](https://docs.globaldatafeeds.in/type-of-corporate-data-available-1142925m0) page for detailed information.
  </Accordion>

  <Accordion title="What is GetCorporateAnnouncements API?">
  Returns company disclosures such as earnings, mergers, dividends, and management updates.
  </Accordion>

  <Accordion title="What is GetCorporateActions API?">
  Provides dividends, splits, buybacks, and other shareholder-impacting events.
  </Accordion>

</AccordionGroup>



<!-- DEVELOPMENT & USAGE -->

## Development & Usage

<AccordionGroup>

  <Accordion title="Are the APIs RESTful?">
  Yes, all APIs follow REST architecture and use standard HTTP methods.
  </Accordion>

  <Accordion title="Which HTTP methods are supported?">
  The GET method is primarily used for retrieving data.
  </Accordion>

  <Accordion title="Is trial access available?">
  Yes, you can test APIs using the “Try It” feature in the documentation.
  </Accordion>

  <Accordion title="Are code samples available?">
  Yes, sample code is available in multiple programming languages. Please refer to the [Code Sample Description](https://docs.globaldatafeeds.in/code-samples-api-trial-923683m0) section for detailed examples.
  </Accordion>

  <Accordion title="How should I handle API issues?">
  APIs return diagnostic message responses when parameters are invalid or missing. Please review the diagnostic message, correct the request parameters, and retry. For more details, please refer to the [Diagnostic message](https://docs.globaldatafeeds.in/diagnostic-api-responses-925705m0) page.
  </Accordion>

  <Accordion title="What is the request per hour limit?">
  The request limit is 900 requests per hour per API key. This means a maximum of 900 API requests can be made within a one-hour window. If the limit is reached before the hour ends, additional requests can be made once the next hour begins and the count automatically resets.
  </Accordion>

  <Accordion title="What happens if I exceed my request per hour limit?">
  If the request per hour limit is reached, you must wait until the limit resets at the start of the next hour. This ensures fair usage and system stability.
  </Accordion>

</AccordionGroup>



<!-- FINANCIAL RESULTS -->

## Financial Results

<AccordionGroup>

  <Accordion title="What is the difference between Standalone type Results and Consolidated type Results?">
  
  **Standalone Financial Results**  
  These reflect only the financial performance of the parent company itself, excluding any subsidiaries or associate entities.

  **Consolidated Financial Results**  
  These present the combined financial performance of the parent company along with all its subsidiaries and associates, reported as a single economic entity.

  </Accordion>

  <Accordion title="What are the approximate counts for consolidated and standalone filings?">
  
  In general:

  - Standalone filings are higher in number, as many companies either do not have subsidiaries or still file standalone statements separately.
  - Consolidated filings are lower, as only companies with subsidiaries report them.

  </Accordion>

  <Accordion title="Why do some companies publish only standalone financial statements?">
  
  Companies may publish only standalone statements when:

  - They do not have subsidiaries, associates, or joint ventures.
  - They are not required by law or regulation to publish consolidated statements.
  - Subsidiaries are considered immaterial in limited cases.
  - The disclosure is intended for interim, regulatory, or internal reporting purposes.

  In such cases, standalone results may effectively represent the complete financial position of the company.

  </Accordion>

  <Accordion title="How is the Financial Results period nomenclature defined?">
  
  Financial results data is stored based on the Financial Year Start (Period Start Year), not the calendar year.

  The folder period nomenclature follows this format:

  `<4-digit year><reporting period>`

  **Reporting Period Codes**

  - QJ → Quarter ending June (30-06)
  - QS → Quarter ending September (30-09)
  - QD → Quarter ending December (31-12)
  - QM → Quarter ending March (31-03)
  - M6 → Half-Yearly (6 months)
  - M9 → 9 Months
  - M12 → 12 months

  **Example: FY 2024-25**

  - 2024QJ → Quarter ending June 2024
  - 2024QS → Quarter ending September 2024
  - 2024QD → Quarter ending December 2024
  - 2024QM → Quarter ending March 2025
  - 2024M6 → 6 Months period
  - 2024M9 → 9 Months period
  - 2024M12 → 12 Months period

  </Accordion>

</AccordionGroup>



<!-- MISCELLANEOUS -->

## Miscellaneous

<AccordionGroup>

  <Accordion title="Does this include real-time price data?">
  No, this documentation focuses on fundamental and corporate data APIs.
  </Accordion>

  <Accordion title="How do I get pricing information?">
  Please contact the Global Datafeeds sales team for pricing details.

  Visit: https://globaldatafeeds.in/contact-us/
  </Accordion>

  <Accordion title="Who can I contact for support?">
  Please contact the Global Datafeeds support team for assistance.

  Visit: https://globaldatafeeds.in/contact-us/
  </Accordion>

</AccordionGroup>
