# Privacy Policy — Monvita Wellness Group S.L. (Nyvos)

**Last updated:** August 2026

## 1. Who we are

Monvita Wellness Group S.L. ("we", "us", "our") is a Spanish company that
markets and sells nutritional supplements under the Nyvos brand. Our
registered address is in Spain.

Contact: salesops@monvita.co

## 2. Scope of this policy

This Privacy Policy describes how we handle data in our internal
inventory-management operations, including data pulled from third-party
platforms (Amazon SP-API, TikTok Shop API, Google Sheets) into our
private dashboard for supplier-order planning.

We do NOT process personal data of end-consumers as part of these
operations. All data we retrieve is operational: aggregated inventory
levels, aggregated sales velocity, product listings, and purchase orders.

## 3. Data we process

- **Inventory data**: unit counts per SKU per warehouse, aggregated across
  Amazon FBA and TikTok FBT.
- **Sales aggregates**: 30-day sales velocity per SKU per market. No
  customer-level order data is retained.
- **Product catalog**: our own SKUs, titles, cost prices, lead times.
- **Purchase orders**: our supplier PO records.

## 4. Third-party services we use

- **Supabase** (Stockholm, Sweden – EU) — encrypted PostgreSQL storage
- **Streamlit Community Cloud** (United States) — application hosting
- **GitHub** (United States) — source-code hosting
- **Amazon SP-API** (EU) — inventory & sales sync
- **TikTok Shop API** (EU) — TikTok FBT inventory sync
- **Google Sheets** — forecast data

All external communication uses TLS. Data at rest is encrypted at the
storage layer.

## 5. Legal basis (GDPR)

We rely on **legitimate interest** (GDPR Art. 6(1)(f)) to process
operational business data required to run our supplier reorder planning.
We do not process special categories of personal data.

## 6. Data retention

Operational data is retained for as long as it is required for
day-to-day operations. Historical inventory snapshots older than 12
months are periodically archived or deleted.

## 7. Your rights

If you believe your personal data may be affected by our operations
(e.g. you are a supplier or team member), you may request access,
rectification, deletion, restriction of processing, or portability at
any time. Contact **salesops@monvita.co** — we respond within 72
hours.

## 8. Security

Access to our dashboard is restricted via email allowlists. API secrets
are stored in encrypted secret managers (Streamlit Cloud Secrets,
GitHub Actions Secrets). All device access is protected by strong
passwords and 2FA where available. Suspected incidents are handled by
immediate credential rotation and access audit.

## 9. Changes to this policy

We may update this policy from time to time. The "Last updated" date at
the top reflects the most recent revision. Material changes are
communicated through our internal dashboard.

## 10. Contact

Questions or requests concerning this policy:
**salesops@monvita.co**
