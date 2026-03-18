---
name: meta-ads-analyzer
description: Provides expert-level analysis and diagnosis for Meta Ads campaigns. Use this skill to interpret performance data, identify root causes of issues, and generate actionable recommendations, with a special focus on correctly handling the 'Breakdown Effect'.
---

# Meta Ads analysis & diagnosis skill

> [!DANGER]
> **CRITICAL: FAILURE TO FOLLOW THE RULES IN THIS DOCUMENT WILL RESULT IN TASK FAILURE.**

> [!IMPORTANT]
> **SCOPE: These rules in the skill apply to EVERY tool calling, especially for tools like `file`, `message` and parameter like `breif`. Compliance is required at all times, not just in the final deliverable.**

## 1. The five non-negotiable rules (MANDATORY)

These are the most common failure points. They are not guidelines; they are absolute requirements.

### 1.1. Audience terminology: use "Accounts Center accounts", never "people" or "users"

- **Reason**: This is a strict legal and policy requirement from Meta.
- **Rule**: When referring to users, audiences, or reach, you **MUST** use the exact phrase `"Accounts Center accounts"`. The words "people" and "person" are forbidden in these contexts.
- **Examples**:
    - **Correct**: "The campaign reached 10,000 Accounts Center accounts."
    - **INCORRECT**: "The campaign reached 10,000 people."
    - **Correct**: "Reach measures the number of Accounts Center accounts that saw your ad."
    - **INCORRECT**: "Reach measures the number of unique users who saw your ad."
> **Common failure point**: When defining `Reach`, you **MUST** use the exact wording from Section 2: *"The number of Accounts Center accounts that saw your ads at least once."* Do NOT paraphrase as "unique users", "people", or "viewers".

### 1.2. Clicks metrics: use "Clicks (all)" or "Link clicks", never "clicks"

- **Reason**: "Clicks" is ambiguous and leads to incorrect analysis. The API returns separate values for different click types, and conflating them causes misleading reports.
- **Rule**: You **MUST** always use the specific metric name — either `"Clicks (all)"` or `"Link clicks"`. **NEVER** use the word "clicks" alone, especially when presenting numerical values from the API. See Section 2 for detailed definitions.
- **Examples**:
    - **Correct**: "The ad received 1,500 Clicks (all) and 800 Link clicks."
    - **INCORRECT**: "The ad received 1,500 clicks."

### 1.3. Metric naming: use exact names, no prefixes or ambiguous terms

- **Reason**: Consistency and clarity are critical for accurate reporting.
- **Rule**: You **MUST** use the exact `Standardized display name` from the glossary in Section 2.
- **Forbidden prefixes**: **NEVER** add `Total`, `Overall`, or `Average` to standard metric names (e.g.,~~ `Total Impressions`,`Total clicks(all)`~~). If you need to express an aggregate sum, rephrase the sentence instead , but **NEVER** place any prefix directly before the metric name. This rule applies to ALL metrics, not just those listed in Section 2.
- **Forbidden ambiguous terms**: **NEVER** use vague terms like `Video views` or `Video View Rate`. Use the specific metric from the glossary, such as `3-second video plays`.

### 1.4. Data integrity: currency and dates

- **Currency**: The monetary values returned by the API are already converted into the standard currency unit (e.g., USD, EUR). When presenting any money-related metrics (such as Amount spent, CPC (all), CPM (cost per 1,000 impressions), Cost per result, etc.), use the returned numerical values directly together with the `currency` field from the ad account context.  For example, if the API returns `spend: 150.50` and the account currency is `USD`, report it as `$150.50`.
- **Partial dates**: If a query's date range includes today, you MUST state that the data is partial and subject to change.

### 1.5. Data scope: account vs. asset level accuracy

- **Reason**: Confusing account-level data with campaign-level (or ad set/ad level) data is a common and critical error. It renders any analysis of specific assets meaningless and misleading.
- **Rule**:  **MUST** ensure the data scope strictly matches the user's query. If a user asks about a specific campaign, you must return data *only* for that campaign, not the entire account. Before presenting data, double-check that the correct entity ID and reporting level were used in the data retrieval process.

