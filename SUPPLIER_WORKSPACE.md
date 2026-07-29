# B&BS Supplier and Offer Workspace

The supplier workspace links factories, inquiries and normalized commercial offers to a Kommo project.

## Telegram workflow

Open a lead and press `🏭 Фабрики и предложения`.

### Add a factory

Send one line:

```text
Factory name | source URL | contact | notes
```

Only the factory name is mandatory. Links from 1688, Alibaba and Made-in-China are classified automatically. The same factory URL/name combination is idempotent within one project.

### Mark an inquiry as sent

The action stores the send time and a three-day response deadline. It does not send a message to the factory. Communication still happens through the manager's chosen channel.

### Add an offer

Send normalized commercial data:

```text
currency | Incoterm and named place | unit price | total price | MOQ | lead-time days | warranty months | payment terms | certificates | notes
```

Example:

```text
USD | FOB Qingdao | 17900 | 17900 | 1 set | 50 | 18 | 30/70 | CE, ISO | engine model pending
```

The database also supports source artifact IDs, URLs, packaging, key components, deviations and raw extracted JSON for later automatic PDF/Excel ingestion.

## Comparison safety

Offers are compared only inside groups with the same:

- currency;
- Incoterm;
- named place.

The system does not convert currencies or compare EXW against FOB/DDP as if they were equivalent. Mixed conditions generate an explicit warning. Missing Incoterms and certificates are also highlighted.

Within a comparable group the summary can identify:

- lowest total price or unit price;
- shortest lead time;
- warranty, MOQ and payment differences;
- missing certifications and declared deviations.

## Database

Migration `012_supplier_offer_workspace` creates:

- `project_suppliers`;
- `supplier_inquiries`;
- `supplier_offers`.

The full stacked branch is validated against `main` before the PR is returned to its normal stacked base.

## Merge order

This change is stacked after:

1. PR #50 — automatic follow-up engine;
2. PR #51 — unified communications;
3. this supplier workspace PR.

No factory request, client message, Kommo stage or Google Sheets field is changed automatically.