### 1.6. Cross-objective aggregation: display "N/A" for mixed result types

- **Reason**: "Cost per result" cannot be aggregated across campaigns with different objectives (e.g., Sales vs. Lead). Ads Manager returns `null` for this.
- **Rule**: When aggregating across mixed objectives, display `"N/A"` for "Cost per result" and "Results" — do NOT compute these metrics.

### 1.7. Null data handling & data authenticity: never fabricate data

- **Reason**: Ads Manager returns `null` for metrics that cannot be computed or are unavailable. Additionally, presenting fabricated numbers destroys trust and leads to incorrect business decisions.
- **Rule**:
    - When a metric returns `null`, display `"Data not available"` or `"N/A"` — do NOT report the raw null value.
    - **MUST** only report numerical values that are explicitly returned by the API. **NEVER** fabricate, estimate, or invent any data values. If a metric is not available in the API response, do NOT guess or make up a number — always indicate that the data is unavailable.

## 2. Standardized metric glossary & definitions

This section is the single source of truth for all metrics. When referring to any metric, you MUST use the exact standardized display name from the table below—do NOT invent alternative names, abbreviate, or reword them (e.g., "3-second video plays", NOT "video views" or "3-second video play"). When mentioning these metrics' definitions, you MUST use the exact definitions below verbatim. Do NOT paraphrase, expand, or add any interpretation of your own. Pay attention to capitalization—use sentence case (e.g., "Messaging conversations started", NOT "Messaging Conversations Started").

| Raw metric name | Standardized display name | Definition |
| :--- | :--- | :--- |
| `impressions` | Impressions | The number of times your ads were on screen. |
| `reach` | Reach | The number of Accounts Center accounts that saw your ads at least once. Reach is different from impressions, which may include multiple views of your ads by the same Accounts Center accounts. |
| `clicks` | Clicks (all) | The number of clicks, taps or swipes on your ads. |
| `inline_link_clicks` | Link clicks | The number of clicks on links within the ad that led to advertiser-specified destinations, on or off Meta technologies. |
| `video_thruplay_watched_actions` | ThruPlays | The number of times your video was played to completion, or for at least 15 seconds. |
| `video_views` | 3-second video plays | The number of times your video played for at least 3 seconds, or for nearly its total length if it's shorter than 3 seconds. For each impression of a video, video plays are counted separately and exclude any time spent replaying the video. |
| `spend` | Amount spent | The approximate total amount of money you've spent on your campaign, ad set or ad during its schedule. |
| `purchase_roas` | Purchase ROAS (return on ad spend) | The total return on ad spend (ROAS) from purchases. This is based on approximate Shop sales that occurred on Meta technologies, such as Shops, Marketplace, Pages or Messenger as well as information received from one or more of your connected Meta Business Tools and attributed to your ads. |
| `cpm` | CPM (cost per 1,000 impressions) | The average cost for 1,000 impressions. |
| `cpc` | CPC (all) | The average cost for each click (all). |
| `ctr` | CTR (all) | The percentage of impressions where a click (all) occurred out of the total number of impressions. |
| `cost_per_result` | Cost per result | The average cost per result from your ads. |
| `inline_link_click_ctr` | CTR (link click-through rate) | The percentage of times Accounts Center accounts saw your ads and performed a link click. |
| `cost_per_inline_link_click` | CPC (cost per link click) | The average cost for each link click. |
| `actions:onsite_conversion.messaging_conversation_started_7d` | Messaging conversations started (MCS) | The number of times a messaging conversation was started with your business after at least 7 days of inactivity, attributed to your ads. |
| `cost_per_action_type` | Cost per 3-second video play | The average cost of each 3-second video play.|
| `unique_video_continuous_2_sec_watched_actions` | Unique 2-second continuous video plays | The number of Accounts Center accounts that performed a 2-second continuous video view. |
| `video_continuous_2_sec_watched_actions` | 2-second continuous video plays | The number of times your video was played for 2 continuous seconds or more. 2-second continuous video plays will have at least 50% of the video pixels in view. |
| `cost_per_2_sec_continuous_video_view` | Cost per 2-second continuous video play | The average cost for each 2-second continuous video play. |
| `video_30_sec_watched_actions` | 30-second video views | The number of times your video played for at least 30 seconds, or for nearly its total length if it's shorter than 30 seconds. For each impression of a video, we'll count video views separately and exclude any time spent replaying the video. |
| `quality_ranking` | Quality ranking | A ranking of your ad's perceived quality. Quality is measured using feedback on your ads and the post-click experience. Your ad is ranked against ads that competed for the same audience. |
| `conversion_rate_ranking` | Conversion rate ranking | A ranking of your ad's expected conversion rate. Your ad is ranked against ads with your optimization goal that competed for the same audience. |
| `engagement_rate_ranking` | Engagement rate ranking | A ranking of your ad's expected engagement rate. Engagement includes all clicks, likes, comments and shares. Your ad is ranked against ads that competed for the same audience. |


## 3. Core analysis principles & workflow

### 3.1. Core principles (how to think)

- All data presented must be rigorously verified for accuracy. This includes its scope (e.g., Account vs. Campaign), units (e.g., cents vs. dollars), and timeframes (e.g., partial vs. full day). Never present data without first confirming its integrity.
- Evaluate at the aggregate level before drilling down.
- Analyze performance over time, not single snapshots.
- The system prioritizes marginal cost (cost of the *next* result), not the average. A segment with a higher average CPA might have a lower marginal CPA.

### 3.2. Analysis workflow (how to act)

**Reference documents:** Start by reading `references/breakdown_effect.md`.

1.  Use Campaign Level for CBO, Ad Set Level otherwise to avoid the Breakdown Effect.
2.  Investigate marginal efficiency, ad relevance diagnostics, and learning phase status.
3.  Explain why the system makes its decisions based on marginal cost, not just average cost.

## 4. Final report generation rules

- NEVER recommend pausing or reducing budget based solely on higher average CPA/CPM in breakdown reports. Frame changes as testable hypotheses.
- ALWAYS justify recommendations with data, system mechanics.
- EVERY insight must include data evidence and an explanation.
- ALIGN WITH OFFICIAL RECOMMENDATIONS from the `get_recommendations` API, or explicitly state why you are diverging.
- All output MUST be in a single, consistent language.
- In narrative text, use sentence case for metrics (e.g., "Link clicks").

## 5. Meta Ads domain knowledge

### Campaign & performance definitions

- **Conversion ads:** Ad entities with objectives like Lead, Sales, or App Promotions are categorized as conversion ads.
- **Conversion rate:** Conversion rate = conversion / impression.
- **Performance indicators:** Lower value of Cost Per result or CPM is associated with higher performance. Higher value of ROAS is associated with higher performance.

### Account & asset issues

- Occurs when one or more of the customer's assets (e.g., FB account, IG account, ad account, page, payout account) have been disabled or restricted by Meta, usually because the customer violated some policies. This is not relevant for general questions about business manager setup, deleting a business manager, or converting a Facebook page to a business page.

### Budget & billing

- **Daily spending limit (DSL):** The current daily spending limit that advertisers can check, increase, or decrease.
- **Billing threshold (payment threshold):** The amount of ad spend that triggers a payment method charge when reached. Advertisers can check limit, lower, or increase their payment threshold. The billing threshold is also relevant to billing frequency (e.g., monthly billing, daily billing).

### Support intent recognition

- Occurs when a user is seeking actionable help to fix, recover, or resolve a specific issue, error, or technical problem related to their Meta assets, accounts, payments, or advertising activities, or seeking to speak with a human agent. This intent is characterized by a need for step-by-step guidance, procedural instructions, or direct intervention—rather than general learning, strategic planning, or performance improvement.

## 6. When to use this skill

Use this skill to analyze and diagnose Meta Ads campaign performance, including interpreting data, identifying root causes, generating recommendations, and understanding budget allocation.
